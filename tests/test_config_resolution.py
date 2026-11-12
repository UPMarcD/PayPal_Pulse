"""Pin the ledger directory resolution order.

The order is explicit argument, then PULSE_CACHE_DIR, then the nearest ancestor
holding .pulse.json or .git, then ~/.pulse/cache. Each test below isolates one
step so a resolver that skips a step fails here instead of failing silently in
production.
"""

from __future__ import annotations

import os
from pathlib import Path

import pulse.config as config


def _clear_env(monkeypatch) -> None:
    """Remove PULSE_CACHE_DIR so the test controls the next resolution step."""
    monkeypatch.delenv("PULSE_CACHE_DIR", raising=False)


def test_explicit_argument_beats_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path / "env"))
    resolved = config.resolve_cache_dir(tmp_path / "explicit")
    assert resolved == tmp_path / "explicit"


def test_environment_beats_project_marker(tmp_path, monkeypatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".pulse.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path / "env"))
    monkeypatch.chdir(project)
    resolved = config.resolve_cache_dir()
    assert resolved == tmp_path / "env"


def test_pulse_json_ancestor_is_used(tmp_path, monkeypatch) -> None:
    project = tmp_path / "proj"
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    (project / ".pulse.json").write_text("{}", encoding="utf-8")
    _clear_env(monkeypatch)
    monkeypatch.chdir(nested)
    resolved = config.resolve_cache_dir()
    assert resolved == project.resolve() / ".pulse_cache"


def test_git_ancestor_is_used(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    nested = repo / "pkg"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    _clear_env(monkeypatch)
    monkeypatch.chdir(nested)
    resolved = config.resolve_cache_dir()
    assert resolved == repo.resolve() / ".pulse_cache"


def test_home_cache_is_the_fallback(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    _clear_env(monkeypatch)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", home)
    monkeypatch.chdir(orphan)
    resolved = config.resolve_cache_dir()
    assert resolved == home / "cache"


def test_field_default_factory_uses_the_same_order(tmp_path, monkeypatch) -> None:
    """A bare PulseSettings() must resolve exactly like resolve_cache_dir()."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".pulse.json").write_text("{}", encoding="utf-8")
    _clear_env(monkeypatch)
    monkeypatch.chdir(project)
    settings = config.PulseSettings()
    assert Path(settings.cache_dir) == project.resolve() / ".pulse_cache"


def test_get_settings_honours_explicit_cache_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path / "env"))
    settings = config.get_settings(cache_dir=tmp_path / "explicit")
    assert settings.cache_dir == tmp_path / "explicit"


def test_parent_env_is_not_loaded(tmp_path, monkeypatch) -> None:
    """A .env above the working directory must not leak into the process."""
    parent_env = tmp_path / ".env"
    parent_env.write_text("PULSE_TEST_MARKER=leaked\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    monkeypatch.delenv("PULSE_TEST_MARKER", raising=False)
    assert config._load_env_files() is None
    assert "PULSE_TEST_MARKER" not in os.environ
