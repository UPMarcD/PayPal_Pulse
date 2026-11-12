"""Shared test isolation.

Every test runs against its own temporary ledger. Without this fixture, pytest
would read and write the developer's own .pulse_cache/pulse.db, so a green run
would depend on whatever data happened to be on that machine.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path_factory, monkeypatch: pytest.MonkeyPatch):
    """Point every test at a private cache directory."""
    cache = tmp_path_factory.mktemp("pulse-cache")
    monkeypatch.setenv("PULSE_CACHE_DIR", str(cache))
    monkeypatch.delenv("PULSE_DEMO_MODE", raising=False)
    yield cache