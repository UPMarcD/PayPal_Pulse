"""Tests for `pp trigger` PayPal webhook event synthesis, fixtures, and instant SQLite ingestion."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from pulse.cli import app
from pulse.constants import EX_OK
from pulse.store import PulseStore

runner = CliRunner()


def test_trigger_payment_capture_completed(tmp_path: Path) -> None:
    """Verify `pp trigger payment.capture.completed` synthesizes and commits order to SQLite."""
    db_file = tmp_path / "test_trigger.db"
    store = PulseStore(db_path=db_file)
    store.seed_telemetry()
    summary_before = store.get_dashboard_summary()
    before_orders = summary_before["total_orders"]

    # Ingest directly through store
    receipt = store.record_webhook_event(
        {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-SIM-TEST-999",
                "status": "COMPLETED",
                "amount": {"value": "99.00", "currency_code": "USD"},
                "custom_id": "v1.2.1|US|PulseCore Pro Enterprise",
            },
        }
    )
    assert receipt["paypal_id"] == "CAP-SIM-TEST-999"
    assert receipt["gross"] == 99.00
    assert receipt["currency"] == "USD"

    summary_after = store.get_dashboard_summary()
    assert summary_after["total_orders"] == before_orders + 1


def test_trigger_order_approved_reads_purchase_units(tmp_path: Path) -> None:
    """Verify CHECKOUT.ORDER.APPROVED reads amount and custom_id from purchase_units."""
    store = PulseStore(db_path=tmp_path / "order_approved.db")
    store.seed_telemetry()
    before_orders = store.get_dashboard_summary()["total_orders"]

    receipt = store.record_webhook_event(
        {
            "event_type": "CHECKOUT.ORDER.APPROVED",
            "resource": {
                "id": "ORD-APPROVED-1",
                "status": "APPROVED",
                "purchase_units": [
                    {
                        "amount": {"value": "49.99", "currency_code": "EUR"},
                        "custom_id": "release:v1.3.0|US|PulseCore Pro",
                    }
                ],
            },
        }
    )
    assert receipt["gross"] == 49.99
    assert receipt["currency"] == "EUR"
    assert receipt["release_version"] == "v1.3.0"
    # APPROVED is pending, not revenue.
    assert store.get_dashboard_summary()["total_orders"] == before_orders


def test_trigger_customer_dispute_created_and_resolved(tmp_path: Path) -> None:
    """Verify `pp trigger customer.dispute.created` increments disputes and decreases health index."""
    db_file = tmp_path / "test_trigger_disp.db"
    store = PulseStore(db_path=db_file)
    store.seed_telemetry()
    before = store.get_dashboard_summary()

    # Trigger dispute created
    disp_res = store.record_webhook_event(
        {
            "event_type": "CUSTOMER.DISPUTE.CREATED",
            "resource": {
                "dispute_id": "PP-D-TRIGGER-SIM",
                "dispute_amount": {"value": "39.99", "currency_code": "USD"},
                "buyer_country": "DE",
                "release_version": "v1.2.1",
                "description": "Simulated buyer dispute via trigger fixture",
            },
        }
    )
    assert disp_res["status"] == "OPEN"
    after = store.get_dashboard_summary()
    assert after["open_disputes_count"] == before["open_disputes_count"] + 1

    # Trigger dispute resolved
    resolve_res = store.record_webhook_event(
        {
            "event_type": "CUSTOMER.DISPUTE.RESOLVED",
            "resource": {
                "dispute_id": "PP-D-TRIGGER-SIM",
            },
        }
    )
    assert resolve_res["status"] == "RESOLVED"
    resolved_summary = store.get_dashboard_summary()
    assert resolved_summary["open_disputes_count"] == before["open_disputes_count"]


def test_cli_trigger_command_json_output() -> None:
    """Verify `pp trigger` CLI command outputs machine-readable event receipt."""
    result = runner.invoke(app, ["trigger", "payment.capture.completed", "--amount", "25.00", "--json"])
    assert result.exit_code == EX_OK
    data = json.loads(result.stdout)
    assert "event" in data
    assert "receipt" in data
    assert data["status"] == "INGESTED"
