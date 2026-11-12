"""Delivery ledger, JSONL import, git tag allowlist, and evidence citation."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pulse.ai_copilot import draft_dispute_evidence
from pulse.cli import app
from pulse.config import PulseSettings, get_settings
from pulse.constants import EX_USAGE
from pulse.store import PulseStore, parse_delivery_jsonl, recent_commits

runner = CliRunner()


def _store(tmp_path) -> PulseStore:
    return PulseStore(tmp_path / "deliveries.db")


def test_record_and_list_deliveries(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_delivery("abc123def456", release_version="v1.0.0", channel="github")
    store.record_delivery("bbb222ccc111", release_version="v1.1.0", channel="ci")

    all_rows = store.get_deliveries()
    assert len(all_rows) == 2

    only_v100 = store.get_deliveries("v1.0.0")
    assert len(only_v100) == 1
    assert only_v100[0]["delivery_id"] == "DLV-ABC123DEF456"


def test_recording_the_same_commit_is_idempotent(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.record_delivery("abc123def456", release_version="v1.0.0", channel="manual")
    second = store.record_delivery("abc123def456", release_version="v1.0.0", channel="github")
    assert first["delivery_id"] == second["delivery_id"]
    assert len(store.get_deliveries()) == 1
    assert store.get_deliveries()[0]["channel"] == "github"


def test_delivery_requires_a_commit_sha(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.record_delivery("   ")


def test_jsonl_import_reports_each_bad_line() -> None:
    text = (
        '{"commit_sha":"aaa111","release_version":"v1.0.0"}\n'
        '{"commit_sha":""}\n'
        "not json\n"
        '{"commit_sha":"ccc333","colour":"red"}\n'
    )
    records, errors = parse_delivery_jsonl(text)
    assert [r["commit_sha"] for r in records] == ["aaa111"]
    assert len(errors) == 3
    assert "commit_sha is required" in errors[0]
    assert "invalid JSON" in errors[1]
    assert "unknown keys" in errors[2]


def test_tag_allowlist_rejects_injection_without_running_git() -> None:
    assert recent_commits("v1.0.0; rm -rf /") == []
    assert recent_commits("-x") == []
    assert recent_commits("") == []
    assert recent_commits("v1.0.0", limit=0) == []


def test_cli_deliver_and_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path))
    res = runner.invoke(app, ["deliver", "abc123def456789", "--release", "v1.0.0", "--json"])
    assert res.exit_code == 0
    record = json.loads(res.stdout)
    assert record["delivery_id"] == "DLV-ABC123DEF456"

    listing = runner.invoke(app, ["deliveries", "--json"])
    assert listing.exit_code == 0
    rows = json.loads(listing.stdout)
    assert len(rows) == 1
    assert rows[0]["commit_sha"] == "abc123def456789"


def test_cli_deliveries_import(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path))
    fixture = tmp_path / "deliveries.jsonl"
    fixture.write_text(
        '{"commit_sha":"aaa111","release_version":"v1.0.0"}\n{"commit_sha":""}\n',
        encoding="utf-8",
    )
    res = runner.invoke(app, ["deliveries", str(fixture), "--json"])
    assert res.exit_code == EX_USAGE
    payload = json.loads(res.stdout)
    assert len(payload["imported"]) == 1
    assert len(payload["errors"]) == 1


def test_evidence_cites_a_recorded_delivery(tmp_path, monkeypatch) -> None:
    """A delivery row must reach the evidence draft, not just the ledger."""
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path))
    settings = get_settings()
    PulseStore(settings.db_path, settings=settings).record_delivery(
        "abc123def456", release_version="v1.2.1", channel="github"
    )
    dispute = {
        "dispute_id": "PP-D-TEST-DELIVERY",
        "paypal_transaction_id": "TX-1",
        "buyer_email": "buyer@example.com",
        "buyer_country": "US",
        "platform_sku": "License",
        "release_version": "v1.2.1",
        "amount": 29.99,
        "currency": "USD",
        "reason": "Test reason",
        "description": "Test note",
    }
    evidence, model = draft_dispute_evidence(
        dispute, settings=PulseSettings(groq_api_key="", cache_dir=tmp_path)
    )
    assert model == "local-template"
    assert "DLV-ABC123DEF456" in evidence
    assert "Delivery Proof" in evidence