"""Tests for 12-Factor CLI standards: stream separation, --json machine readability, and POSIX exit codes."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from pulse.cli import app
from pulse.config import PulseSettings
from pulse.constants import EX_CONFIG, EX_GENERAL, EX_NOPERM, EX_OK, EX_UNAVAILABLE, EX_USAGE
from pulse.paypal_client import PayPalAuthResult
from pulse.store import PulseStore

runner = CliRunner()


def test_cli_pulse_json_stream_separation() -> None:
    """Verify `pp pulse --json` writes clean, parseable JSON to stdout without ANSI escape sequences."""
    result = runner.invoke(app, ["pulse", "--json"])
    assert result.exit_code == EX_OK
    # Must parse cleanly via json.loads
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    assert "total_gross_usd" in data
    assert "total_net_usd" in data
    assert "currencies" in data
    assert "daily_14" in data


def test_cli_timeline_json_stream_separation() -> None:
    """Verify `pp timeline --json` outputs structured release correlation array."""
    result = runner.invoke(app, ["timeline", "--json"])
    assert result.exit_code == EX_OK
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) >= 3
    assert any(r.get("version") == "v1.2.1" for r in data)


def test_cli_disputes_json_stream_separation() -> None:
    """Verify `pp disputes --json` outputs disputes ledger array."""
    result = runner.invoke(app, ["disputes", "--json"])
    assert result.exit_code == EX_OK
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert "dispute_id" in data[0]


def test_cli_auth_json_and_exit_codes() -> None:
    """Verify `pp auth --json` outputs auth payload and returns proper semantic exit codes."""
    # Test successful or cached auth
    result = runner.invoke(app, ["auth", "--json"])
    assert result.stdout.strip().startswith("{")
    data = json.loads(result.stdout)
    assert "paypal" in data
    assert "pulse_ai" in data

    # Test failure exit code mapping when auth fails
    mock_fail_auth = PayPalAuthResult(
        authenticated=False,
        error_type="AUTH_FAILED",
        error="Invalid client credentials",
    )
    with patch("pulse.cli.PayPalClient.authenticate", return_value=mock_fail_auth):
        res_fail = runner.invoke(app, ["auth", "--json"])
        assert res_fail.exit_code == EX_NOPERM  # 77

    mock_timeout_auth = PayPalAuthResult(
        authenticated=False,
        error_type="NETWORK_UNAVAILABLE",
        error="Gateway timeout",
    )
    with patch("pulse.cli.PayPalClient.authenticate", return_value=mock_timeout_auth):
        res_timeout = runner.invoke(app, ["auth", "--json"])
        assert res_timeout.exit_code == EX_UNAVAILABLE  # 69


def test_cli_disputes_not_found_exit_code() -> None:
    """Verify resolving non-existent dispute returns semantic exit code."""
    result = runner.invoke(app, ["disputes", "--resolve", "PP-NON-EXISTENT-999"])
    assert result.exit_code == EX_USAGE  # 2


def test_cli_seed_json_stream_separation() -> None:
    """Verify `pp seed --json` outputs structured JSON receipt with orders and summary."""
    result = runner.invoke(app, ["seed", "--json"])
    assert result.exit_code == EX_OK
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    assert data.get("status") == "seeded"
    assert "orders_created" in data
    assert "summary" in data
    assert "total_gross_usd" in data["summary"]


def test_interrogative_intent_guard() -> None:
    """Verify interrogative inquiries do not trigger order creation side effects."""
    from pulse.ai_copilot import _detect_order_intent

    # Interrogative/informational queries must NOT trigger side effects
    interrogative_queries = [
        "How do I create an order?",
        "how to make a checkout link?",
        "Can you explain how to create an invoice?",
        "What happens when I issue an order?",
        "Why should I build a payment link?",
        "Explain how to generate an order for customers",
    ]
    for q in interrogative_queries:
        should_create, _, _, _ = _detect_order_intent(q)
        assert not should_create, f"Interrogative query '{q}' should not trigger order creation intent"

    # Direct imperative commands MUST trigger order creation intent
    imperative_queries = [
        "Create a $14.99 USD order for PulseCore Pro",
        "Generate a 5.99 EUR payment link for EU tier",
        "Make an invoice for 49.00 USD to partner@example.com",
    ]
    for q in imperative_queries:
        should_create, amt, cur, _ = _detect_order_intent(q)
        assert should_create, f"Imperative command '{q}' should trigger order creation intent"
        assert amt > 0


def test_paypal_client_401_token_refresh() -> None:
    """Verify PayPalClient _request_with_retry automatically refreshes token on HTTP 401."""
    import httpx
    from pulse.paypal_client import PayPalClient

    client = PayPalClient()
    client._access_token = "expired_dummy_token"

    # Mock response 1: 401 Unauthorized, Mock response 2: 200 OK
    resp_401 = httpx.Response(status_code=401, request=httpx.Request("GET", "https://api-m.sandbox.paypal.com/test"))
    resp_200 = httpx.Response(status_code=200, json={"ok": True}, request=httpx.Request("GET", "https://api-m.sandbox.paypal.com/test"))

    def fake_auth(force_refresh: bool = False) -> PayPalAuthResult:
        client._access_token = "fresh_token_123"
        return PayPalAuthResult(authenticated=True)

    with patch("httpx.Client.request", side_effect=[resp_401, resp_200]), \
         patch.object(client, "authenticate", side_effect=fake_auth) as mock_auth_call:
        final_resp = client._request_with_retry("GET", "https://api-m.sandbox.paypal.com/test")
        assert final_resp.status_code == 200
        mock_auth_call.assert_called_once_with(force_refresh=True)
        assert client._access_token == "fresh_token_123"


def test_cli_demo_json_and_db_isolation() -> None:
    """Verify `pp demo --json` outputs demo metadata pointing to isolated demo.db."""
    result = runner.invoke(app, ["demo", "--json"])
    assert result.exit_code == EX_OK
    data = json.loads(result.stdout)
    assert data.get("mode") == "demo"
    assert "demo.db" in data.get("db_path", "")
    assert "summary" in data
    assert data["summary"]["seeded"] is True


def test_cli_setup_non_interactive_guard() -> None:
    """Verify `pp setup` fails with EX_USAGE in non-interactive environment without flags."""
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == EX_USAGE


def test_cli_setup_demo_flag() -> None:
    """Verify `pp setup --demo` invokes demo mode cleanly."""
    result = runner.invoke(app, ["setup", "--demo"])
    assert result.exit_code == EX_OK
    assert "demo.db" in result.stdout or "Demo" in result.stdout


def test_hierarchical_global_config(tmp_path: Path) -> None:
    """Verify save_global_config and get_settings read from global user config file."""
    from pulse.config import GLOBAL_CONFIG_FILE, get_settings, save_global_config
    import os

    # Temporarily override GLOBAL_CONFIG_FILE path for testing
    test_cfg_file = tmp_path / "global_config.json"
    with patch("pulse.config.GLOBAL_CONFIG_FILE", test_cfg_file), \
         patch.dict(os.environ, {"PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": "", "GROQ_API_KEY": ""}, clear=False):
        save_global_config(
            paypal_client_id="test_global_client_id",
            paypal_client_secret="test_global_secret",
            paypal_mode="sandbox",
            groq_api_key="test_global_groq",
        )
        assert test_cfg_file.exists()
        loaded = get_settings()
        assert loaded.paypal_client_id == "test_global_client_id"
        assert loaded.paypal_client_secret == "test_global_secret"
        assert loaded.groq_api_key == "test_global_groq"


def test_cli_status_alias(tmp_path):
    """Test `pp status` functions as a full alias to `pp pulse`."""
    db_file = tmp_path / "test_status.db"
    store = PulseStore(db_file)
    store.seed_telemetry()

    with patch("pulse.cli.get_settings") as mock_get:
        mock_get.return_value = PulseSettings(
            paypal_client_id="mock_id",
            paypal_client_secret="mock_secret",
            groq_api_key="mock_groq",
            cache_dir=tmp_path,
        )
        res = runner.invoke(app, ["status", "--json", "--demo"])
        assert res.exit_code == 0
        data = json.loads(res.stdout)
        assert "total_gross_usd" in data
        assert "fee_drag_pct" in data


def test_cli_releases_alias_and_since(tmp_path):
    """Test `pp releases` alias and `--since` date filter."""
    db_file = tmp_path / "test_releases.db"
    store = PulseStore(db_file)
    store.seed_telemetry()

    with patch("pulse.cli.get_settings") as mock_get:
        mock_get.return_value = PulseSettings(
            paypal_client_id="mock_id",
            paypal_client_secret="mock_secret",
            groq_api_key="mock_groq",
            cache_dir=tmp_path,
        )
        res = runner.invoke(app, ["releases", "--since", "2026-10-20", "--json", "--demo"])
        assert res.exit_code == 0
        items = json.loads(res.stdout)
        versions = [r["version"] for r in items]
        assert "v1.2.0" in versions
        assert "v1.2.1" in versions
        assert "v1.1.0" not in versions


def test_cli_disputes_open_flag(tmp_path):
    """Test `pp disputes --open` filters out resolved cases."""
    db_file = tmp_path / "test_disputes_open.db"
    store = PulseStore(db_file)
    store.seed_telemetry()

    with patch("pulse.cli.get_settings") as mock_get:
        mock_get.return_value = PulseSettings(
            paypal_client_id="mock_id",
            paypal_client_secret="mock_secret",
            groq_api_key="mock_groq",
            cache_dir=tmp_path,
        )
        res = runner.invoke(app, ["disputes", "--open", "--json", "--demo"])
        assert res.exit_code == 0
        items = json.loads(res.stdout)
        assert len(items) == 3
        for item in items:
            assert item["status"] == "OPEN"


def test_cli_ledger_csv_export(tmp_path):
    """Test `pp ledger --csv` outputs clean CSV with transaction headers."""
    db_file = tmp_path / "test_ledger.db"
    store = PulseStore(db_file)
    store.seed_telemetry()

    with patch("pulse.cli.get_settings") as mock_get:
        mock_get.return_value = PulseSettings(
            paypal_client_id="mock_id",
            paypal_client_secret="mock_secret",
            groq_api_key="mock_groq",
            cache_dir=tmp_path,
        )
        res = runner.invoke(app, ["ledger", "--csv", "--demo"])
        assert res.exit_code == 0
        assert "paypal_id,created_at,gross_amount" in res.stdout
        assert "PP-TXN-" in res.stdout


def test_mcp_server_protocol_lifecycle(tmp_path):
    """Test standard JSON-RPC 2.0 initialize, tools/list, and tool execution."""
    import io
    from pulse.mcp_server import run_mcp_stdio

    db_file = tmp_path / "test_mcp.db"
    store = PulseStore(db_file)
    store.seed_telemetry()

    requests = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "pulse_get_financial_summary", "arguments": {}},
        }),
    ]
    input_stream = io.StringIO("\n".join(requests) + "\n")
    output_stream = io.StringIO()

    with patch("pulse.mcp_server.get_settings") as mock_get, \
         patch("sys.stdin", input_stream), \
         patch("sys.stdout", output_stream), \
         patch("sys.stderr", io.StringIO()):
        mock_get.return_value = PulseSettings(
            paypal_client_id="mock_id",
            paypal_client_secret="mock_secret",
            groq_api_key="mock_groq",
            cache_dir=tmp_path,
        )
        run_mcp_stdio(demo_mode=True)

    output_lines = [json.loads(line) for line in output_stream.getvalue().splitlines() if line.strip()]
    assert len(output_lines) == 3
    # 1. Initialize
    assert output_lines[0]["id"] == 1
    assert output_lines[0]["result"]["serverInfo"]["name"] == "paypal-pulse"
    # 2. Tools list
    assert output_lines[1]["id"] == 2
    tool_names = [t["name"] for t in output_lines[1]["result"]["tools"]]
    assert "pulse_get_financial_summary" in tool_names
    assert "pulse_create_order" in tool_names
    # 3. Tool call
    assert output_lines[2]["id"] == 3
    assert output_lines[2]["result"]["isError"] is False
    summary_data = json.loads(output_lines[2]["result"]["content"][0]["text"])
    assert "total_gross_usd" in summary_data


def test_cli_order_unconfigured_ex_config(tmp_path):
    """Test `pp order` exits with EX_CONFIG (78) when credentials are missing."""
    with patch("pulse.cli.get_settings") as mock_get:
        mock_get.return_value = PulseSettings(
            paypal_client_id="",
            paypal_client_secret="",
            groq_api_key="",
            cache_dir=tmp_path,
        )
        res = runner.invoke(app, ["order", "10", "USD"])
        assert res.exit_code == EX_CONFIG
        assert "PAYPAL_CLIENT_ID not configured" in res.output


def test_emoji_free_terminal_standards_and_muted_yellow_accents():
    """Verify clean emoji-free typography, strip_emojis sanitization, and muted yellow accents."""
    from pulse.ai_copilot import strip_emojis
    from pulse.tui import PayPalPulseTUI

    # Test strip_emojis sanitizes broken glyphs and keycap emojis
    raw_text = "Quick examples\n# 1️⃣ Current KPI snapshot\n# 2️⃣ Export ledger\n⚡ Live tool\n⚠️ Alert\nﮩ٨ـ pulse"
    cleaned = strip_emojis(raw_text)
    assert "1." in cleaned
    assert "2." in cleaned
    assert "1️⃣" not in cleaned
    assert "2️⃣" not in cleaned
    assert "⚡" not in cleaned
    assert "⚠️" not in cleaned
    assert "ﮩ٨ـ" not in cleaned
    assert "[ALERT]" in cleaned

    # Verify TUI CSS enforces muted PayPal yellow accent (#e5a824) and navy canvas
    assert "#e5a824" in PayPalPulseTUI.CSS
    assert "#0a101d" in PayPalPulseTUI.CSS


def test_pulse_mascot_sprites_and_headers():
    """Verify Pulsey mascot sprites and stateful headers render without error and without glitch characters."""
    from pulse.mascot import (
        PULSE_GLYPH,
        PULSE_MASCOT_ALERT,
        PULSE_MASCOT_IDLE,
        PULSE_MASCOT_SUCCESS,
        PULSE_MASCOT_THINK,
        format_mascot_header,
    )

    for sprite in (PULSE_MASCOT_IDLE, PULSE_MASCOT_THINK, PULSE_MASCOT_SUCCESS, PULSE_MASCOT_ALERT):
        assert "▀█▀" in sprite
        assert "·▛·" in sprite
        assert "ﮩ٨ـ" not in sprite
        assert "⚡" not in sprite

    assert "ﮩ٨ـ" not in PULSE_GLYPH
    assert "⚡" not in PULSE_GLYPH
    assert "Pulse" in PULSE_GLYPH

    for state in ("idle", "think", "success", "alert"):
        hdr = format_mascot_header("Test Title", "Test Subtitle", state=state)
        assert "Test Title" in hdr
        assert "·▛·" in hdr
        assert "ﮩ٨ـ" not in hdr
        assert "⚡" not in hdr




