import json
import threading

import pytest

from sync_common import Config, log_upload_failure


@pytest.fixture
def cfg(tmp_path):
    return Config(
        base_dir=tmp_path / "ftp",
        bucket="my-bucket",
        prefix="cam",
        log_dir=tmp_path / "logs",
        project="binalapse-cameras",
        min_age_sec=120,
        max_retries=3,
        timeout_sec=480,
        max_process_workers=None,
        max_thread_workers=None,
        failure_log=tmp_path / "logs" / "upload_failures.jsonl",
    )


def test_failure_log_schema(cfg):
    log_upload_failure(
        cfg,
        script="retry_failed",
        camera="cam042",
        file_path="2026/06/25/photo_143000.jpg",
        reason="exhausted 3 retries: network error",
        attempts=3,
    )
    record = json.loads(cfg.failure_log.read_text(encoding="utf-8").strip())
    assert record["timestamp"].endswith("Z")
    assert record["level"] == "error"
    assert record["project"] == "binalapse-cameras"
    assert record["script"] == "retry_failed"
    assert record["s3_key"] == "cam/cam042/2026/06/25/photo_143000.jpg"
    assert record["bucket"] == "my-bucket"


def test_failure_log_defaults_to_upload_failed_event(cfg):
    log_upload_failure(
        cfg,
        script="camera_sync",
        camera="cam01",
        file_path="a.jpg",
        reason="boom",
        attempts=1,
    )
    record = json.loads(cfg.failure_log.read_text(encoding="utf-8").strip())
    assert record["event"] == "upload_failed"


def test_failure_log_file_rejected_event(cfg):
    log_upload_failure(
        cfg,
        script="camera_sync",
        camera="cam01",
        file_path="182.163.106.233/2026-7-5/sched/photo.jpg",
        reason="magic_bytes",
        attempts=0,
        event="file_rejected",
    )
    record = json.loads(cfg.failure_log.read_text(encoding="utf-8").strip())
    assert record["event"] == "file_rejected"
    assert record["reason"] == "magic_bytes"
    assert record["attempts"] == 0


def test_failure_log_sanitizes_multiline_reason(cfg):
    log_upload_failure(
        cfg,
        script="camera_sync",
        camera="cam01",
        file_path="a.jpg",
        reason="line1\nline2",
        attempts=1,
    )
    record = json.loads(cfg.failure_log.read_text(encoding="utf-8").strip())
    assert "\n" not in record["reason"]


def test_failure_log_concurrent_append(cfg):
    cfg.log_dir.mkdir(parents=True, exist_ok=True)

    def write(i):
        log_upload_failure(
            cfg,
            script="camera_sync",
            camera="cam01",
            file_path=f"{i}.jpg",
            reason="fail",
            attempts=1,
        )

    threads = [threading.Thread(target=write, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(cfg.failure_log.read_text(encoding="utf-8").strip().splitlines()) == 10
