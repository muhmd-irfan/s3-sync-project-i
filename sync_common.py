"""Shared utilities for camera_sync.py and retry_failed.py."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO, List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

ELIGIBLE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".txt"})
INTERNAL_DIRS = frozenset({".spool", "failed"})

LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_CNT = 5

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_failure_log_lock = threading.Lock()


@dataclass(frozen=True)
class Config:
    base_dir: Path
    bucket: str
    prefix: str
    log_dir: Path
    project: str
    min_age_sec: int
    max_retries: int
    timeout_sec: int
    max_process_workers: Optional[int]
    max_thread_workers: Optional[int]
    failure_log: Path
    eligible_exts: frozenset[str] = ELIGIBLE_EXTS
    internal_dirs: frozenset[str] = INTERNAL_DIRS
    log_max_bytes: int = LOG_MAX_BYTES
    log_backup_cnt: int = LOG_BACKUP_CNT


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def _env_str(name: str, default: str) -> str:
    val = os.environ.get(name)
    return default if val is None or val.strip() == "" else val.strip()


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return int(val)


def _env_opt_int(name: str) -> Optional[int]:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return None
    return int(val)


def load_config() -> Config:
    """Load configuration from environment variables."""
    log_dir = Path(_env_str("CAMERA_SYNC_LOG_DIR", "/var/log/camera"))
    bucket = _env_str("CAMERA_SYNC_BUCKET", "")
    if not bucket or bucket == "<S3-BUCKET-NAME>":
        raise ConfigError(
            "CAMERA_SYNC_BUCKET must be set to a real S3 bucket name "
            "(not empty or the placeholder <S3-BUCKET-NAME>)"
        )

    failure_log_default = log_dir / "upload_failures.jsonl"
    failure_log = Path(
        _env_str("CAMERA_SYNC_FAILURE_LOG", str(failure_log_default))
    )

    return Config(
        base_dir=Path(_env_str("CAMERA_SYNC_BASE_DIR", "/var/ftp/local")),
        bucket=bucket,
        prefix=_env_str("CAMERA_SYNC_PREFIX", "cam"),
        log_dir=log_dir,
        project=_env_str("CAMERA_SYNC_PROJECT", "camera-sync"),
        min_age_sec=_env_int("CAMERA_SYNC_MIN_AGE_SEC", 120),
        max_retries=_env_int("CAMERA_SYNC_MAX_RETRIES", 3),
        timeout_sec=_env_int("CAMERA_SYNC_TIMEOUT_SEC", 480),
        max_process_workers=_env_opt_int("CAMERA_SYNC_MAX_PROCESS_WORKERS"),
        max_thread_workers=_env_opt_int("CAMERA_SYNC_MAX_THREAD_WORKERS"),
        failure_log=failure_log,
    )


def make_s3_key(cfg: Config, camera: str, file_path: str) -> str:
    """Build the S3 object key for a camera file."""
    normalized = file_path.replace("\\", "/").lstrip("/")
    return f"{cfg.prefix}/{camera}/{normalized}"


def build_logger(
    cfg: Config,
    name: str,
    main_log: str,
    error_log: str,
) -> logging.Logger:
    """Return a logger with rotating main, error, and console handlers."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(processName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg.log_dir.mkdir(parents=True, exist_ok=True)

    fh = RotatingFileHandler(
        cfg.log_dir / main_log,
        maxBytes=cfg.log_max_bytes,
        backupCount=cfg.log_backup_cnt,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    eh = RotatingFileHandler(
        cfg.log_dir / error_log,
        maxBytes=cfg.log_max_bytes,
        backupCount=cfg.log_backup_cnt,
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


def pool_sizes(cfg: Config, user_count: int, file_count: int) -> Tuple[int, int]:
    """Pick process and thread pool sizes dynamically."""
    cpus = os.cpu_count() or 4

    if cfg.max_process_workers is not None:
        proc_w = cfg.max_process_workers
    else:
        proc_w = min(user_count, cpus)

    if cfg.max_thread_workers is not None:
        thr_w = cfg.max_thread_workers
    else:
        per_proc = max(1, file_count // max(proc_w, 1))
        thr_w = min(per_proc, 20)
        thr_w = max(thr_w, 4)

    return max(proc_w, 1), max(thr_w, 1)


def atomic_move(src: Path, dst: Path) -> None:
    """Move *src* to *dst* atomically (same filesystem) or with copy+delete."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)
    except OSError:
        shutil.move(str(src), str(dst))


def prune_empty_dirs(root: Path) -> None:
    """Delete empty sub-directories under *root*, bottom-up."""
    for dirpath, _, _ in os.walk(str(root), topdown=False):
        dp = Path(dirpath)
        if dp == root:
            continue
        try:
            dp.rmdir()
        except OSError:
            pass


def is_safe_relative_path(rel: Path) -> bool:
    """Return True if *rel* has no traversal or empty path components."""
    if not rel.parts:
        return False
    return ".." not in rel.parts and not any(p == "" for p in rel.parts)


def path_under_root(path: Path, root: Path) -> bool:
    """Return True if *path* resolves to a location under *root*."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def validate_magic_bytes(path: Path, ext: str) -> bool:
    """Return True if file content matches the expected type for *ext*."""
    if ext == ".txt":
        return True
    try:
        with open(path, "rb") as f:
            header = f.read(8)
    except OSError:
        return False

    if ext in (".jpg", ".jpeg"):
        return header[:3] == JPEG_MAGIC
    if ext == ".png":
        return header[:8] == PNG_MAGIC
    return False


def is_eligible_file(
    path: Path,
    user_root: Path,
    now: float,
    cfg: Config,
    *,
    check_age: bool = True,
) -> Tuple[bool, Optional[str]]:
    """Return (eligible, skip_reason) for a candidate file under *user_root*."""
    if path.is_symlink():
        return False, "symlink"

    if not path.is_file():
        return False, "not_a_file"

    try:
        rel = path.relative_to(user_root)
    except ValueError:
        return False, "outside_root"

    if not is_safe_relative_path(rel):
        return False, "unsafe_path"

    if not path_under_root(path, user_root):
        return False, "outside_root"

    top_dir = rel.parts[0] if rel.parts else ""
    if top_dir in cfg.internal_dirs:
        return False, "internal_dir"

    ext = path.suffix.lower()
    if ext not in cfg.eligible_exts:
        return False, "extension"

    if check_age:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            return False, "stat_error"
        if age < cfg.min_age_sec:
            return False, "too_young"

    if not validate_magic_bytes(path, ext):
        return False, "magic_bytes"

    return True, None


def md5_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Return the hex MD5 digest of a file (matches single-part S3 ETag)."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


class HashingReader:
    """File-like reader that computes MD5 while streaming."""

    def __init__(self, path: Path):
        self._path = path
        self._f: BinaryIO = open(path, "rb")
        self._h = hashlib.md5()
        self.size = path.stat().st_size

    def read(self, n: int = -1) -> bytes:
        chunk = self._f.read(n)
        if chunk:
            self._h.update(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self._h.hexdigest()

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "HashingReader":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def upload_and_verify(
    s3_client,
    cfg: Config,
    s3_key: str,
    local_path: Path,
) -> Tuple[bool, str, float, float]:
    """Upload a file to S3 in one read pass and verify size + MD5.

    Returns (success, message, upload_seconds, verify_seconds).
    """
    try:
        local_size = local_path.stat().st_size
    except OSError as exc:
        return False, f"Local stat failed: {exc}", 0.0, 0.0

    t_upload0 = time.perf_counter()
    try:
        with HashingReader(local_path) as reader:
            s3_client.upload_fileobj(reader, cfg.bucket, s3_key)
            local_md5 = reader.hexdigest()
    except (BotoCoreError, ClientError, OSError) as exc:
        upload_sec = time.perf_counter() - t_upload0
        return False, f"Upload failed: {exc}", upload_sec, 0.0
    upload_sec = time.perf_counter() - t_upload0

    t_verify0 = time.perf_counter()
    try:
        head = s3_client.head_object(Bucket=cfg.bucket, Key=s3_key)
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


def upload_with_retries(
    s3_client,
    cfg: Config,
    s3_key: str,
    local_path: Path,
) -> Tuple[bool, str, float, float, int]:
    """Upload with retries. Returns (ok, message, upload_s, verify_s, attempts)."""
    last_msg = ""
    total_up = 0.0
    total_ver = 0.0
    for attempt in range(1, cfg.max_retries + 1):
        ok, msg, up_s, ver_s = upload_and_verify(
            s3_client, cfg, s3_key, local_path,
        )
        total_up += up_s
        total_ver += ver_s
        if ok:
            return True, msg, total_up, total_ver, attempt
        last_msg = msg
    return False, last_msg, total_up, total_ver, cfg.max_retries


def diagnose_s3_object(
    s3_client,
    cfg: Config,
    s3_key: str,
    local_path: Path,
) -> str:
    """Return a reason string for a file sitting in failed/."""
    try:
        local_size = local_path.stat().st_size
        local_md5 = md5_file(local_path)
    except OSError as exc:
        return f"local_unreadable: {exc}"

    try:
        head = s3_client.head_object(Bucket=cfg.bucket, Key=s3_key)
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


def _rotate_log_file(path: Path, max_bytes: int, backup_count: int) -> None:
    """Rotate *path* when it exceeds *max_bytes* (same semantics as RotatingFileHandler)."""
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    for i in range(backup_count, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        dst = path.with_name(f"{path.name}.{i + 1}")
        if i == backup_count and dst.exists():
            dst.unlink()
        if src.exists():
            if i == backup_count:
                src.unlink()
            else:
                src.rename(dst)
    path.rename(path.with_name(f"{path.name}.1"))


def log_upload_failure(
    cfg: Config,
    *,
    script: str,
    camera: str,
    file_path: str,
    reason: str,
    attempts: int = 1,
    s3_key: Optional[str] = None,
    error_logger: Optional[logging.Logger] = None,
) -> None:
    """Append one JSON line to upload_failures.jsonl for Grafana/Loki."""
    normalized_path = file_path.replace("\\", "/").lstrip("/")
    key = s3_key or make_s3_key(cfg, camera, normalized_path)
    clean_reason = reason.replace("\n", " ").replace("\r", " ")

    record = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "level": "error",
        "event": "upload_failed",
        "project": cfg.project,
        "script": script,
        "camera": camera,
        "file_path": normalized_path,
        "s3_key": key,
        "bucket": cfg.bucket,
        "reason": clean_reason,
        "attempts": attempts,
    }

    line = json.dumps(record, separators=(",", ":")) + "\n"

    with _failure_log_lock:
        cfg.failure_log.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log_file(cfg.failure_log, cfg.log_max_bytes, cfg.log_backup_cnt)
        with open(cfg.failure_log, "a", encoding="utf-8") as f:
            f.write(line)

    if error_logger is not None:
        error_logger.error(
            "upload_failed | project=%s camera=%s file=%s reason=%s",
            cfg.project,
            camera,
            normalized_path,
            clean_reason,
        )


_alive_lock = threading.Lock()
_alive_bytes_written = 0


def write_alive_marker(cfg: Config) -> None:
    """Write a one-line heartbeat with size-based rotation."""
    global _alive_bytes_written
    alive_path = cfg.log_dir / "cron_alive.log"
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} OK\n"

    with _alive_lock:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        _rotate_log_file(alive_path, cfg.log_max_bytes, cfg.log_backup_cnt)
        if alive_path.exists():
            _alive_bytes_written = alive_path.stat().st_size
        with open(alive_path, "a", encoding="utf-8") as f:
            f.write(line)
            _alive_bytes_written += len(line.encode("utf-8"))


class FileLock:
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
