"""Comprehensive unit, edge-case, concurrency, Textual TUI pilot, and live integration tests for PayPal Pulse (`pp`)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from textual.widgets import DataTable, Input, Static, TabbedContent
from typer.testing import CliRunner

from pulse.ai_copilot import (
    _detect_order_intent,
    ask_copilot,
    verify_groq_connection,
)
from pulse.cli import app
from pulse.config import get_settings
from pulse.paypal_client import PayPalClient, PayPalOrderResult
from pulse.store import PulseStore, compute_paypal_fee
from pulse.tui import PayPalPulseTUI

runner = CliRunner()


def test_fee_calculation_edge_cases() -> None:
    """Verify PayPal fee drag calculation across currencies and amounts."""
    usd_fee = compute_paypal_fee(10.00, "USD", cross_border=False)
    assert usd_fee > 0.49
    eur_fee = compute_paypal_fee(5.99, "EUR", cross_border=True)
    assert 0.39 < eur_fee < 5.99
    php_fee = compute_paypal_fee(299.00, "PHP", cross_border=True)
    assert php_fee > 25.00


def test_order_intent_parser_edge_cases() -> None:
    """Verify natural language order/invoice detection, version disambiguation, and PowerShell $var stripping guard."""
    ok, amt, cur, _ = _detect_order_intent("Create a $14.99 USD order for ChronoPulse Pro")
    assert ok is True
    assert amt == 14.99
    assert cur == "USD"

    ok_eur, amt_eur, cur_eur, _ = _detect_order_intent(
        "Generate a PayPal checkout link for €5.99 EUR on v1.2.1"
    )
    assert ok_eur is True
    assert amt_eur == 5.99
    assert cur_eur == "EUR"

    # Guard against PowerShell variable expansion stripping "$14" from "$14.99" -> ".99"
    ok_ps, amt_ps, cur_ps, _ = _detect_order_intent("Create a .99 USD order for ChronoPulse Pro")
    assert ok_ps is True
    assert amt_ps == 9.99
    assert cur_ps == "USD"

    # Invoice with email containing digits should extract price, not email digits
    ok_inv, amt_inv, cur_inv, _ = _detect_order_intent(
        "Generate a PayPal invoice for 49.00 USD to partner25@example.com"
    )
    assert ok_inv is True
    assert amt_inv == 49.00
    assert cur_inv == "USD"

    ok_no, _, _, _ = _detect_order_intent("Why did German users dispute v1.2.1?")
    assert ok_no is False


def test_sqlite_dual_engine_store_isolated(tmp_path: Path) -> None:
    """Test Dual-Engine SQLite store seeding, rollups, conversion_rate, daily_metrics, and dispute sync."""
    db_file = tmp_path / "test_pulse.db"
    store = PulseStore(db_path=db_file)
    assert store.is_seeded() is False

    mock_live = PayPalOrderResult(
        success=True,
        order_id="TEST-LIVE-ORDER-999",
        status="CREATED",
        amount=9.99,
        currency="USD",
        description="Test Order",
        approve_url="https://www.sandbox.paypal.com/checkoutnow?token=TEST-LIVE-ORDER-999",
    )
    summary = store.seed_smartwatch_telemetry(live_orders=[mock_live])
    assert store.is_seeded() is True
    assert summary["total_gross_usd"] > 5000
    assert summary["total_net_usd"] > 4000
    assert summary["open_disputes_count"] == 3
    assert len(summary["releases"]) == 3
    assert len(summary["daily_14"]) == 14

    before_v121 = next(r for r in store.get_releases(include_git_tags=False) if r["version"] == "v1.2.1")
    before_today_orders = summary["daily_14"][-1]["paid_orders"]

    # Record another live order dynamically
    new_order = PayPalOrderResult(
        success=True,
        order_id="TEST-DYNAMIC-ORDER-1000",
        status="CREATED",
        amount=19.99,
        currency="EUR",
        description="Dynamic Order",
        approve_url="https://www.sandbox.paypal.com/checkoutnow?token=TEST-DYNAMIC-ORDER-1000",
    )
    rec = store.record_live_order(new_order, release_version="v1.2.1", country_code="DE")
    assert rec["paypal_id"] == "TEST-DYNAMIC-ORDER-1000"
    assert rec["net"] < 19.99

    after_v121 = next(r for r in store.get_releases(include_git_tags=False) if r["version"] == "v1.2.1")
    assert after_v121["paid_conversions"] == before_v121["paid_conversions"] + 1
    assert after_v121["conversion_rate"] > before_v121["conversion_rate"]

    after_today_orders = store.get_recent_daily_metrics(days=14)[-1]["paid_orders"]
    assert after_today_orders == before_today_orders + 1

    # Recording duplicate paypal_id updates status without double-counting rollups
    dup_order = PayPalOrderResult(
        success=True,
        order_id="TEST-DYNAMIC-ORDER-1000",
        status="COMPLETED",
        amount=19.99,
        currency="EUR",
    )
    store.record_live_order(dup_order, release_version="v1.2.1", country_code="DE")
    dup_v121 = next(r for r in store.get_releases(include_git_tags=False) if r["version"] == "v1.2.1")
    assert dup_v121["paid_conversions"] == after_v121["paid_conversions"]

    # Resolve 1 dispute and verify releases.dispute_count and status_badge synchronize
    resolved_ok = store.mark_dispute_resolved(
        "PP-D-90411-DE",
        "Patched in v1.2.2",
        "TEST-DYNAMIC-ORDER-1000",
    )
    assert resolved_ok is True
    updated_summary = store.get_dashboard_summary()
    assert updated_summary["open_disputes_count"] == 2
    rel_v121 = next(r for r in store.get_releases(include_git_tags=False) if r["version"] == "v1.2.1")
    assert rel_v121["dispute_count"] == 2
    assert "2 DE" in rel_v121["status_badge"]

    # Resolve remaining 2 disputes and verify badge updates to RESOLVED
    store.mark_dispute_resolved("PP-D-90418-DE", "Patched in v1.2.2", "TEST-DYNAMIC-ORDER-1001")
    store.mark_dispute_resolved("PP-D-90425-DE", "Patched in v1.2.2", "TEST-DYNAMIC-ORDER-1002")
    rel_all_resolved = next(r for r in store.get_releases(include_git_tags=False) if r["version"] == "v1.2.1")
    assert rel_all_resolved["dispute_count"] == 0
    assert "RESOLVED" in rel_all_resolved["status_badge"]

    # Non-existent dispute returns False
    assert store.mark_dispute_resolved("NON-EXISTENT-ID", "notes", "ORD") is False


def test_concurrent_sqlite_writes(tmp_path: Path) -> None:
    """Verify WAL mode + busy_timeout handles concurrent SQLite writes and reads without locking errors."""
    db_file = tmp_path / "concurrent_pulse.db"
    store = PulseStore(db_path=db_file)
    store.seed_smartwatch_telemetry()

    def _worker(idx: int) -> str:
        worker_store = PulseStore(db_path=db_file)
        order = PayPalOrderResult(
            success=True,
            order_id=f"CONCURRENT-ORD-{idx}",
            status="CREATED",
            amount=4.99 + idx,
            currency="USD",
            description=f"Concurrent Order {idx}",
            approve_url=f"https://www.sandbox.paypal.com/checkoutnow?token=CONCURRENT-ORD-{idx}",
        )
        worker_store.record_live_order(order, release_version="v1.2.1", country_code="US")
        summary = worker_store.get_dashboard_summary()
        assert summary["total_gross_usd"] > 0
        return order.order_id

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_worker, range(12)))

    assert len(results) == 12
    live_txs = store.get_live_sandbox_transactions(limit=20)
    assert len(live_txs) == 12


def test_live_paypal_sandbox_and_groq_integration(tmp_path: Path) -> None:
    """Test real PayPal Sandbox OAuth2, Order & Invoice Creation, and Pulse AI API with .env credentials."""
    settings = get_settings()
    assert settings.has_paypal_credentials is True
    assert settings.has_groq_credentials is True

    client = PayPalClient(settings)
    auth = client.authenticate(force_refresh=True)
    assert auth.authenticated is True
    assert auth.app_id != ""

    # Edge cases on client
    bad_amt = client.create_order(amount=-5.0, currency="USD")
    assert bad_amt.success is False
    empty_id = client.get_order("")
    assert empty_id.success is False

    # Create and fetch a real order on PayPal Sandbox
    order = client.create_order(amount=4.99, currency="USD", description="Pytest Live Verification")
    assert order.success is True
    assert len(order.order_id) > 8
    assert "sandbox.paypal.com" in order.approve_url

    fetched = client.get_order(order.order_id)
    assert fetched.success is True
    assert fetched.order_id == order.order_id

    # Verify Pulse AI connection
    groq_status = verify_groq_connection(settings)
    assert groq_status.connected is True
    assert groq_status.available_models_count > 0

    # Verify ask_copilot invoice + order tool execution on PayPal Sandbox
    iso_store = PulseStore(db_path=tmp_path / "copilot_live.db")
    iso_store.seed_smartwatch_telemetry()
    ans = ask_copilot(
        "Generate a PayPal invoice for 29.00 USD to partner@example.com",
        store=iso_store,
        paypal_client=client,
        settings=settings,
    )
    assert ans.created_invoice is not None and ans.created_invoice.success is True
    assert ans.created_order is not None and ans.created_order.success is True
    assert "Invoice" in (ans.executed_action or "")
    assert ans.model_used != "" and "groq/" not in ans.model_used


def test_cli_commands_end_to_end() -> None:
    """Run all CLI commands through Typer CliRunner to verify zero exit errors and Pulse branding."""
    r_auth = runner.invoke(app, ["auth", "--cached"])
    assert r_auth.exit_code == 0
    assert "AUTHENTICATED" in r_auth.stdout
    assert "Pulse AI" in r_auth.stdout
    assert "Groq Copilot" not in r_auth.stdout

    r_pulse = runner.invoke(app, ["pulse"])
    assert r_pulse.exit_code == 0
    assert "True Net Revenue" in r_pulse.stdout
    assert "Groq Copilot" not in r_auth.stdout

    r_timeline = runner.invoke(app, ["timeline"])
    assert r_timeline.exit_code == 0
    assert "v1.2.1" in r_timeline.stdout

    r_disputes = runner.invoke(app, ["disputes"])
    assert r_disputes.exit_code == 0
    assert "PP-D-90411-DE" in r_disputes.stdout

    # Test unquoted multi-word question in `pp ask`
    r_ask = runner.invoke(app, ["ask", "What", "is", "our", "net", "margin?"])
    assert r_ask.exit_code == 0
    assert "Pulse" in r_ask.stdout
    assert "Groq Copilot" not in r_ask.stdout

    r_ask_empty = ask_copilot("   ")
    assert r_ask_empty.model_used == "validation"

    r_tui_check = runner.invoke(app, ["tui", "--check"])
    assert r_tui_check.exit_code == 0


def test_textual_tui_interactive_pilot(tmp_path: Path) -> None:
    """Mount PayPalPulseTUI in a headless Textual terminal session and exercise all tabs, non-blocking workers, and GroqChat-modeled Pulse UI."""
    settings = get_settings()
    iso_store = PulseStore(db_path=tmp_path / "tui_pilot.db")
    iso_store.seed_smartwatch_telemetry()
    pp_client = PayPalClient(settings)

    async def _run_pilot() -> None:
        tui = PayPalPulseTUI(settings=settings, store=iso_store, pp_client=pp_client)
        assert "Pulse AI" in tui.SUB_TITLE
        assert "Groq" not in tui.SUB_TITLE
        async with tui.run_test(size=(130, 42)) as pilot:
            await tui.workers.wait_for_complete()
            await pilot.pause()
            tabs = tui.query_one("#main-tabs", TabbedContent)
            assert tabs.active == "tab-dashboard"

            # Verify DataTables populated on mount
            cur_table = tui.query_one("#currency-table", DataTable)
            assert cur_table.row_count == 3

            # Test keybindings to switch across all 5 tabs + refresh
            await pilot.press("t")
            await pilot.pause()
            assert tabs.active == "tab-timeline"

            await pilot.press("o")
            await pilot.pause()
            assert tabs.active == "tab-orders"

            # Trigger Live Sandbox Order creation worker (including invalid input edge case first)
            tui.query_one("#order-amount", Input).value = "invalid"
            tui.handle_create_order()
            await tui.workers.wait_for_complete()
            await pilot.pause()

            tui.query_one("#order-amount", Input).value = "7.99"
            tui.handle_create_order()
            await tui.workers.wait_for_complete()
            await pilot.pause()
            assert len(iso_store.get_live_sandbox_transactions(limit=5)) >= 1

            # Switch to Disputes tab ('x') and resolve a dispute via background worker
            await pilot.press("x")
            await pilot.pause()
            assert tabs.active == "tab-disputes"

            # Exercise live dispute simulation
            tui.handle_simulate_dispute()
            assert iso_store.get_dashboard_summary()["open_disputes_count"] == 4

            # Exercise Sandbox dispute sync worker
            tui.handle_sync_disputes()
            await tui.workers.wait_for_complete()
            await pilot.pause()

            tui.query_one("#dispute-id-input", Input).value = "PP-D-90411-DE"
            tui.handle_resolve_dispute()
            await tui.workers.wait_for_complete()
            await pilot.pause()
            assert iso_store.get_dashboard_summary()["open_disputes_count"] == 3

            # Switch to Ask Pulse tab ('a') and verify GroqChat-modeled UI elements + background worker
            await pilot.press("a")
            await pilot.pause()
            assert tabs.active == "tab-copilot"
            assert len(tui.query(".prompt-chip")) == 4
            status_bar = tui.query_one("#copilot-context-bar", Static)
            assert status_bar is not None
            global_bar = tui.query_one("#global-context-bar", Static)
            assert global_bar is not None

            # Test empty input edge case
            tui.query_one("#copilot-input", Input).value = "   "
            tui.handle_ask_copilot()
            await tui.workers.wait_for_complete()
            await pilot.pause()

            # Test typed question submission
            tui.query_one("#copilot-input", Input).value = "Summarize v1.2.1 disputes in one sentence."
            tui.handle_ask_copilot()
            await tui.workers.wait_for_complete()
            await pilot.pause()

            # Test clicking a quick-prompt chip (#chip-margin)
            await pilot.click("#chip-margin")
            await tui.workers.wait_for_complete()
            await pilot.pause()

            # Unfocus active widget so keybindings are received by app, switch back to Dashboard ('d') and refresh ('r')
            tui.set_focus(None)
            await pilot.press("d")
            await pilot.pause()
            assert tabs.active == "tab-dashboard"
            tui.action_refresh_data()
            await tui.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(_run_pilot())

