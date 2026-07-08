#!/usr/bin/env python3
"""
camera_sync.py — Sync newly uploaded files from a local FTP directory tree
to AWS S3, verify each upload, then delete the local copy or quarantine it.
"""

from __future__ import annotations

import platform
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Tuple

import boto3

from sync_common import (
    Config,
    ConfigError,
    FileLock,
    atomic_move,
    build_logger,
    diagnose_s3_object,
    is_eligible_file,
    load_config,
    log_upload_failure,
    make_s3_key,
    pool_sizes,
    prune_empty_dirs,
    upload_with_retries,
    write_alive_marker,
)

SCRIPT_NAME = "camera_sync"
LOCKFILE = Path("/var/run/camera_sync.lock")

try:
    CFG = load_config()
except ConfigError as exc:
    print(f"Configuration error: {exc}", file=sys.stderr)
    sys.exit(1)

log = build_logger(CFG, SCRIPT_NAME, "sync.log", "error.log")


def sync_user(cfg: Config, user: str, thread_workers: int) -> dict:
    """Full scan → spool → upload → verify → cleanup for one user."""
    proc_log = build_logger(cfg, SCRIPT_NAME, "sync.log", "error.log")
    stats: dict = {
        "user": user,
        "ok": 0,
        "failed": 0,
        "skipped": 0,
        "rejected": 0,
        "errors": [],
        "upload_sec": 0.0,
        "verify_sec": 0.0,
    }
    user_root = cfg.base_dir / user
    spool_dir = user_root / ".spool"
    failed_dir = user_root / "failed"
    s3_client = boto3.client("s3")

    # ── 1. Recover files stranded in .spool/ from a previous crash ────────
    if spool_dir.is_dir():
        for stranded in spool_dir.rglob("*"):
            eligible, skip_reason = is_eligible_file(
                stranded, spool_dir, time.time(), cfg, check_age=False,
            )
            if not eligible:
                if skip_reason not in ("extension", "internal_dir"):
                    proc_log.warning(
                        "%s | Spool skip | %s | %s",
                        user,
                        stranded.relative_to(spool_dir) if stranded.is_relative_to(spool_dir) else stranded,
                        skip_reason,
                    )
                continue

            rel = stranded.relative_to(spool_dir)
            s3_key = make_s3_key(cfg, user, rel.as_posix())
            diagnosis = diagnose_s3_object(s3_client, cfg, s3_key, stranded)
            if diagnosis == "already_on_s3":
                try:
                    stranded.unlink()
                except OSError:
                    pass
                proc_log.info(
                    "%s | Spool already on S3 (deleted) | %s", user, rel,
                )
                continue

            recover_target = user_root / rel
            atomic_move(stranded, recover_target)
            proc_log.info("%s | Recovered from spool | %s", user, rel)

    # ── 2. Scan for eligible files ────────────────────────────────────────
    now = time.time()
    eligible: List[Path] = []

    for fpath in user_root.rglob("*"):
        ok, skip_reason = is_eligible_file(fpath, user_root, now, cfg)
        if not ok:
            if skip_reason == "too_young":
                stats["skipped"] += 1
            elif skip_reason in ("magic_bytes", "unsafe_path", "symlink", "outside_root"):
                rel_display = (
                    fpath.relative_to(user_root)
                    if fpath.is_relative_to(user_root)
                    else fpath
                )
                proc_log.warning(
                    "%s | Rejected | %s | %s",
                    user,
                    rel_display,
                    skip_reason,
                )
                stats["rejected"] += 1
                log_upload_failure(
                    cfg,
                    script=SCRIPT_NAME,
                    camera=user,
                    file_path=rel_display.as_posix() if isinstance(rel_display, Path) else str(rel_display),
                    reason=skip_reason,
                    attempts=0,
                    error_logger=proc_log,
                    event="file_rejected",
                )
            continue
        eligible.append(fpath)

    if not eligible:
        proc_log.info("%s | No eligible files", user)
        return stats

    # ── 3. Spool — atomic move into .spool/ ──────────────────────────────
    spooled: List[Tuple[Path, Path]] = []
    for fpath in eligible:
        rel = fpath.relative_to(user_root)
        spool_target = spool_dir / rel
        try:
            atomic_move(fpath, spool_target)
            spooled.append((spool_target, rel))
        except OSError as exc:
            proc_log.warning("%s | Spool move failed | %s | %s", user, rel, exc)
            stats["errors"].append(str(rel))
            log_upload_failure(
                cfg,
                script=SCRIPT_NAME,
                camera=user,
                file_path=rel.as_posix(),
                reason=f"Spool move failed: {exc}",
                attempts=0,
                error_logger=proc_log,
            )
            stats["failed"] += 1

    proc_log.info(
        "%s | Staged %d file(s) → syncing to s3://%s/%s/%s/",
        user, len(spooled), cfg.bucket, cfg.prefix, user,
    )

    # ── 4. Upload + Verify (threaded) ────────────────────────────────────
    def _process_file(
        spool_path: Path, rel: Path,
    ) -> Tuple[Path, Path, bool, str, float, float, int]:
        s3_key = make_s3_key(cfg, user, rel.as_posix())
        ok, msg, up_s, ver_s, attempts = upload_with_retries(
            s3_client, cfg, s3_key, spool_path,
        )
        return spool_path, rel, ok, msg, up_s, ver_s, attempts

    with ThreadPoolExecutor(max_workers=thread_workers) as pool:
        futures = {
            pool.submit(_process_file, sp, rel): rel
            for sp, rel in spooled
        }

        for fut in as_completed(futures):
            rel = futures[fut]
            try:
                spool_path, rel_out, ok, msg, up_s, ver_s, attempts = fut.result()
            except Exception as exc:
                proc_log.error("%s | Unhandled | %s | %s", user, rel, exc)
                stats["failed"] += 1
                stats["errors"].append(str(rel))
                log_upload_failure(
                    cfg,
                    script=SCRIPT_NAME,
                    camera=user,
                    file_path=str(rel),
                    reason=f"Unhandled: {exc}",
                    attempts=cfg.max_retries,
                    error_logger=proc_log,
                )
                continue

            if ok:
                try:
                    spool_path.unlink()
                except OSError:
                    pass
                stats["upload_sec"] += up_s
                stats["verify_sec"] += ver_s
                proc_log.info(
                    "%s | OK | %s (deleted) | upload_s=%.3f verify_s=%.3f",
                    user, rel_out, up_s, ver_s,
                )
                stats["ok"] += 1
            else:
                atomic_move(spool_path, failed_dir / rel_out)
                proc_log.warning(
                    "%s | FAIL | %s | %s | upload_s=%.3f verify_s=%.3f",
                    user, rel_out, msg, up_s, ver_s,
                )
                stats["failed"] += 1
                stats["errors"].append(f"{rel_out}: {msg}")
                log_upload_failure(
                    cfg,
                    script=SCRIPT_NAME,
                    camera=user,
                    file_path=rel_out.as_posix(),
                    reason=msg,
                    attempts=attempts,
                    error_logger=proc_log,
                )

    if spool_dir.is_dir():
        prune_empty_dirs(spool_dir)

    proc_log.info(
        "%s | Done | ok=%d failed=%d rejected=%d skipped=%d | upload_s=%.3f verify_s=%.3f",
        user,
        stats["ok"],
        stats["failed"],
        stats["rejected"],
        stats["skipped"],
        stats["upload_sec"],
        stats["verify_sec"],
    )
    return stats


def _run_sync(cfg: Config) -> None:
    log.info(
        "Sync start | project=%s | base=%s | bucket=%s | prefix=%s",
        cfg.project, cfg.base_dir, cfg.bucket, cfg.prefix,
    )

    users: List[str] = sorted(
        d.name
        for d in cfg.base_dir.iterdir()
        if d.is_dir() and d.name not in cfg.internal_dirs
    )

    if not users:
        log.info("No user directories found under %s", cfg.base_dir)
        return

    proc_w, thr_w = pool_sizes(cfg, len(users), len(users))
    log.info(
        "Detected %d user(s) → %d process(es), %d thread(s)/process",
        len(users), proc_w, thr_w,
    )

    all_stats: List[dict] = []

    with ProcessPoolExecutor(max_workers=proc_w) as pool:
        future_map = {
            pool.submit(sync_user, cfg, user, thr_w): user for user in users
        }
        for fut in as_completed(future_map):
            user = future_map[fut]
            try:
                stats = fut.result()
                all_stats.append(stats)
            except Exception:
                log.exception("Process-level failure for user %s", user)
                all_stats.append({
                    "user": user,
                    "ok": 0,
                    "failed": 0,
                    "rejected": 0,
                    "errors": ["process crash"],
                    "upload_sec": 0.0,
                    "verify_sec": 0.0,
                })

    total_ok = sum(s["ok"] for s in all_stats)
    total_fail = sum(s["failed"] for s in all_stats)
    total_rejected = sum(s.get("rejected", 0) for s in all_stats)
    total_upload_s = sum(float(s.get("upload_sec", 0.0)) for s in all_stats)
    total_verify_s = sum(float(s.get("verify_sec", 0.0)) for s in all_stats)

    write_alive_marker(cfg)

    log.info(
        "Sync end | total_ok=%d total_failed=%d total_rejected=%d | upload_s=%.3f verify_s=%.3f",
        total_ok,
        total_fail,
        total_rejected,
        total_upload_s,
        total_verify_s,
    )


def _timeout_handler(signum, frame) -> None:
    log.error("Sync timed out after %d seconds", CFG.timeout_sec)
    sys.exit(124)


def main() -> None:
    lock = FileLock(LOCKFILE)
    if not lock.acquire():
        log.info("Sync skipped | previous run still in progress")
        return

    alarm_set = False
    if CFG.timeout_sec > 0 and platform.system() != "Windows":
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(CFG.timeout_sec)
        alarm_set = True

    try:
        _run_sync(CFG)
    finally:
        if alarm_set:
            signal.alarm(0)
        lock.release()


if __name__ == "__main__":
    main()
