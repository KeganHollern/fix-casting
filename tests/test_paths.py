"""Persistent install-path behavior."""

from pathlib import Path

from cast_tab.paths import user_data_dir


def test_absolute_xdg_data_home_is_used(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert user_data_dir() == tmp_path / "fix-casting"


def test_relative_xdg_data_home_does_not_depend_on_working_directory(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data")
    assert user_data_dir() == Path.home() / ".local" / "share" / "fix-casting"
