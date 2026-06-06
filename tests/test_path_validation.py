from pathlib import Path

import pytest

from security import validate_allowed_path


def test_allowed_path_inside_root(tmp_path):
    root = tmp_path / "repo"
    child = root / "file.txt"
    root.mkdir()
    child.write_text("x")

    assert validate_allowed_path(str(child), [str(root)]) == child.resolve()


def test_disallowed_path_outside_root(tmp_path):
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    with pytest.raises(ValueError):
        validate_allowed_path(str(outside), [str(root)])


def test_path_traversal_cannot_escape_allowed_root(tmp_path):
    root = tmp_path / "repo"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_text("secret")

    escape = root / ".." / "outside.txt"
    with pytest.raises(ValueError):
        validate_allowed_path(str(escape), [str(root)])
