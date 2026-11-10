"""Tests for thread-safe OAuth2 lifecycle and token renewal window."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from pulse.config import PulseSettings
from pulse.paypal_client import PayPalAuthResult, PayPalClient


def test_concurrent_auth_double_checked_locking() -> None:
    """Verify that multiple concurrent threads calling authenticate() don't stampede PayPal API."""
    settings = PulseSettings(
        paypal_client_id="TEST_CLIENT_ID_12345678",
        paypal_client_secret="TEST_SECRET_ABCDEF",
        paypal_mode="sandbox",
    )
    client = PayPalClient(settings)

    mock_resp_data = {
        "access_token": "TEST_BEARER_TOKEN_CONCURRENT_999",
        "app_id": "APP-TEST-999",
        "expires_in": 32400,
        "scope": "https://api.paypal.com/v1/payments/.*",
        "nonce": "test-nonce",
    }

    call_count = 0

    def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)  # simulate network latency
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = mock_resp_data
        return resp

    # Run 10 threads concurrently
    with patch("httpx.Client.post", side_effect=mock_post):
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(client.authenticate, False) for _ in range(10)]
            results = [f.result() for f in futures]

    assert all(r.authenticated is True for r in results)
    # Thanks to double-checked locking, only the first thread executes the network request!
    assert call_count == 1
