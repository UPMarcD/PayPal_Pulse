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
