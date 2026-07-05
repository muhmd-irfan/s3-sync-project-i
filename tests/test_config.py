import os
from pathlib import Path

import pytest

from sync_common import Config, ConfigError, load_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CAMERA_SYNC_"):
            monkeypatch.delenv(key, raising=False)


def test_load_config_requires_bucket():
    with pytest.raises(ConfigError, match="CAMERA_SYNC_BUCKET"):
        load_config()


def test_load_config_rejects_placeholder(monkeypatch):
    monkeypatch.setenv("CAMERA_SYNC_BUCKET", "<S3-BUCKET-NAME>")
    with pytest.raises(ConfigError, match="CAMERA_SYNC_BUCKET"):
        load_config()


def test_load_config_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMERA_SYNC_BUCKET", "my-bucket")
    monkeypatch.setenv("CAMERA_SYNC_LOG_DIR", str(tmp_path / "logs"))
    cfg = load_config()
    assert cfg.bucket == "my-bucket"
    assert cfg.prefix == "cam"
    assert cfg.project == "camera-sync"
    assert cfg.min_age_sec == 120
    assert cfg.max_retries == 3
    assert cfg.timeout_sec == 480
    assert cfg.failure_log == tmp_path / "logs" / "upload_failures.jsonl"


def test_load_config_project_override(monkeypatch):
    monkeypatch.setenv("CAMERA_SYNC_BUCKET", "b")
    monkeypatch.setenv("CAMERA_SYNC_PROJECT", "binalapse-cameras")
    cfg = load_config()
    assert cfg.project == "binalapse-cameras"


def test_config_is_frozen(monkeypatch):
    monkeypatch.setenv("CAMERA_SYNC_BUCKET", "b")
    cfg = load_config()
    with pytest.raises(Exception):
        cfg.bucket = "other"  # type: ignore[misc]
