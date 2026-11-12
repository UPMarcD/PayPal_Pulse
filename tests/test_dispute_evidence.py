"""Tests for dispute evidence type selection and submission paths."""

from __future__ import annotations

import json
from typing import Any

from pulse.config import PulseSettings
from pulse.paypal_client import PayPalClient


class _Response:
    def __init__(self, status_code: int = 201, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _client() -> PayPalClient:
    return PayPalClient(
        PulseSettings(paypal_client_id="test-id", paypal_client_secret="test-secret")
    )


def test_evidence_type_matches_paypal_reason_enum() -> None:
    """Verify evidence types match PayPal's documented dispute-reason table."""
    assert (
        PayPalClient.select_evidence_type_for_reason(
            "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
        )
        == "OTHER"
    )
    assert (
        PayPalClient.select_evidence_type_for_reason("DUPLICATE_TRANSACTION")
        == "OTHER"
    )
    assert (
        PayPalClient.select_evidence_type_for_reason(
            "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
        )
        == "PROOF_OF_FULFILLMENT"
    )


def test_notes_only_fulfillment_evidence(monkeypatch) -> None:
    """Verify INR evidence submits notes without tracking instead of blocking."""
    client = _client()
    captured: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> _Response:
        captured.append({"method": method, "url": url, **kwargs})
        return _Response(201)

    monkeypatch.setattr(client, "_request_with_retry", fake_request)
    monkeypatch.setattr(
        client,
        "get_dispute_details",
        lambda *a, **k: {"ok": True, "dispute": {"status": "UNDER_REVIEW"}},
    )

    res = client.submit_dispute_resolution(
        dispute_id="PP-D-1",
        evidence_text="Digital delivery via download link.",
        reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        resolution_strategy="FULFILL_AND_EVIDENCE",
    )

    assert res["ok"] is True
    payload = json.loads(captured[0]["files"]["input"][1])
    evidence = payload["evidences"][0]
    assert evidence["evidence_type"] == "PROOF_OF_FULFILLMENT"
    assert evidence["notes"]
    assert "evidence_info" not in evidence


def test_accept_and_refund_skips_evidence(monkeypatch) -> None:
    """Verify the accept-claim path does not call provide-evidence."""
    client = _client()
    calls: list[str] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> _Response:
        calls.append(url)
        return _Response(204)

    monkeypatch.setattr(client, "_request_with_retry", fake_request)
    monkeypatch.setattr(
        client,
        "get_dispute_details",
        lambda *a, **k: {"ok": True, "dispute": {"status": "RESOLVED"}},
    )
    monkeypatch.setattr(client, "create_order", lambda **k: None)

    res = client.submit_dispute_resolution(
        dispute_id="PP-D-2",
        evidence_text="refund",
        reason="DUPLICATE_TRANSACTION",
        resolution_strategy="ACCEPT_AND_REFUND",
    )

    assert res["ok"] is True
    assert "CLAIM_REFUNDED" in res["dispute_api_status"]
    assert not any("provide-evidence" in c for c in calls)
    assert any("accept-claim" in c for c in calls)