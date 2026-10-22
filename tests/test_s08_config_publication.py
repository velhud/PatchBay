"""S08 audit proposals adapted and executed as disposable regression fixtures."""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from patchbay.connector import profiles


def test_s08_failed_atomic_replace_preserves_previous_config(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "runtime.yaml"
    original = b"server:\n  port: 8000\n"
    destination.write_bytes(original)
    destination.chmod(0o600)
    observed = []

    def refuse_replace(source, target, *args, **kwargs):
        observed.append((Path(source), Path(target)))
        assert Path(target) == destination
        assert Path(source) != destination
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        raise OSError(errno.ENOSPC, "disposable publication fault")

    monkeypatch.setattr(profiles.os, "replace", refuse_replace)
    with pytest.raises(OSError) as failure:
        profiles._write_private_text(destination, "server:\n  port: 8001\n")
    assert failure.value.errno == errno.ENOSPC
    assert observed, "The proposed implementation must publish through the atomic seam"
    assert destination.read_bytes() == original
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_s08_private_config_writer_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    outside = tmp_path / "operator-owned.txt"
    outside.write_text("preserve me\n", encoding="utf-8")
    link = tmp_path / "runtime.yaml"
    link.symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        profiles._write_private_text(link, "replacement\n")
    assert link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "preserve me\n"


def test_s08_private_config_positive_roundtrip(tmp_path: Path) -> None:
    destination = tmp_path / "runtime.yaml"
    profiles._write_private_text(destination, "workspace: café\n")
    assert destination.read_bytes() == "workspace: café\n".encode("utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
