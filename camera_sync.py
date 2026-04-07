#!/usr/bin/env python3
"""
camera_sync.py — Sync newly uploaded files from a local FTP directory tree
to AWS S3, verify each upload, then delete the local copy or quarantine it.

Replaces the original camera_sync.sh with:
  • ProcessPoolExecutor  – one worker per user directory
  • ThreadPoolExecutor   – concurrent I/O-bound S3 uploads inside each process
  • Per-file MD5 / size verification against S3 ETag
  • Thread-safe rotating log files
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# ──────────────────────────────────────────────────────────────────────────────
# Configuration — adjust these before deployment
# ──────────────────────────────────────────────────────────────────────────────
BASE_DIR        = Path(r"/var/ftp/local")
BUCKET          = "<S3-BUCKET-NAME>"
PREFIX          = "cam"
LOG_DIR         = Path(r"/var/log/camera")
LOCKFILE        = Path(r"/var/run/camera_sync.lock")

MIN_AGE_SEC     = 120           # ignore files younger than this (still being written)
ELIGIBLE_EXTS   = {".jpg", ".jpeg", ".png", ".txt"}

# Pool sizing — None ⇒ auto-scale (see _pool_sizes())
MAX_PROCESS_WORKERS: int | None = None
MAX_THREAD_WORKERS:  int | None = None

LOG_MAX_BYTES   = 10 * 1024 * 1024   # 10 MiB per log file
LOG_BACKUP_CNT  = 5

INTERNAL_DIRS   = {".spool", "failed"}


# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def _build_logger(name: str = "camera_sync") -> logging.Logger:
    """Return a logger that writes to both console and a rotating file."""
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
        LOG_DIR / "sync.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_CNT,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    eh = RotatingFileHandler(
        LOG_DIR / "error.log",
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
    """Pick process and thread pool sizes dynamically.

    Returns (process_workers, thread_workers_per_process).
    """
    cpus = os.cpu_count() or 4

    if MAX_PROCESS_WORKERS is not None:
        proc_w = MAX_PROCESS_WORKERS
    else:
        proc_w = min(user_count, cpus)

    if MAX_THREAD_WORKERS is not None:
        thr_w = MAX_THREAD_WORKERS
    else:
        per_proc = max(1, file_count // max(proc_w, 1))
        thr_w = min(per_proc, 20)
        thr_w = max(thr_w, 4)

    return max(proc_w, 1), max(thr_w, 1)


def md5_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Return the hex MD5 digest of a file (matches single-part S3 ETag)."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def _atomic_move(src: Path, dst: Path) -> None:
    """Move *src* to *dst* atomically (same filesystem) or with copy+delete."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)
    except OSError:
        shutil.move(str(src), str(dst))


def _prune_empty_dirs(root: Path) -> None:
    """Delete empty sub-directories under *root*, bottom-up."""
    for dirpath, dirnames, filenames in os.walk(str(root), topdown=False):
        dp = Path(dirpath)
        if dp == root:
            continue
        try:
            dp.rmdir()
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# S3 operations (called inside thread workers)
# ──────────────────────────────────────────────────────────────────────────────

def _upload_and_verify(
    s3_client,
    bucket: str,
    s3_key: str,
    local_path: Path,
) -> Tuple[bool, str]:
    """Upload a single file to S3 and verify it.

    Returns (success: bool, message: str).
    """
    local_size = local_path.stat().st_size
    local_md5 = md5_file(local_path)

    try:
        s3_client.upload_file(
            str(local_path),
            bucket,
            s3_key,
            ExtraArgs={"ContentMD5": _b64md5(local_path)},
        )
    except (BotoCoreError, ClientError) as exc:
        return False, f"Upload failed: {exc}"

    try:
        head = s3_client.head_object(Bucket=bucket, Key=s3_key)
    except (BotoCoreError, ClientError) as exc:
        return False, f"Verify head-object failed: {exc}"

    remote_size = head["ContentLength"]
    remote_etag = head["ETag"].strip('"')

    if remote_size != local_size:
        return False, (
            f"Size mismatch: local={local_size} remote={remote_size}"
        )

    # ETag equals MD5 only for single-part uploads (files < 5 GiB by default).
    if "-" not in remote_etag and remote_etag != local_md5:
        return False, (
            f"MD5 mismatch: local={local_md5} remote={remote_etag}"
        )

    return True, "OK"


def _b64md5(path: Path) -> str:
    """Return the base64-encoded MD5 required by S3 ContentMD5."""
    import base64
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode()


# ──────────────────────────────────────────────────────────────────────────────
# Per-user pipeline (runs inside a child process)
# ──────────────────────────────────────────────────────────────────────────────

def sync_user(user: str, thread_workers: int) -> dict:
    """Full scan → spool → upload → verify → cleanup for one user.

    Returns a stats dict: {user, ok, failed, skipped, errors[]}.
    """
    proc_log = _build_logger()
    stats: dict = {"user": user, "ok": 0, "failed": 0, "skipped": 0, "errors": []}
    user_root = BASE_DIR / user
    spool_dir = user_root / ".spool"
    failed_dir = user_root / "failed"

    # ── 1. Recover files stranded in .spool/ from a previous crash ────────
    if spool_dir.is_dir():
        for stranded in spool_dir.rglob("*"):
            if not stranded.is_file():
                continue
            rel = stranded.relative_to(spool_dir)
            recover_target = user_root / rel
            _atomic_move(stranded, recover_target)
            proc_log.info("%s | Recovered from spool | %s", user, rel)

    # ── 2. Scan for eligible files ────────────────────────────────────────
    now = time.time()
    eligible: List[Path] = []

    for fpath in user_root.rglob("*"):
        if not fpath.is_file():
            continue

        rel = fpath.relative_to(user_root)
        top_dir = rel.parts[0] if rel.parts else ""
        if top_dir in INTERNAL_DIRS:
            continue

        if fpath.suffix.lower() not in ELIGIBLE_EXTS:
            continue

        try:
            age = now - fpath.stat().st_mtime
        except OSError:
            continue
        if age < MIN_AGE_SEC:
            stats["skipped"] += 1
            continue

        eligible.append(fpath)

    if not eligible:
        proc_log.info("%s | No eligible files", user)
        return stats

    # ── 3. Spool — atomic move into .spool/ ──────────────────────────────
    spooled: List[Tuple[Path, Path]] = []   # (spool_path, relative)
    for fpath in eligible:
        rel = fpath.relative_to(user_root)
        spool_target = spool_dir / rel
        try:
            _atomic_move(fpath, spool_target)
            spooled.append((spool_target, rel))
        except OSError as exc:
            proc_log.warning("%s | Spool move failed | %s | %s", user, rel, exc)
            stats["errors"].append(str(rel))

    proc_log.info(
        "%s | Staged %d file(s) → syncing to s3://%s/%s/%s/",
        user, len(spooled), BUCKET, PREFIX, user,
    )

    # ── 4. Upload + Verify (threaded) ────────────────────────────────────
    s3_client = boto3.client("s3")

    def _process_file(spool_path: Path, rel: Path) -> Tuple[Path, Path, bool, str]:
        s3_key = f"{PREFIX}/{user}/{rel.as_posix()}"
        ok, msg = _upload_and_verify(s3_client, BUCKET, s3_key, spool_path)
        return spool_path, rel, ok, msg

    with ThreadPoolExecutor(max_workers=thread_workers) as pool:
        futures = {
            pool.submit(_process_file, sp, rel): rel
            for sp, rel in spooled
        }

        for fut in as_completed(futures):
            rel = futures[fut]
            try:
                spool_path, rel_out, ok, msg = fut.result()
            except Exception as exc:
                proc_log.error("%s | Unhandled | %s | %s", user, rel, exc)
                stats["failed"] += 1
                stats["errors"].append(str(rel))
                continue

            if ok:
                try:
                    spool_path.unlink()
                except OSError:
                    pass
                proc_log.info("%s | OK | %s (deleted)", user, rel_out)
                stats["ok"] += 1
            else:
                _atomic_move(spool_path, failed_dir / rel_out)
                proc_log.warning("%s | FAIL | %s | %s", user, rel_out, msg)
                stats["failed"] += 1
                stats["errors"].append(f"{rel_out}: {msg}")

    # ── 5. Prune empty spool sub-dirs ────────────────────────────────────
    if spool_dir.is_dir():
        _prune_empty_dirs(spool_dir)

    proc_log.info(
        "%s | Done | ok=%d failed=%d skipped=%d",
        user, stats["ok"], stats["failed"], stats["skipped"],
    )
    return stats


# ──────────────────────────────────────────────────────────────────────────────
# Lock file
# ──────────────────────────────────────────────────────────────────────────────

class _FileLock:
    """Simple cross-platform exclusive file lock (non-blocking)."""

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

    def __enter__(self):
        if not self.acquire():
            raise SystemExit(0)
        return self

    def __exit__(self, *_):
        self.release()


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    lock = _FileLock(LOCKFILE)
    if not lock.acquire():
        log.info("Sync skipped | previous run still in progress")
        return

    try:
        _run_sync()
    finally:
        lock.release()


def _run_sync() -> None:
    log.info(
        "Sync start | base=%s | bucket=%s | prefix=%s",
        BASE_DIR, BUCKET, PREFIX,
    )

    users: List[str] = sorted(
        d.name
        for d in BASE_DIR.iterdir()
        if d.is_dir() and d.name not in INTERNAL_DIRS
    )

    if not users:
        log.info("No user directories found under %s", BASE_DIR)
        return

    total_files = sum(
        1
        for u in users
        for f in (BASE_DIR / u).rglob("*")
        if f.is_file()
        and f.suffix.lower() in ELIGIBLE_EXTS
        and f.relative_to(BASE_DIR / u).parts[0] not in INTERNAL_DIRS
    )

    proc_w, thr_w = _pool_sizes(len(users), total_files)
    log.info(
        "Detected %d user(s), ~%d eligible file(s) → %d process(es), %d thread(s)/process",
        len(users), total_files, proc_w, thr_w,
    )

    all_stats: List[dict] = []

    with ProcessPoolExecutor(max_workers=proc_w) as pool:
        future_map = {
            pool.submit(sync_user, user, thr_w): user for user in users
        }
        for fut in as_completed(future_map):
            user = future_map[fut]
            try:
                stats = fut.result()
                all_stats.append(stats)
            except Exception:
                log.exception("Process-level failure for user %s", user)
                all_stats.append({"user": user, "ok": 0, "failed": 0, "errors": ["process crash"]})

    total_ok = sum(s["ok"] for s in all_stats)
    total_fail = sum(s["failed"] for s in all_stats)

    # Write alive marker
    alive_path = LOG_DIR / "cron_alive.log"
    with open(alive_path, "a") as af:
        af.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} OK\n")

    log.info("Sync end | total_ok=%d total_failed=%d", total_ok, total_fail)


if __name__ == "__main__":
    main()
