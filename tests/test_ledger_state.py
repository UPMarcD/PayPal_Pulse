"""Prove the ledger state machine: fresh, sample, and connected.

A stranger who runs `pp pulse` must see a first-run card, never fabricated
revenue. These tests pin the predicate that decides which one they see.
"""

from __future__ import annotations

import json
from pathlib import Path

from pulse.config import PulseSettings
from pulse.paypal_client import PayPalOrderResult
from pulse.store import PulseStore


def _store(tmp_path: Path) -> PulseStore:
    settings = PulseSettings(cache_dir=tmp_path)
    return PulseStore(settings.db_path, settings=settings)


def _order(order_id: str, amount: float = 5.99) -> PayPalOrderResult:
    return PayPalOrderResult(
        success=True,
        order_id=order_id,
        status="COMPLETED",
        amount=amount,
        currency="USD",
    )


def test_empty_ledger_is_fresh(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.detect_state() == "fresh"
    assert store.should_auto_seed() is False
    assert store.next_actions()
    summary = store.get_dashboard_summary()
    assert summary["state"] == "fresh"
    assert summary["daily_14"] == []
    assert summary["total_gross_usd"] == 0.0
    assert summary["health_score"] == 0.0
    assert summary["health_rating"] == "NO DATA"
    assert summary["health_telemetry"] == {}


def test_seeded_ledger_is_sample(tmp_path) -> None:
    store = _store(tmp_path)
    store.seed_telemetry()
    assert store.detect_state() == "sample"
    assert store.next_actions() == ["Run `pp reset` to clear sample data before recording real orders."]


def test_real_order_is_connected(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_live_order(_order("ORDER-1"))
    assert store.detect_state() == "connected"
    assert store.next_actions() == []


def test_reset_clears_the_sample_marker(tmp_path) -> None:
    """A seeded ledger that is wiped and then given a real order must not read as sample."""
    store = _store(tmp_path)
    store.seed_telemetry()
    store.reset(mark_clean=True)
    assert store.detect_state() == "fresh"
    store.record_live_order(_order("ORDER-2"))
    assert store.detect_state() == "connected"


def test_ask_reports_no_data_instead_of_zero_analysis(tmp_path, monkeypatch) -> None:
    """A fresh ledger must not be analysed as if $0.00 were a real result."""
    from typer.testing import CliRunner

    from pulse.cli import app

    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path))
    result = CliRunner().invoke(app, ["ask", "What is our net margin?", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["state"] == "fresh"
    assert "no orders" in payload["answer"].lower()


def test_demo_database_still_auto_seeds(tmp_path) -> None:
    settings = PulseSettings(cache_dir=tmp_path, demo_mode=True)
    store = PulseStore(settings.db_path, settings=settings)
    assert store.should_auto_seed() is True
    assert store.detect_state() == "fresh"
    store.seed_telemetry()
    assert store.detect_state() == "sample"