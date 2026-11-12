"""Tests for PayPal telemetry synchronization, sync CLI command, and refresh capabilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from pulse.cli import app
from pulse.config import get_settings
from pulse.paypal_client import PayPalClient, PayPalOrderResult
from pulse.store import PulseStore

runner = CliRunner()


def test_sync_external_transactions_ingestion(tmp_path: Path) -> None:
    """Verify sync_external_transactions parses PayPal Reporting API details into the local SQLite ledger."""
    db_file = tmp_path / "sync_test.db"
    store = PulseStore(db_path=db_file)

    sample_tx_details = [
        {
            "transaction_info": {
                "transaction_id": "PP-TX-SYNC-001",
                "transaction_initiation_date": "2026-11-10T14:30:00Z",
                "transaction_amount": {
                    "currency_code": "USD",
                    "value": "29.99",
                },
                "fee_amount": {
                    "currency_code": "USD",
                    "value": "-1.47",
                },
                "transaction_status": "S",
                "custom_field": "release:v1.2.0|US|PulseCore Pro",
            },
            "payer_info": {
                "country_code": "US",
                "email_address": "buyer@example.com",
            },
            "cart_info": {
                "item_details": [
                    {
                        "item_name": "PulseCore Pro License",
                        "item_sku": "PulseCore Pro",
                    }
                ]
            },
        },
        {
            "transaction_info": {
                "transaction_id": "PP-TX-SYNC-002",
                "transaction_initiation_date": "2026-11-10T16:00:00Z",
                "transaction_amount": {
                    "currency_code": "EUR",
                    "value": "15.00",
                },
                "fee_amount": {
                    "currency_code": "EUR",
                    "value": "-1.10",
                },
                "transaction_status": "S",
                "custom_field": "release:v1.2.1|DE|PulseCore Pro",
            },
            "payer_info": {
                "country_code": "DE",
            },
        },
    ]

    added = store.sync_external_transactions(sample_tx_details)
    assert added == 2

    # Second sync of identical transactions is idempotent (0 added)
    re_added = store.sync_external_transactions(sample_tx_details)
    assert re_added == 0

    txs = store.get_transactions()
    assert len(txs) == 2
    tx1 = next(t for t in txs if t["paypal_id"] == "PP-TX-SYNC-001")
    assert tx1["gross_amount"] == 29.99
    assert tx1["fee_amount"] == 1.47
    assert tx1["net_amount"] == 28.52
    assert tx1["currency"] == "USD"
    assert tx1["release_version"] == "v1.2.0"
    assert tx1["country_code"] == "US"
    assert tx1["status"] == "COMPLETED"


def test_sync_external_transactions_refund_and_denied(tmp_path: Path) -> None:
    """Verify refunds reduce revenue and denied/fee rows are not counted as sales."""
    db_file = tmp_path / "refund_test.db"
    store = PulseStore(db_path=db_file)

    details = [
        {
            "transaction_info": {
                "transaction_id": "PP-SALE-1",
                "transaction_initiation_date": "2026-11-10T14:30:00Z",
                "transaction_amount": {"currency_code": "USD", "value": "100.00"},
                "fee_amount": {"currency_code": "USD", "value": "-4.99"},
                "transaction_status": "S",
                "transaction_event_code": "T0006",
            },
            "payer_info": {"country_code": "US"},
        },
        {
            "transaction_info": {
                "transaction_id": "PP-REFUND-1",
                "transaction_initiation_date": "2026-11-11T14:30:00Z",
                "transaction_amount": {"currency_code": "USD", "value": "-100.00"},
                "fee_amount": {"currency_code": "USD", "value": "4.99"},
                "transaction_status": "S",
                "transaction_event_code": "T1107",
            },
            "payer_info": {"country_code": "US"},
        },
        {
            "transaction_info": {
                "transaction_id": "PP-DENIED-1",
                "transaction_initiation_date": "2026-11-12T14:30:00Z",
                "transaction_amount": {"currency_code": "USD", "value": "50.00"},
                "fee_amount": {"currency_code": "USD", "value": "0"},
                "transaction_status": "D",
                "transaction_event_code": "T0006",
            },
            "payer_info": {"country_code": "US"},
        },
        {
            "transaction_info": {
                "transaction_id": "PP-FEE-1",
                "transaction_initiation_date": "2026-11-12T15:00:00Z",
                "transaction_amount": {"currency_code": "USD", "value": "-1.00"},
                "fee_amount": {"currency_code": "USD", "value": "0"},
                "transaction_status": "S",
                "transaction_event_code": "T0107",
            },
            "payer_info": {"country_code": "US"},
        },
    ]

    added = store.sync_external_transactions(details)
    assert added == 3  # the fee row is skipped

    txs = {t["paypal_id"]: t for t in store.get_transactions()}
    assert txs["PP-SALE-1"]["status"] == "COMPLETED"
    assert txs["PP-SALE-1"]["gross_amount"] == 100.00
    assert txs["PP-SALE-1"]["net_amount"] == 95.01
    assert txs["PP-REFUND-1"]["status"] == "REFUNDED"
    assert txs["PP-REFUND-1"]["gross_amount"] == -100.00
    assert txs["PP-REFUND-1"]["net_amount"] == -95.01
    assert txs["PP-DENIED-1"]["status"] == "DENIED"

    summary = store.get_dashboard_summary()
    # One paid order, fully refunded: revenue nets to zero.
    assert summary["total_orders"] == 1
    assert summary["total_gross_usd"] == 0.0


def test_sync_same_id_reversal_voids_sale(tmp_path: Path) -> None:
    """Verify a same-ID reversal cannot keep the original positive revenue."""
    store = PulseStore(db_path=tmp_path / "same_id.db")

    def row(tx_id: str, value: str, status: str, event: str) -> dict:
        return {
            "transaction_info": {
                "transaction_id": tx_id,
                "transaction_initiation_date": "2026-11-10T14:30:00Z",
                "transaction_amount": {"currency_code": "USD", "value": value},
                "fee_amount": {"currency_code": "USD", "value": "-4.99"},
                "transaction_status": status,
                "transaction_event_code": event,
            },
            "payer_info": {"country_code": "US"},
        }

    store.sync_external_transactions([row("PP-DUP-1", "100.00", "S", "T0006")])
    store.sync_external_transactions([row("PP-DUP-1", "-100.00", "V", "T1106")])

    txs = store.get_transactions()
    assert len(txs) == 1
    assert txs[0]["status"] == "REVERSED"
    assert txs[0]["gross_amount"] == 0.0
    assert store.get_dashboard_summary()["total_gross_usd"] == 0.0


def test_sync_skips_credit_card_deposit(tmp_path: Path) -> None:
    """Verify a T0700 credit card deposit is not recorded as merchant revenue."""
    store = PulseStore(db_path=tmp_path / "deposit.db")
    deposit = {
        "transaction_info": {
            "transaction_id": "PP-DEP-1",
            "transaction_initiation_date": "2026-11-10T14:30:00Z",
            "transaction_amount": {"currency_code": "USD", "value": "5000.00"},
            "fee_amount": {"currency_code": "USD", "value": "0"},
            "transaction_status": "S",
            "transaction_event_code": "T0700",
        },
        "payer_info": {"country_code": "US"},
    }
    assert store.sync_external_transactions([deposit]) == 0
    assert store.get_transactions() == []


def test_sync_matches_local_order_by_reference_id(tmp_path: Path) -> None:
    """Verify a reporting capture updates the local order instead of duplicating it."""
    store = PulseStore(db_path=tmp_path / "ref_match.db")
    order = PayPalOrderResult(
        success=True,
        order_id="ORD-1",
        status="COMPLETED",
        amount=100.00,
        currency="USD",
        description="Local order",
    )
    store.record_live_order(order)

    reporting = {
        "transaction_info": {
            "transaction_id": "CAP-1",
            "paypal_reference_id": "ORD-1",
            "paypal_reference_id_type": "ODR",
            "transaction_initiation_date": "2026-11-10T14:30:00Z",
            "transaction_amount": {"currency_code": "USD", "value": "100.00"},
            "fee_amount": {"currency_code": "USD", "value": "-4.99"},
            "transaction_status": "S",
            "transaction_event_code": "T0006",
        },
        "payer_info": {"country_code": "US"},
    }
    store.sync_external_transactions([reporting])

    txs = store.get_transactions()
    assert len(txs) == 1
    assert txs[0]["paypal_id"] == "ORD-1"
    summary = store.get_dashboard_summary()
    assert summary["total_gross_usd"] == 100.0
    assert summary["total_orders"] == 1


def test_record_live_order_capture_applies_rollup(tmp_path: Path) -> None:
    """Verify a pending order that later captures adds revenue once."""
    store = PulseStore(db_path=tmp_path / "capture.db")
    pending = PayPalOrderResult(
        success=True,
        order_id="ORD-CAP",
        status="CREATED",
        amount=50.00,
        currency="USD",
        description="Pending order",
    )
    store.record_live_order(pending)
    assert store.get_dashboard_summary()["total_gross_usd"] == 0.0

    captured = PayPalOrderResult(
        success=True,
        order_id="ORD-CAP",
        status="COMPLETED",
        amount=50.00,
        currency="USD",
        description="Captured order",
    )
    store.record_live_order(captured)

    summary = store.get_dashboard_summary()
    assert summary["total_gross_usd"] == 50.0
    assert summary["total_orders"] == 1


def test_sync_allows_only_payment_and_refund_codes(tmp_path: Path) -> None:
    """Verify holds, withdrawals, and display rows are not ingested as sales."""
    store = PulseStore(db_path=tmp_path / "tcodes.db")

    def row(tx_id: str, value: str, event: str) -> dict:
        return {
            "transaction_info": {
                "transaction_id": tx_id,
                "transaction_initiation_date": "2026-11-10T14:30:00Z",
                "transaction_amount": {"currency_code": "USD", "value": value},
                "fee_amount": {"currency_code": "USD", "value": "-0.34"},
                "transaction_status": "S",
                "transaction_event_code": event,
            },
            "payer_info": {"country_code": "US"},
        }

    rows = [
        row("PP-HOLD", "100.00", "T2101"),
        row("PP-WITHDRAW", "25.00", "T1700"),
        row("PP-DISPLAY", "50.00", "T9800"),
        row("PP-PAYMENT", "10.00", "T0006"),
    ]
    assert store.sync_external_transactions(rows) == 1
    assert [t["paypal_id"] for t in store.get_transactions()] == ["PP-PAYMENT"]


def test_sync_cli_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verify pp sync command executes authentication, dispute sync, and transaction sync."""
    db_file = tmp_path / "cli_sync.db"
    monkeypatch.setenv("PULSE_DB_PATH", str(db_file))

    # Test sync --demo --json
    res_json = runner.invoke(app, ["sync", "--demo", "--json"])
    assert res_json.exit_code == 0
    payload = json.loads(res_json.stdout)
    assert payload["ok"] is True
    assert "disputes_synced" in payload
    assert "transactions_synced" in payload

    # Test rich format output
    res_text = runner.invoke(app, ["sync", "--demo"])
    assert res_text.exit_code == 0
    assert "PAYPAL TELEMETRY SYNC" in res_text.stdout
    assert "Disputes Ingested" in res_text.stdout


def test_pulse_refresh_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verify pp pulse -r flag runs refresh and displays dashboard."""
    db_file = tmp_path / "cli_pulse_refresh.db"
    monkeypatch.setenv("PULSE_DB_PATH", str(db_file))

    res = runner.invoke(app, ["pulse", "--demo", "-r", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert "total_gross_usd" in data
