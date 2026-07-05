import os
from pathlib import Path

import pytest

from sync_common import is_eligible_file, is_safe_relative_path, load_config


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("CAMERA_SYNC_BUCKET", "test-bucket")
    return load_config()


def test_is_safe_relative_path_rejects_dotdot():
    assert is_safe_relative_path(Path("..")) is False
    assert is_safe_relative_path(Path("a/../b.jpg")) is False
    assert is_safe_relative_path(Path("2026/06/photo.jpg")) is True


def test_is_eligible_rejects_symlink(tmp_path, cfg):
    user_root = tmp_path / "cam01"
    user_root.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("secret")
    link = user_root / "link.jpg"
    if os.name == "nt":
        pytest.skip("symlink creation may require admin on Windows")
    link.symlink_to(target)
    ok, reason = is_eligible_file(link, user_root, 1_000_000.0, cfg, check_age=False)
    assert ok is False
    assert reason == "symlink"


def test_is_eligible_rejects_unsafe_path_component(tmp_path, cfg):
    user_root = tmp_path / "cam01"
    unsafe = user_root / ".." / "outside.jpg"
    unsafe.parent.mkdir(parents=True, exist_ok=True)
    unsafe.write_bytes(b"\xff\xd8\xff\x00")
    ok, reason = is_eligible_file(unsafe, user_root, 1_000_000.0, cfg, check_age=False)
    assert ok is False
    assert reason in ("unsafe_path", "outside_root")
