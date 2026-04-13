#!/usr/bin/env python3
"""
retry_failed.py — Scan every user's failed/ directory, diagnose why each
file is there, attempt to re-upload to S3, and clean up on success.

Designed to run hourly via cron alongside camera_sync.py.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# ──────────────────────────────────────────────────────────────────────────────
# Configuration — must match camera_sync.py values
# ──────────────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(r"/var/ftp/local")
BUCKET        = "<S3-BUCKET-NAME>"
PREFIX        = "cam"
LOG_DIR       = Path(r"/var/log/camera")
LOCKFILE      = Path(r"/var/run/retry_failed.lock")

ELIGIBLE_EXTS = {".jpg", ".jpeg", ".png", ".txt"}
MAX_RETRIES   = 3

MAX_PROCESS_WORKERS: int | None = None
MAX_THREAD_WORKERS:  int | None = None

LOG_MAX_BYTES  = 10 * 1024 * 1024
LOG_BACKUP_CNT = 5

SKIP_DIRS      = {".spool", "failed"}


# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def _build_logger(name: str = "retry_failed") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(processName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    fh = RotatingFileHandler(
        LOG_DIR / "retry.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_CNT,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    eh = RotatingFileHandler(
        LOG_DIR / "retry_error.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_CNT,
    )
    eh.setLevel(logging.WARNING)
    eh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(eh)
    logger.addHandler(ch)
    return logger


log = _build_logger()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pool_sizes(user_count: int, file_count: int) -> Tuple[int, int]:
    cpus = os.cpu_count() or 4
    proc_w = MAX_PROCESS_WORKERS if MAX_PROCESS_WORKERS is not None else min(user_count, cpus)
    if MAX_THREAD_WORKERS is not None:
        thr_w = MAX_THREAD_WORKERS
    else:
        per_proc = max(1, file_count // max(proc_w, 1))
        thr_w = max(4, min(per_proc, 20))
    return max(proc_w, 1), max(thr_w, 1)


def _md5_hex(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _prune_empty_dirs(root: Path) -> None:
    for dirpath, _, _ in os.walk(str(root), topdown=False):
        dp = Path(dirpath)
        if dp == root:
            continue
        try:
            dp.rmdir()
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Diagnosis — figure out *why* a file is in failed/
# ──────────────────────────────────────────────────────────────────────────────

def _diagnose(s3_client, bucket: str, s3_key: str, local_path: Path) -> str:
    """Return a human-readable reason string for a file sitting in failed/.

    Possible outcomes:
      - "already_on_s3"         — file exists and matches (previous cleanup missed it)
      - "s3_size_mismatch"      — file exists on S3 but sizes differ
      - "s3_md5_mismatch"       — file exists on S3 but checksums differ
      - "not_on_s3"             — file never made it to S3
      - "local_unreadable"      — can't stat/read the local file
      - "s3_access_error: ..."  — permissions or connectivity issue
    """
    try:
        local_size = local_path.stat().st_size
        local_md5 = _md5_hex(local_path)
    except OSError as exc:
        return f"local_unreadable: {exc}"

    try:
        head = s3_client.head_object(Bucket=bucket, Key=s3_key)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("404", "NoSuchKey"):
            return "not_on_s3"
        return f"s3_access_error: {exc}"
    except BotoCoreError as exc:
        return f"s3_access_error: {exc}"

    remote_size = head["ContentLength"]
    remote_etag = head["ETag"].strip('"')

    if remote_size != local_size:
        return f"s3_size_mismatch: local={local_size} remote={remote_size}"

    if "-" not in remote_etag and remote_etag != local_md5:
        return f"s3_md5_mismatch: local={local_md5} remote={remote_etag}"

    return "already_on_s3"


# ──────────────────────────────────────────────────────────────────────────────
# Upload + verify (same logic as camera_sync.py)
# ──────────────────────────────────────────────────────────────────────────────

def _upload_and_verify(
    s3_client, bucket: str, s3_key: str, local_path: Path,
) -> Tuple[bool, str, float, float]:
    """Returns (success, message, upload_seconds, verify_seconds)."""
    local_size = local_path.stat().st_size
    local_md5 = _md5_hex(local_path)

    t_upload0 = time.perf_counter()
    try:
        s3_client.upload_file(str(local_path), bucket, s3_key)
    except (BotoCoreError, ClientError) as exc:
        upload_sec = time.perf_counter() - t_upload0
        return False, f"Upload failed: {exc}", upload_sec, 0.0
    upload_sec = time.perf_counter() - t_upload0

    t_verify0 = time.perf_counter()
    try:
        head = s3_client.head_object(Bucket=bucket, Key=s3_key)
    except (BotoCoreError, ClientError) as exc:
        verify_sec = time.perf_counter() - t_verify0
        return False, f"Verify head-object failed: {exc}", upload_sec, verify_sec

    remote_size = head["ContentLength"]
    remote_etag = head["ETag"].strip('"')

    if remote_size != local_size:
        verify_sec = time.perf_counter() - t_verify0
        return (
            False,
            f"Size mismatch: local={local_size} remote={remote_size}",
            upload_sec,
            verify_sec,
        )

    if "-" not in remote_etag and remote_etag != local_md5:
        verify_sec = time.perf_counter() - t_verify0
        return (
            False,
            f"MD5 mismatch: local={local_md5} remote={remote_etag}",
            upload_sec,
            verify_sec,
        )

    verify_sec = time.perf_counter() - t_verify0
    return True, "OK", upload_sec, verify_sec


# ──────────────────────────────────────────────────────────────────────────────
# Per-file retry (runs inside thread worker)
# ──────────────────────────────────────────────────────────────────────────────

def _retry_file(
    s3_client, bucket: str, user: str, failed_dir: Path, fpath: Path,
) -> Tuple[Path, str, bool, str, float, float]:
    """Diagnose, optionally re-upload; returns (path, rel, ok, detail, upload_s, verify_s)."""
    rel = fpath.relative_to(failed_dir)
    s3_key = f"{PREFIX}/{user}/{rel.as_posix()}"
    rlog = _build_logger()

    reason = _diagnose(s3_client, bucket, s3_key, fpath)
    rlog.info("%s | DIAG | %s | reason=%s", user, rel, reason)

    if reason == "already_on_s3":
        return fpath, str(rel), True, "already_on_s3 (deleting local)", 0.0, 0.0

    if reason.startswith("local_unreadable"):
        return fpath, str(rel), False, reason, 0.0, 0.0

    if reason.startswith("s3_access_error"):
        return fpath, str(rel), False, reason, 0.0, 0.0

    # Needs re-upload: not_on_s3 / size_mismatch / md5_mismatch
    last_err = reason
    for attempt in range(1, MAX_RETRIES + 1):
        ok, msg, up_s, ver_s = _upload_and_verify(
            s3_client, bucket, s3_key, fpath,
        )
        if ok:
            rlog.info(
                "%s | RETRY OK | %s | attempt=%d/%d | upload_s=%.3f verify_s=%.3f",
                user, rel, attempt, MAX_RETRIES, up_s, ver_s,
            )
            return (
                fpath,
                str(rel),
                True,
                f"retry_ok (attempt {attempt})",
                up_s,
                ver_s,
            )
        last_err = msg
        rlog.warning(
            "%s | RETRY FAIL | %s | attempt=%d/%d | %s",
            user, rel, attempt, MAX_RETRIES, msg,
        )

    return (
        fpath,
        str(rel),
        False,
        f"exhausted {MAX_RETRIES} retries: {last_err}",
        0.0,
        0.0,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Per-user pipeline (runs inside a child process)
# ──────────────────────────────────────────────────────────────────────────────

def retry_user(user: str, thread_workers: int) -> dict:
    plog = _build_logger()
    stats: dict = {
        "user": user,
        "ok": 0,
        "failed": 0,
        "errors": [],
        "upload_sec": 0.0,
        "verify_sec": 0.0,
    }
    user_root = BASE_DIR / user
    failed_dir = user_root / "failed"

    if not failed_dir.is_dir():
        return stats

    files = [
        f for f in failed_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in ELIGIBLE_EXTS
    ]

    if not files:
        return stats

    plog.info("%s | Found %d file(s) in failed/", user, len(files))

    s3_client = boto3.client("s3")

    with ThreadPoolExecutor(max_workers=thread_workers) as pool:
        futures = {
            pool.submit(_retry_file, s3_client, BUCKET, user, failed_dir, fp): fp
            for fp in files
        }

        for fut in as_completed(futures):
            fp = futures[fut]
            try:
                fpath, rel, ok, detail, up_s, ver_s = fut.result()
            except Exception as exc:
                plog.error("%s | Unhandled | %s | %s", user, fp, exc)
                stats["failed"] += 1
                stats["errors"].append(str(fp))
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

    _prune_empty_dirs(failed_dir)

    plog.info(
        "%s | Retry done | ok=%d still_failed=%d | upload_s=%.3f verify_s=%.3f",
        user,
        stats["ok"],
        stats["failed"],
        stats["upload_sec"],
        stats["verify_sec"],
    )
    return stats


# ──────────────────────────────────────────────────────────────────────────────
# Lock file
# ──────────────────────────────────────────────────────────────────────────────

class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fd = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self.path, "w")
        if platform.system() == "Windows":
            import msvcrt
            try:
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except (IOError, OSError):
                self._fd.close()
                self._fd = None
                return False
        else:
            import fcntl
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (IOError, OSError):
                self._fd.close()
                self._fd = None
                return False

    def release(self) -> None:
        if self._fd is None:
            return
        if platform.system() == "Windows":
            import msvcrt
            try:
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (IOError, OSError):
                pass
        else:
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()
        self._fd = None


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    lock = _FileLock(LOCKFILE)
    if not lock.acquire():
        log.info("Retry skipped | previous run still in progress")
        return

    try:
        _run_retry()
    finally:
        lock.release()


def _run_retry() -> None:
    log.info(
        "Retry start | base=%s | bucket=%s | prefix=%s",
        BASE_DIR, BUCKET, PREFIX,
    )

    users = sorted(
        d.name for d in BASE_DIR.iterdir()
        if d.is_dir() and d.name not in SKIP_DIRS
    )

    if not users:
        log.info("No user directories found")
        return

    users_with_failed = [
        u for u in users if (BASE_DIR / u / "failed").is_dir()
        and any((BASE_DIR / u / "failed").rglob("*"))
    ]

    if not users_with_failed:
        log.info("No failed files found across any user")
        return

    total_files = sum(
        1
        for u in users_with_failed
        for f in (BASE_DIR / u / "failed").rglob("*")
        if f.is_file()
    )

    proc_w, thr_w = _pool_sizes(len(users_with_failed), total_files)
    log.info(
        "Retry %d user(s), %d failed file(s) → %d process(es), %d thread(s)/process",
        len(users_with_failed), total_files, proc_w, thr_w,
    )

    all_stats: list[dict] = []

    with ProcessPoolExecutor(max_workers=proc_w) as pool:
        future_map = {
            pool.submit(retry_user, user, thr_w): user
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


if __name__ == "__main__":
    main()
