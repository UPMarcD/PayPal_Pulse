"""Tests for Textual TUI resize resilience, minimum viewport guards, and Vim navigation."""

from __future__ import annotations

import pytest
from textual import events
from textual.geometry import Size
from textual.widgets import DataTable, Static

from pulse.tui import PayPalPulseTUI


@pytest.mark.asyncio
async def test_tui_resize_guard_overlay() -> None:
    """Verify that resizing the terminal window below 80x24 displays the warning overlay."""
    app = PayPalPulseTUI()
    async with app.run_test(size=(120, 35)) as pilot:
        overlay = app.query_one("#resize-warning-overlay", Static)
        assert overlay.display is False

        # Simulate resize below 80x24 (e.g. 70x20)
        app.on_resize(events.Resize(size=Size(70, 20), virtual_size=Size(70, 20)))
        assert overlay.display is True
        assert "TOO SMALL" in str(overlay.render())

        # Simulate resize back to comfort size
        app.on_resize(events.Resize(size=Size(100, 30), virtual_size=Size(100, 30)))
        assert overlay.display is False


@pytest.mark.asyncio
async def test_tui_vim_navigation_bindings() -> None:
    """Verify Vim 'j' and 'k' keys navigate table rows without unhandled exceptions."""
    app = PayPalPulseTUI()
    async with app.run_test(size=(120, 35)) as pilot:
        tbl = app.query_one("#currency-table", DataTable)
        initial_row = tbl.cursor_row
        app.action_vim_down()
        assert tbl.cursor_row >= initial_row
        app.action_vim_up()


@pytest.mark.asyncio
async def test_tui_numeric_tab_bindings() -> None:
    """Verify numeric keys 1-5 jump directly to corresponding tabs."""
    from textual.widgets import TabbedContent
    app = PayPalPulseTUI()
    async with app.run_test(size=(120, 35)) as pilot:
        tabs = app.query_one("#main-tabs", TabbedContent)

        app.action_switch_tab("tab-timeline")
        assert tabs.active == "tab-timeline"

        app.action_switch_tab("tab-orders")
        assert tabs.active == "tab-orders"

        app.action_switch_tab("tab-disputes")
        assert tabs.active == "tab-disputes"

        app.action_switch_tab("tab-copilot")
        assert tabs.active == "tab-copilot"

        app.action_switch_tab("tab-dashboard")
        assert tabs.active == "tab-dashboard"


@pytest.mark.asyncio
async def test_tui_help_modal_push_and_dismiss() -> None:
    """Verify pressing '?' opens HelpModal and ESC closes it."""
    from pulse.tui import HelpModal
    app = PayPalPulseTUI()
    async with app.run_test(size=(120, 35)) as pilot:
        assert not isinstance(app.screen, HelpModal)
        app.action_show_help()
        await pilot.pause()
        assert isinstance(app.screen, HelpModal)
        # Dismiss help modal
        app.screen.dismiss()
        await pilot.pause()
        assert not isinstance(app.screen, HelpModal)


@pytest.mark.asyncio
async def test_tui_disputes_browser_button_and_row_select(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Verify that Tab 4 has the open-dispute-browser button and row selection populates dispute input."""
    from textual.widgets import Button, Input
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path))
    app = PayPalPulseTUI()
    app.store.seed_telemetry()
    opened_urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened_urls.append(url))

    async with app.run_test(size=(120, 35)) as pilot:
        app.action_switch_tab("tab-disputes")
        await pilot.pause()

        btn = app.query_one("#btn-open-dispute-browser", Button)
        assert btn is not None
        assert "PayPal" in str(btn.label)

        resolve_btn = app.query_one("#btn-resolve-dispute", Button)
        assert resolve_btn is not None
        assert str(resolve_btn.label) == "Resolve with AI"

        input_field = app.query_one("#dispute-id-input", Input)
        input_field.value = "PP-D-TEST-123"

        app.handle_open_dispute_browser()
        assert len(opened_urls) == 1
        assert "PP-D-TEST-123" in opened_urls[0]
        assert "paypal.com/disputes" in opened_urls[0]

        reload_btn = app.query_one("#btn-sync-disputes", Button)
        assert reload_btn is not None
        assert str(reload_btn.label) == "Reload"

        # Verify simulate dispute button has been removed from UI
        assert len(app.query("#btn-simulate-dispute")) == 0

        # Verify dynamic button update for resolved disputes
        app.store.mark_dispute_resolved("PP-D-90411-DE", "**Evidence Statement:** Hotfix deployed", "ORDER-999")
        app._update_dispute_action_ui("PP-D-90411-DE")
        assert str(resolve_btn.label) == "View Evidence"
        assert resolve_btn.variant == "success"

        # Verify table row selection/highlighting updates input field
        from textual.widgets import DataTable
        table = app.query_one("#disputes-table", DataTable)
        table.move_cursor(row=1)
        await pilot.pause()
        assert input_field.value in ["PP-D-90418-DE", "PP-D-90425-DE", "PP-D-90411-DE"]


@pytest.mark.asyncio
async def test_tui_kpi_cards_formatting_and_mrr(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that KPI cards format cleanly and display MRR properly on standard viewports."""
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path))
    app = PayPalPulseTUI()
    app.store.seed_telemetry()
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        kpi_watch = app.query_one("#kpi-watch", Static)
        text = str(kpi_watch.render())
        assert "MRR:" in text
        assert "/mo" in text
        assert "CUSTOMERS & SALES" in text

        kpi_net = app.query_one("#kpi-net", Static)
        net_text = str(kpi_net.render())
        assert "TRUE NET TAKE-HOME" in net_text
        assert "Fees:" in net_text

        kpi_health = app.query_one("#kpi-health", Static)
        health_text = str(kpi_health.render())
        assert "MERCHANT HEALTH" in health_text
        assert "Disputes:" in health_text


