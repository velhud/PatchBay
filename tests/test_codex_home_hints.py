"""Configuration hints must not disclose paths nested inside the user home."""
from pathlib import Path

import pytest

from patchbay.codex_home import codex_home_path_hint


@pytest.mark.parametrize("location", ["private/project-codex", "other", "outside"])
def test_custom_location_hint_hides_both_home_relative_and_external_paths(tmp_path, monkeypatch, location):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    directory = tmp_path / "external" if location == "outside" else home / location
    assert codex_home_path_hint(directory / "config.toml") == "configured_codex_home/config.toml"


def test_default_location_keeps_recognizable_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert codex_home_path_hint(tmp_path / ".codex/config.toml") == "~/.codex/config.toml"
