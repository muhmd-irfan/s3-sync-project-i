import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sync_common import (
    Config,
    log_upload_failure,
    upload_and_verify,
    upload_with_retries,
)


@pytest.fixture
def cfg(tmp_path):
    return Config(
        base_dir=tmp_path / "ftp",
        bucket="test-bucket",
        prefix="cam",
        log_dir=tmp_path / "logs",
        project="test-project",
        min_age_sec=120,
        max_retries=3,
        timeout_sec=480,
        max_process_workers=None,
        max_thread_workers=None,
        failure_log=tmp_path / "logs" / "upload_failures.jsonl",
    )


def test_upload_with_retries_succeeds_on_second_attempt(cfg, tmp_path):
    local = tmp_path / "photo.jpg"
    local.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

    client = MagicMock()
    etag = "d41d8cd98f00b204e9800998ecf8427e"
    head = {"ContentLength": len(local.read_bytes()), "ETag": f'"{etag}"'}

    call_count = {"n": 0}
    real_md5 = __import__("hashlib").md5(local.read_bytes()).hexdigest()

    def side_effect(*_args, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "500"}}, "upload_fileobj")

    client.upload_fileobj.side_effect = side_effect
    client.head_object.return_value = {
        "ContentLength": local.stat().st_size,
        "ETag": f'"{real_md5}"',
    }

    with patch("sync_common.HashingReader") as mock_reader_cls:
        mock_reader = MagicMock()
        mock_reader.__enter__ = MagicMock(return_value=mock_reader)
        mock_reader.__exit__ = MagicMock(return_value=False)
        mock_reader.hexdigest.return_value = real_md5
        mock_reader_cls.return_value = mock_reader

        ok, msg, _, _, attempts = upload_with_retries(
            client, cfg, "cam/u/f.jpg", local,
        )

    assert ok is True
    assert attempts == 2


def test_upload_and_verify_size_mismatch(cfg, tmp_path):
    local = tmp_path / "photo.jpg"
    local.write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)

    client = MagicMock()
    client.upload_fileobj.return_value = None
    client.head_object.return_value = {
        "ContentLength": 9999,
        "ETag": '"abc"',
    }

    ok, msg, _, _ = upload_and_verify(client, cfg, "key", local)
    assert ok is False
    assert "Size mismatch" in msg


def test_log_upload_failure_writes_json(cfg):
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    log_upload_failure(
        cfg,
        script="camera_sync",
        camera="cam042",
        file_path="2026/06/25/photo.jpg",
        reason="Upload failed: timeout",
        attempts=3,
    )
    lines = cfg.failure_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "upload_failed"
    assert record["project"] == "test-project"
    assert record["camera"] == "cam042"
    assert record["file_path"] == "2026/06/25/photo.jpg"
    assert record["script"] == "camera_sync"
    assert record["attempts"] == 3
    assert "timeout" in record["reason"]


def test_log_upload_failure_thread_safe(cfg):
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    errors = []

    def worker(i):
        try:
            log_upload_failure(
                cfg,
                script="camera_sync",
                camera=f"cam{i:03d}",
                file_path=f"f{i}.jpg",
                reason=f"err{i}",
                attempts=1,
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    lines = cfg.failure_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 20
