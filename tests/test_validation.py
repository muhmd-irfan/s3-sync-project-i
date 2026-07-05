from pathlib import Path

import pytest

from sync_common import (
    JPEG_MAGIC,
    PNG_MAGIC,
    is_eligible_file,
    load_config,
    validate_magic_bytes,
)


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("CAMERA_SYNC_BUCKET", "test-bucket")
    return load_config()


def test_validate_jpeg_magic(tmp_path):
    p = tmp_path / "photo.jpg"
    p.write_bytes(JPEG_MAGIC + b"\x00" * 5)
    assert validate_magic_bytes(p, ".jpg") is True


def test_validate_png_magic(tmp_path):
    p = tmp_path / "photo.png"
    p.write_bytes(PNG_MAGIC)
    assert validate_magic_bytes(p, ".png") is True


def test_reject_fake_jpg(tmp_path):
    p = tmp_path / "evil.jpg"
    p.write_bytes(b"not a jpeg at all")
    assert validate_magic_bytes(p, ".jpg") is False


def test_txt_skips_magic_check(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("any content")
    assert validate_magic_bytes(p, ".txt") is True


def test_is_eligible_rejects_fake_jpg(tmp_path, cfg):
    user_root = tmp_path / "cam01"
    user_root.mkdir()
    p = user_root / "evil.jpg"
    p.write_bytes(b"fake")
    ok, reason = is_eligible_file(p, user_root, 1_000_000.0, cfg, check_age=False)
    assert ok is False
    assert reason == "magic_bytes"


def test_is_eligible_accepts_real_jpeg(tmp_path, cfg):
    user_root = tmp_path / "cam01"
    user_root.mkdir()
    p = user_root / "real.jpg"
    p.write_bytes(JPEG_MAGIC + b"\xff" * 10)
    ok, reason = is_eligible_file(p, user_root, 1_000_000.0, cfg, check_age=False)
    assert ok is True
    assert reason is None
