#!/usr/bin/env python3
"""
retry_failed.py — Scan every user's failed/ directory, diagnose why each
file is there, attempt to re-upload to S3, and clean up on success.
"""

from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Tuple

import boto3

from sync_common import (
    Config,
    ConfigError,
    FileLock,
    build_logger,
    diagnose_s3_object,
    load_config,
    log_upload_failure,
    make_s3_key,
    pool_sizes,
    prune_empty_dirs,
    upload_with_retries,
)

SCRIPT_NAME = "retry_failed"
LOCKFILE = Path("/var/run/retry_failed.lock")

try:
    CFG = load_config()
except ConfigError as exc:
    print(f"Configuration error: {exc}", file=sys.stderr)
    sys.exit(1)

log = build_logger(CFG, SCRIPT_NAME, "retry.log", "retry_error.log")


def _retry_file(
    cfg: Config,
    s3_client,
    user: str,
    failed_dir: Path,
    fpath: Path,
) -> Tuple[Path, str, bool, str, float, float, int]:
    """Diagnose, optionally re-upload; returns (path, rel, ok, detail, up_s, ver_s, attempts)."""
    rel = fpath.relative_to(failed_dir)
    file_path = rel.as_posix()
    s3_key = make_s3_key(cfg, user, file_path)
    rlog = build_logger(cfg, SCRIPT_NAME, "retry.log", "retry_error.log")

    reason = diagnose_s3_object(s3_client, cfg, s3_key, fpath)
    rlog.info("%s | DIAG | %s | reason=%s", user, rel, reason)

    if reason == "already_on_s3":
        return fpath, file_path, True, "already_on_s3 (deleting local)", 0.0, 0.0, 0

    if reason.startswith("local_unreadable"):
        log_upload_failure(
            cfg,
            script=SCRIPT_NAME,
            camera=user,
            file_path=file_path,
            reason=reason,
            attempts=0,
            error_logger=rlog,
        )
        return fpath, file_path, False, reason, 0.0, 0.0, 0

    if reason.startswith("s3_access_error"):
        log_upload_failure(
            cfg,
            script=SCRIPT_NAME,
            camera=user,
            file_path=file_path,
            reason=reason,
            attempts=0,
            error_logger=rlog,
        )
        return fpath, file_path, False, reason, 0.0, 0.0, 0

    ok, msg, up_s, ver_s, attempts = upload_with_retries(
        s3_client, cfg, s3_key, fpath,
    )
    if ok:
        rlog.info(
            "%s | RETRY OK | %s | attempt=%d/%d | upload_s=%.3f verify_s=%.3f",
            user, rel, attempts, cfg.max_retries, up_s, ver_s,
        )
        return (
            fpath,
            file_path,
            True,
            f"retry_ok (attempt {attempts})",
            up_s,
            ver_s,
            attempts,
        )

    detail = f"exhausted {cfg.max_retries} retries: {msg}"
    rlog.warning("%s | RETRY FAIL | %s | %s", user, rel, detail)
    log_upload_failure(
        cfg,
        script=SCRIPT_NAME,
        camera=user,
        file_path=file_path,
        reason=detail,
        attempts=attempts,
        error_logger=rlog,
    )
    return fpath, file_path, False, detail, up_s, ver_s, attempts


def retry_user(cfg: Config, user: str, thread_workers: int) -> dict:
    plog = build_logger(cfg, SCRIPT_NAME, "retry.log", "retry_error.log")
    stats: dict = {
        "user": user,
        "ok": 0,
        "failed": 0,
        "errors": [],
        "upload_sec": 0.0,
        "verify_sec": 0.0,
    }
    user_root = cfg.base_dir / user
    failed_dir = user_root / "failed"

    if not failed_dir.is_dir():
        return stats

    files = [
        f for f in failed_dir.rglob("*")
        if f.is_file()
        and not f.is_symlink()
        and f.suffix.lower() in cfg.eligible_exts
    ]

    if not files:
        return stats

    plog.info("%s | Found %d file(s) in failed/", user, len(files))

    s3_client = boto3.client("s3")

    with ThreadPoolExecutor(max_workers=thread_workers) as pool:
        futures = {
            pool.submit(_retry_file, cfg, s3_client, user, failed_dir, fp): fp
            for fp in files
        }

        for fut in as_completed(futures):
            fp = futures[fut]
            try:
                fpath, rel, ok, detail, up_s, ver_s, _attempts = fut.result()
            except Exception as exc:
                plog.error("%s | Unhandled | %s | %s", user, fp, exc)
                stats["failed"] += 1
                stats["errors"].append(str(fp))
                rel_str = str(fp.relative_to(failed_dir)) if fp.is_relative_to(failed_dir) else str(fp)
                log_upload_failure(
                    cfg,
                    script=SCRIPT_NAME,
                    camera=user,
                    file_path=rel_str.replace("\\", "/"),
                    reason=f"Unhandled: {exc}",
                    attempts=cfg.max_retries,
                    error_logger=plog,
                )
                continue

            if ok:
                try:
                    fpath.unlink()
                except OSError:
                    pass
                stats["upload_sec"] += up_s
                stats["verify_sec"] += ver_s
                plog.info(
                    "%s | RESOLVED | %s | %s | upload_s=%.3f verify_s=%.3f",
                    user, rel, detail, up_s, ver_s,
                )
                stats["ok"] += 1
            else:
                plog.warning("%s | STILL FAILED | %s | %s", user, rel, detail)
                stats["failed"] += 1
                stats["errors"].append(f"{rel}: {detail}")

    prune_empty_dirs(failed_dir)

    plog.info(
        "%s | Retry done | ok=%d still_failed=%d | upload_s=%.3f verify_s=%.3f",
        user,
        stats["ok"],
        stats["failed"],
        stats["upload_sec"],
        stats["verify_sec"],
    )
    return stats


def _run_retry(cfg: Config) -> None:
    log.info(
        "Retry start | project=%s | base=%s | bucket=%s | prefix=%s",
        cfg.project, cfg.base_dir, cfg.bucket, cfg.prefix,
    )

    users = sorted(
        d.name for d in cfg.base_dir.iterdir()
        if d.is_dir() and d.name not in cfg.internal_dirs
    )

    if not users:
        log.info("No user directories found")
        return

    users_with_failed = [
        u for u in users if (cfg.base_dir / u / "failed").is_dir()
        and any((cfg.base_dir / u / "failed").rglob("*"))
    ]

    if not users_with_failed:
        log.info("No failed files found across any user")
        return

    total_files = sum(
        1
        for u in users_with_failed
        for f in (cfg.base_dir / u / "failed").rglob("*")
        if f.is_file()
    )

    proc_w, thr_w = pool_sizes(cfg, len(users_with_failed), total_files)
    log.info(
        "Retry %d user(s), %d failed file(s) → %d process(es), %d thread(s)/process",
        len(users_with_failed), total_files, proc_w, thr_w,
    )

    all_stats: list[dict] = []

    with ProcessPoolExecutor(max_workers=proc_w) as pool:
        future_map = {
            pool.submit(retry_user, cfg, user, thr_w): user
            for user in users_with_failed
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
                    "errors": ["process crash"],
                    "upload_sec": 0.0,
                    "verify_sec": 0.0,
                })

    total_ok = sum(s["ok"] for s in all_stats)
    total_fail = sum(s["failed"] for s in all_stats)
    total_upload_s = sum(float(s.get("upload_sec", 0.0)) for s in all_stats)
    total_verify_s = sum(float(s.get("verify_sec", 0.0)) for s in all_stats)
    log.info(
        "Retry end | resolved=%d still_failed=%d | upload_s=%.3f verify_s=%.3f",
        total_ok,
        total_fail,
        total_upload_s,
        total_verify_s,
    )


def main() -> None:
    lock = FileLock(LOCKFILE)
    if not lock.acquire():
        log.info("Retry skipped | previous run still in progress")
        return

    try:
        _run_retry(CFG)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
