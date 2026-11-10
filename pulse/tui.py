"""Interactive full-screen Textual Terminal UI (TUI) for PayPal Pulse (`pp tui`)."""

from __future__ import annotations

import asyncio
import webbrowser
from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)
from textual.worker import Worker

from pulse.ai_copilot import CopilotAnswer, ask_copilot, draft_dispute_evidence, get_active_groq_model
from pulse.config import PulseSettings, get_settings
from pulse.paypal_client import PayPalAuthResult, PayPalClient
from pulse.store import PulseStore

QUICK_PROMPT_MAP: dict[str, str] = {
    "chip-disputes": "Why did German users open disputes on v1.2.1?",
    "chip-userbase": "Who makes up the bulk of the userbase?",
    "chip-margin": "What is our true net margin after PayPal fees?",
    "chip-order": "Create a 9.99 USD order for PulseCore Pro Developer License",
}


def _build_sparkline(values: list[float]) -> str:
    if not values:
        return ""
    blocks = " ▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    span = (hi - lo) if hi > lo else 1.0
    return "".join(blocks[min(7, int(((v - lo) / span) * 7))] for v in values)


class HelpModal(ModalScreen[None]):
    """Modal dialog displaying keyboard navigation and shortcuts."""

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
        background: rgba(13, 17, 23, 0.85);
    }
    #help-dialog {
        width: 66;
        height: auto;
        border: round #58a6ff;
        background: #161b22;
        padding: 1 2;
    }
    #help-title {
        color: #58a6ff;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #help-close-tip {
        color: #8b949e;
        text-align: center;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
        Binding("space", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static("⌨  PayPal Pulse — Keyboard Shortcuts & Navigation", id="help-title")
            yield Static("[bold #58a6ff]Tabs & Views[/bold #58a6ff]")
            yield Static("  [bold #f0f6fc]1[/bold #f0f6fc] or [bold #f0f6fc]d[/bold #f0f6fc]    Dashboard & 30-Day Financial KPIs")
            yield Static("  [bold #f0f6fc]2[/bold #f0f6fc] or [bold #f0f6fc]t[/bold #f0f6fc]    Git Release-to-Revenue Correlation Timeline")
            yield Static("  [bold #f0f6fc]3[/bold #f0f6fc] or [bold #f0f6fc]o[/bold #f0f6fc]    Live PayPal Sandbox Orders & Invoicing")
            yield Static("  [bold #f0f6fc]4[/bold #f0f6fc] or [bold #f0f6fc]x[/bold #f0f6fc]    Customer Disputes & Evidence Drafter")
            yield Static("  [bold #f0f6fc]5[/bold #f0f6fc] or [bold #f0f6fc]a[/bold #f0f6fc]    Pulse AI Copilot Interactive Log\n")
            yield Static("[bold #58a6ff]Actions & Navigation[/bold #58a6ff]")
            yield Static("  [bold #f0f6fc]j / k[/bold #f0f6fc]        Scroll active DataTable rows (Vim down / up)")
            yield Static("  [bold #f0f6fc]r[/bold #f0f6fc]            Force refresh PayPal Sandbox balances & ledger")
            yield Static("  [bold #f0f6fc]L[/bold #f0f6fc]            Live/Sandbox credential setup")
            yield Static("  [bold #f0f6fc]Tab[/bold #f0f6fc]          Focus next interactive control or input")
            yield Static("  [bold #f0f6fc]?[/bold #f0f6fc]            Open this keyboard shortcuts cheat sheet")
            yield Static("  [bold #f0f6fc]q[/bold #f0f6fc]            Quit PayPal Pulse\n")
            yield Static("[dim]Press ESC, q, ?, or Space to dismiss[/dim]", id="help-close-tip")


class PayPalPulseTUI(App):
    """Full-screen terminal command center for PayPal Pulse."""

    TITLE = "PayPal Pulse"
    SUB_TITLE = "Pulse AI"

    CSS = """
    Screen {
        background: #0d1117;
        color: #c9d1d9;
        overflow: hidden;
    }
    #global-context-bar {
        height: auto;
        min-height: 1;
        border-bottom: solid #21262d;
        background: #161b22;
        padding: 0 1;
        margin: 0;
        color: #8b949e;
    }
    TabbedContent {
        background: #0d1117;
        height: 1fr;
    }
    Tabs {
        background: #0d1117;
        border-bottom: solid #21262d;
        height: 3;
    }
    Tab {
        background: #0d1117;
        color: #8b949e;
        padding: 0 2;
    }
    Tab:hover {
        color: #c9d1d9;
        background: #161b22;
    }
    Tab.-active {
        color: #f0f6fc;
        text-style: bold;
        background: #161b22;
    }
    Underline {
        color: #58a6ff;
    }
    TabPane {
        background: #0d1117;
        padding: 0 1;
        height: 1fr;
    }
    #dash-container {
        height: 1fr;
        padding: 0 1;
        overflow: hidden;
    }
    #dash-status-banner {
        display: none;
        height: 0;
        margin: 0;
        padding: 0;
    }
    .kpi-row {
        height: 5;
        margin: 0;
    }
    .kpi-card {
        width: 1fr;
        height: 100%;
        border: round #21262d;
        background: #161b22;
        padding: 0 1;
        margin: 0 1 0 0;
    }
    .kpi-card:hover {
        border: round #58a6ff;
    }
    .alert-banner {
        width: 100%;
        height: 1;
        border-left: thick #f85149;
        background: #1c1417;
        color: #ffccd0;
        padding: 0 1;
        margin: 0;
    }
    #dash-sparkline {
        height: 3;
        margin: 0;
    }
    .section-box {
        border: round #21262d;
        background: #161b22;
        padding: 0 1;
        margin: 0 0 1 0;
        height: auto;
    }
    .form-row {
        height: auto;
        margin: 1 0;
        align: left middle;
    }
    .form-row Input {
        width: 1fr;
        margin: 0 1 0 0;
        background: #161b22;
        border: round #30363d;
        color: #e6edf3;
    }
    .form-row Input:focus {
        border: round #58a6ff;
    }
    .form-row Button {
        width: auto;
        min-width: 18;
        margin: 0 1 0 0;
        background: #21262d;
        border: round #30363d;
        color: #c9d1d9;
    }
    .form-row Button:hover {
        background: #30363d;
    }
    DataTable {
        height: 1fr;
        min-height: 5;
        max-height: 12;
        border: round #21262d;
        background: #161b22;
        margin: 0;
    }
    DataTable > .datatable--header {
        background: #161b22;
        color: #8b949e;
        text-style: bold;
        border-bottom: heavy #30363d;
    }
    RichLog {
        height: 1fr;
        min-height: 10;
        border: round #21262d;
        background: #11161d;
        padding: 1 2;
        margin: 1 0;
    }
    #copilot-shell {
        height: 1fr;
        width: 100%;
        padding: 0;
    }
    #copilot-context-bar {
        display: none;
        height: 0;
        margin: 0;
        padding: 0;
    }
    #copilot-chat-scroll {
        height: 1fr;
        border: round #263040;
        background: #0d1117;
        padding: 1 2;
        margin: 0;
    }
    .claude-welcome-card {
        height: auto;
        border-left: thick #d97757;
        background: #161b22;
        padding: 1 2;
        margin: 0 0 1 0;
    }
    .claude-user-msg {
        height: auto;
        border-left: thick #58a6ff;
        background: #161f2c;
        padding: 0 2;
        margin: 1 0;
    }
    .claude-thinking-msg {
        height: auto;
        border-left: thick #d97757;
        background: #1c1817;
        color: #f0b89e;
        padding: 0 2;
        margin: 0 0 1 0;
    }
    .claude-tool-pill {
        height: auto;
        border: round #238636;
        background: #0f2419;
        color: #7ee787;
        padding: 0 2;
        margin: 0 0 1 0;
    }
    .claude-assistant-card {
        height: auto;
        border-left: thick #d97757;
        background: #161b22;
        padding: 1 2;
        margin: 0 0 1 0;
    }
    #copilot-bottom-dock {
        height: auto;
        dock: bottom;
        padding: 0;
        margin: 1 0 0 0;
    }
    #copilot-chips-row {
        height: auto;
        margin: 0 0 1 0;
        align: left middle;
    }
    .claude-chip {
        width: auto;
        min-width: 14;
        margin: 0 1 0 0;
        background: #21262d;
        border: round #30363d;
        color: #8b949e;
    }
    .claude-chip:hover {
        color: #58a6ff;
        border: round #58a6ff;
    }
    #copilot-prompt-bar {
        height: auto;
        margin: 0;
        align: left middle;
    }
    #copilot-prompt-prefix {
        width: auto;
        padding: 0 1;
        color: #d97757;
        text-style: bold;
    }
    #copilot-prompt-bar Input {
        width: 1fr;
        margin: 0 1 0 0;
        background: #161b22;
        border: round #30363d;
    }
    #copilot-prompt-bar Button {
        width: auto;
        min-width: 14;
        margin: 0;
    }
    #resize-warning-overlay {
        display: none;
        dock: top;
        layer: overlay;
        width: 100%;
        height: 100%;
        background: #0d1117;
        color: #d29922;
        content-align: center middle;
        text-align: center;
        border: heavy #d29922;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_data", "Refresh"),
        Binding("1", "switch_tab('tab-dashboard')", "1:Dash", show=True),
        Binding("2", "switch_tab('tab-timeline')", "2:Timeline", show=True),
        Binding("3", "switch_tab('tab-orders')", "3:Orders", show=True),
        Binding("4", "switch_tab('tab-disputes')", "4:Disputes", show=True),
        Binding("5", "switch_tab('tab-copilot')", "5:Pulse AI", show=True),
        Binding("question_mark", "show_help", "Help (?)", show=True),
        Binding("L", "switch_to_live", "Live Setup", show=False),
        Binding("d", "switch_tab('tab-dashboard')", "Dashboard", show=False),
        Binding("t", "switch_tab('tab-timeline')", "Timeline", show=False),
        Binding("o", "switch_tab('tab-orders')", "Orders", show=False),
        Binding("x", "switch_tab('tab-disputes')", "Disputes", show=False),
        Binding("a", "switch_tab('tab-copilot')", "Pulse AI", show=False),
        Binding("j", "vim_down", "Down", show=False),
        Binding("k", "vim_up", "Up", show=False),
    ]

    def __init__(
        self,
        settings: PulseSettings | None = None,
        store: PulseStore | None = None,
        pp_client: PayPalClient | None = None,
        demo_mode: bool = False,
    ) -> None:
        super().__init__()
        self.settings = settings or get_settings(demo_mode=demo_mode)
        if demo_mode:
            self.settings.demo_mode = True
        self.demo_mode = self.settings.demo_mode
        self.store = store or PulseStore(self.settings.db_path)
        if not self.store.is_seeded():
            self.store.seed_telemetry()
        self.pp_client = pp_client or PayPalClient(self.settings)
        self._auth_result: PayPalAuthResult | None = None
        self._git_tags_loaded: bool = False
        self._turn_counter: int = 0
        self._last_order_approve_url: str | None = None
        if self.demo_mode:
            self.SUB_TITLE = "DEMO MODE (Sample Telemetry) — Press 'L' for Live Setup"

    def action_switch_to_live(self) -> None:
        """Inform user how to connect live PayPal credentials."""
        self.notify(
            "To connect live PayPal Sandbox credentials, exit the TUI (press 'q') and run 'pp setup'.",
            title="Switch to Live Mode",
            severity="information",
            timeout=8.0,
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="resize-warning-overlay")
        yield Static(id="global-context-bar")
        with TabbedContent(initial="tab-dashboard", id="main-tabs"):
            with TabPane("[1] Dashboard", id="tab-dashboard"):
                with Vertical(id="dash-container"):
                    yield Static(id="dash-status-banner")
                    yield Static(id="dash-dispute-alert", classes="alert-banner")
                    with Horizontal(classes="kpi-row"):
                        yield Static(id="kpi-net", classes="kpi-card")
                        yield Static(id="kpi-watch", classes="kpi-card")
                        yield Static(id="kpi-health", classes="kpi-card")
                    yield Static(id="dash-sparkline", classes="section-box")
                    yield DataTable(id="currency-table")

            with TabPane("[2] Timeline", id="tab-timeline"):
                with VerticalScroll():
                    yield Static(
                        "[bold #58a6ff]Release-to-Revenue Correlation Engine[/bold #58a6ff]\n"
                        "[dim]Correlates Git tags & software releases with real PayPal conversions, fee drag, and buyer disputes.[/dim]",
                        classes="section-box",
                    )
                    yield DataTable(id="timeline-table")
                    yield Static(id="timeline-diagnostics", classes="section-box")

            with TabPane("[3] Orders", id="tab-orders"):
                with Vertical():
                    yield Static(
                        "[bold #58a6ff]Create Real PayPal Sandbox Order (POST /v2/checkout/orders)[/bold #58a6ff]\n"
                        "[dim]Mints a live PayPal Sandbox Order ID and records it immediately into the Dual-Engine SQLite ledger.[/dim]",
                        classes="section-box",
                    )
                    with Horizontal(classes="form-row"):
                        yield Input(value="9.99", placeholder="Amount (e.g. 9.99)", id="order-amount")
                        yield Input(value="USD", placeholder="Currency (USD/EUR/PHP)", id="order-currency")
                        yield Input(
                            value="PulseCore Pro Developer License (v1.2.1)",
                            placeholder="Description",
                            id="order-desc",
                        )
                        yield Button("Create Live Sandbox Order", variant="primary", id="btn-create-order")
                        yield Button("🌐 Open in Browser", variant="default", id="btn-open-order-browser", disabled=True)
                    yield RichLog(id="order-log", highlight=True, markup=True, wrap=True)

            with TabPane("[4] Disputes", id="tab-disputes"):
                with Vertical():
                    yield Static(
                        "[bold #f85149]Active Customer Disputes & Automated Evidence Drafter[/bold #f85149]\n"
                        "[dim]Bi-Directional PayPal Sandbox Bridge • Automated AI Evidence Packaging & Real-Time Chargeback Remediation[/dim]",
                        classes="section-box",
                    )
                    yield DataTable(id="disputes-table")
                    with Horizontal(classes="form-row"):
                        yield Input(
                            value="PP-D-90411-DE",
                            placeholder="Dispute ID (e.g. PP-D-90411-DE) — Press Enter to Resolve",
                            id="dispute-id-input",
                        )
                        yield Button(
                            "Draft AI Evidence & Resolve",
                            variant="warning",
                            id="btn-resolve-dispute",
                        )
                        yield Button(
                            "⚡ Simulate Incoming Dispute",
                            variant="error",
                            id="btn-simulate-dispute",
                        )
                        yield Button(
                            "🔄 Sync Sandbox API",
                            variant="default",
                            id="btn-sync-disputes",
                        )
                    yield RichLog(id="dispute-log", highlight=True, markup=True, wrap=True)

            with TabPane("[5] Pulse AI", id="tab-copilot"):
                with Vertical(id="copilot-shell"):
                    yield Static(id="copilot-context-bar")
                    with VerticalScroll(id="copilot-chat-scroll"):
                        yield Static(
                            "[bold #d97757]● Pulse[/bold #d97757] [dim]— PayPal Pulse Financial Intelligence[/dim]\n"
                            "Ask anything about your 30-day multi-currency ledger, release conversion velocity, "
                            "or German v1.2.1 buyer disputes — or instruct Pulse to mint live PayPal Sandbox orders & invoices.",
                            classes="claude-welcome-card",
                        )
                    with Vertical(id="copilot-bottom-dock"):
                        with Horizontal(id="copilot-chips-row"):
                            yield Button(
                                "\\[Why did DE dispute v1.2.1?]",
                                id="chip-disputes",
                                classes="claude-chip prompt-chip",
                            )
                            yield Button(
                                "\\[Who is our core userbase?]",
                                id="chip-userbase",
                                classes="claude-chip prompt-chip",
                            )
                            yield Button(
                                "\\[True net margin]",
                                id="chip-margin",
                                classes="claude-chip prompt-chip",
                            )
                            yield Button(
                                "\\[+ Mint $9.99 Order]",
                                id="chip-order",
                                classes="claude-chip prompt-chip",
                            )
                        with Horizontal(id="copilot-prompt-bar"):
                            yield Static("❯", id="copilot-prompt-prefix")
                            yield Input(
                                placeholder="Ask Pulse anything about your revenue, releases, or type an action...",
                                id="copilot-input",
                            )
                            yield Button("Send ↵", variant="success", id="btn-ask-copilot")
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()
        self._populate_all()
        order_log = self.query_one("#order-log", RichLog)
        order_log.write(
            "[dim]Ready to mint live PayPal Sandbox Orders (`POST /v2/checkout/orders`). Press Enter in any field above or click the button.[/dim]"
        )
        dispute_log = self.query_one("#dispute-log", RichLog)
        dispute_log.write(
            "[dim]Enter a Dispute ID (e.g. `PP-D-90411-DE`) above and press Enter to generate Pulse AI evidence and a replacement Sandbox order.[/dim]"
        )
        # Check background updates (such as pp trigger) every 3 seconds
        self.set_interval(3.0, self.action_refresh_data)

    def on_resize(self, event: events.Resize) -> None:
        """Protect layout and show minimum-dimension warning on viewports below 80x24."""
        is_too_small = event.size.width < 80 or event.size.height < 24
        try:
            overlay = self.query_one("#resize-warning-overlay", Static)
            overlay.display = is_too_small
            if is_too_small:
                overlay.update(
                    f"\n\n[bold red]⚠ TERMINAL VIEWPORT TOO SMALL[/bold red]\n\n"
                    f"Current: [bold yellow]{event.size.width} cols × {event.size.height} rows[/bold yellow]\n"
                    f"Minimum: [bold green]80 cols × 24 rows[/bold green]\n\n"
                    f"[dim]Please expand your terminal window or decrease font size.[/dim]"
                )
        except Exception:
            pass

    def action_vim_down(self) -> None:
        """Scroll active table down with Vim 'j' if input field is not active."""
        if self.focused and isinstance(self.focused, Input):
            return
        active_tab = self.query_one("#main-tabs", TabbedContent).active
        table_id = (
            "#currency-table"
            if active_tab == "tab-dashboard"
            else ("#timeline-table" if active_tab == "tab-timeline" else ("#disputes-table" if active_tab == "tab-disputes" else None))
        )
        if table_id:
            try:
                tbl = self.query_one(table_id, DataTable)
                tbl.action_cursor_down()
            except Exception:
                pass

    def action_vim_up(self) -> None:
        """Scroll active table up with Vim 'k' if input field is not active."""
        if self.focused and isinstance(self.focused, Input):
            return
        active_tab = self.query_one("#main-tabs", TabbedContent).active
        table_id = (
            "#currency-table"
            if active_tab == "tab-dashboard"
            else ("#timeline-table" if active_tab == "tab-timeline" else ("#disputes-table" if active_tab == "tab-disputes" else None))
        )
        if table_id:
            try:
                tbl = self.query_one(table_id, DataTable)
                tbl.action_cursor_up()
            except Exception:
                pass

    def _setup_tables(self) -> None:
        cur_table = self.query_one("#currency-table", DataTable)
        cur_table.add_columns(
            "Currency", "Tx Count", "Gross", "PayPal Fees", "Fee Drag %", "True Net", "Net Margin %", "USD Equiv Net"
        )

        tl_table = self.query_one("#timeline-table", DataTable)
        tl_table.add_columns(
            "Version", "Commit", "Released", "Customers", "Paid", "Conv %", "True Net (USD)", "Refunds", "Disputes", "Status"
        )

        disp_table = self.query_one("#disputes-table", DataTable)
        disp_table.add_columns(
            "Dispute ID", "Status", "Country", "Platform / SKU", "Release", "Amount", "Due Date", "Reason"
        )

    def _update_auth_banner(self, auth: PayPalAuthResult | None, open_disputes_count: int | None = None) -> None:
        if self.demo_mode:
            sb_badge = "[bold #d29922]●[/bold #d29922] [bold #e3b341]DEMO MODE (Sample Telemetry)[/bold #e3b341]"
        elif auth is None:
            sb_badge = "[bold #58a6ff]●[/bold #58a6ff] [bold #c9d1d9]Sandbox: Connecting...[/bold #c9d1d9]"
        elif auth.authenticated:
            lat_str = f"{auth.latency_ms / 1000:.1f}s" if auth.latency_ms >= 1000 else f"{auth.latency_ms:.0f}ms"
            sb_badge = f"[#3fb950]●[/#3fb950] [bold #c9d1d9]Sandbox: Connected[/bold #c9d1d9] [dim]({lat_str})[/dim]"
        else:
            sb_badge = "[#8b949e]●[/#8b949e] [bold #c9d1d9]Sandbox: Offline[/bold #c9d1d9]"

        if open_disputes_count is None:
            open_disputes_count = len(self.store.get_disputes(status_filter="OPEN"))

        lat_val = auth.latency_ms if auth and auth.authenticated else 180.0
        conn_val = auth.authenticated if auth else False
        summary_peek = self.store.get_dashboard_summary(api_latency_ms=lat_val, api_connected=conn_val)
        score_val = summary_peek["health_score"]
        rating_str = summary_peek.get("health_rating", "HEALTHY")

        disp_status = f"[bold #f85149]{open_disputes_count} Open[/bold #f85149]" if open_disputes_count > 0 else "[#3fb950]0 Open[/#3fb950]"

        left_content = (
            f" {sb_badge}  [dim]│[/dim]  "
            f"[dim]Health:[/dim] [bold #58a6ff]{score_val}/100[/bold #58a6ff] [dim]({rating_str})[/dim]  [dim]│[/dim]  "
            f"[dim]Ledger:[/dim] [bold #c9d1d9]30d Synced[/bold #c9d1d9]"
        )
        right_content = (
            f"[dim]Releases:[/dim] [bold #c9d1d9]v1.1.0..v1.2.1[/bold #c9d1d9]  [dim]│[/dim]  "
            f"[dim]Disputes:[/dim] {disp_status} "
        )

        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right", ratio=1)
        grid.add_row(left_content, right_content)

        for bar_id in ("#global-context-bar", "#copilot-context-bar"):
            try:
                self.query_one(bar_id, Static).update(grid)
            except Exception:
                pass

    @work(thread=True, exclusive=True, group="auth-check")
    def _run_auth_check_worker(self, force_refresh: bool = False) -> None:
        auth_res = self.pp_client.authenticate(force_refresh=force_refresh)
        self._auth_result = auth_res
        self.call_from_thread(self._update_auth_banner, auth_res)

    def _populate_all(self) -> None:
        lat_val = self._auth_result.latency_ms if self._auth_result and self._auth_result.authenticated else 180.0
        conn_val = self._auth_result.authenticated if self._auth_result else False
        summary = self.store.get_dashboard_summary(api_latency_ms=lat_val, api_connected=conn_val)

        # Non-blocking auth check: use cached memory state or disk token cache; otherwise authenticate on background thread
        if self._auth_result is None:
            if not self.settings.has_paypal_credentials or self.pp_client._load_cached_token() is not None:
                self._auth_result = self.pp_client.authenticate(force_refresh=False)
            else:
                self._run_auth_check_worker(force_refresh=False)

        open_cnt = summary["open_disputes_count"]
        self._update_auth_banner(self._auth_result, open_disputes_count=open_cnt)

        if open_cnt > 0:
            self.query_one("#dash-dispute-alert", Static).display = True
            self.query_one("#dash-dispute-alert", Static).update(
                f"[bold #f85149]⚠ ACTION REQUIRED:[/bold #f85149] {open_cnt} open buyer disputes on release [bold #f0f6fc]v1.2.1 (DE)[/bold #f0f6fc] "
                f"[dim]• Press 4 to inspect and auto-draft evidence with Pulse AI[/dim]"
            )
        else:
            self.query_one("#dash-dispute-alert", Static).display = False

        self.query_one("#kpi-net", Static).update(
            f"[bold #8b949e]TRUE NET TAKE-HOME[/bold #8b949e]\n"
            f"[bold #3fb950]${summary['total_net_usd']:,.2f}[/bold #3fb950] [dim]({summary['net_margin_pct']}% margin)[/dim]\n"
            f"[dim]Gross:[/dim] [bold #f0f6fc]${summary['total_gross_usd']:,.2f}[/bold #f0f6fc]  [dim]Fee Drag:[/dim] [bold #8b949e]-${summary['total_fees_usd']:,.2f} ({summary['fee_drag_pct']}%)[/bold #8b949e]"
        )

        cust_count = summary.get("total_customers", summary.get("total_installs", 0))
        active_cust = summary.get("active_customers", summary.get("active_users", 0))
        paid_orders = summary.get("total_orders", summary.get("total_paid", 0))

        self.query_one("#kpi-watch", Static).update(
            f"[bold #8b949e]CUSTOMERS & SALES[/bold #8b949e]\n"
            f"[bold #f0f6fc]{cust_count:,}[/bold #f0f6fc] [dim]Users ({active_cust:,} active)[/dim]\n"
            f"[dim]Orders:[/dim] [bold #f0f6fc]{paid_orders:,}[/bold #f0f6fc] [dim]({summary['overall_conversion_pct']}% conv)[/dim]  [dim]MRR:[/dim] [bold #58a6ff]${summary['mrr_usd']:,.2f}/mo[/bold #58a6ff]"
        )

        telemetry = summary.get("health_telemetry", {})
        score_val = summary["health_score"]
        rating_str = summary.get("health_rating", "HEALTHY")
        self.query_one("#kpi-health", Static).update(
            f"[bold #8b949e]CFO HEALTH SCORE[/bold #8b949e]\n"
            f"[bold #58a6ff]{score_val} / 100[/bold #58a6ff] [dim][{rating_str}][/dim]\n"
            f"[dim]Dispute Ratio:[/dim] [bold #f0f6fc]{telemetry.get('dispute_ratio_pct', 0.26)}%[/bold #f0f6fc]  [dim]API Latency:[/dim] [bold #f0f6fc]{lat_val:.0f}ms[/bold #f0f6fc]"
        )

        daily = summary["daily_14"]
        net_vals = [float(d["net_usd"]) for d in daily] or [0.0]
        inst_vals = [float(d["installs"]) for d in daily] or [0.0]
        self.query_one("#dash-sparkline", Static).update(
            f"[bold #8b949e]14-Day True Net Velocity:[/bold #8b949e] [bold #58a6ff]{_build_sparkline(net_vals)}[/bold #58a6ff]  "
            f"[dim](${min(net_vals):.0f}..${max(net_vals):.0f}/day)[/dim]\n"
            f"[bold #8b949e]14-Day Customer Acquisition:[/bold #8b949e] [bold #8b949e]{_build_sparkline(inst_vals)}[/bold #8b949e]  "
            f"[dim]({int(min(inst_vals))}..{int(max(inst_vals))} users/day)[/dim]"
        )

        cur_table = self.query_one("#currency-table", DataTable)
        cur_table.clear()
        for c in summary["currencies"]:
            cur_table.add_row(
                c["currency"],
                str(c["tx_count"]),
                f"{c['gross']:,.2f}",
                f"-{c['fees']:,.2f}",
                f"{c['fee_pct']}%",
                f"{c['net']:,.2f}",
                f"{c['net_margin_pct']}%",
                f"${c['usd_net']:,.2f}",
            )

        tl_table = self.query_one("#timeline-table", DataTable)
        tl_table.clear()
        diag_lines = []
        releases = self.store.get_releases(include_git_tags=not self._git_tags_loaded)
        self._git_tags_loaded = True
        for r in releases:
            cust_val = r.get("customers", r.get("installs", 0))
            order_val = r.get("paid_orders", r.get("paid_conversions", 0))
            tl_table.add_row(
                r["version"],
                r["git_commit"],
                r["released_at"],
                f"{cust_val:,}",
                f"{order_val:,}",
                f"{r['conversion_rate']}%",
                f"${r['net_usd']:,.2f}",
                str(r["refund_count"]),
                str(r["dispute_count"]),
                r["status_badge"],
            )
            diag_lines.append(f"• [bold]{r['version']}[/bold] ({r['title']}): {r['ai_diagnostic']}")
        self.query_one("#timeline-diagnostics", Static).update("\n".join(diag_lines))

        disp_table = self.query_one("#disputes-table", DataTable)
        disp_table.clear()
        for d in summary["disputes"]:
            sku_entry = d.get("platform_sku", d.get("device_model", ""))
            disp_table.add_row(
                d["dispute_id"],
                d["status"],
                d["buyer_country"],
                sku_entry,
                d["release_version"],
                f"{d['amount']:.2f} {d['currency']}",
                d["due_date"],
                d["reason"],
            )

    def action_refresh_data(self) -> Worker[None]:
        return self._run_refresh_worker()

    @work(thread=True, exclusive=True, group="refresh")
    def _run_refresh_worker(self) -> None:
        self._auth_result = self.pp_client.authenticate(force_refresh=False)
        self.call_from_thread(self._populate_all)

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one("#main-tabs", TabbedContent).active = tab_id

    def action_show_help(self) -> None:
        """Display the interactive keyboard shortcuts modal dialog."""
        self.push_screen(HelpModal())

    @on(Button.Pressed, "#btn-create-order")
    @on(Input.Submitted, "#order-amount")
    @on(Input.Submitted, "#order-currency")
    @on(Input.Submitted, "#order-desc")
    def handle_create_order(self) -> Worker[None] | None:
        amt_str = self.query_one("#order-amount", Input).value.strip()
        cur_str = self.query_one("#order-currency", Input).value.strip().upper() or "USD"
        desc_str = self.query_one("#order-desc", Input).value.strip() or "PulseCore Pro Developer License"
        log = self.query_one("#order-log", RichLog)
        btn = self.query_one("#btn-create-order", Button)

        try:
            amt = float(amt_str)
        except ValueError:
            log.write("[bold red]Invalid amount. Please enter a numeric value (e.g. 9.99).[/bold red]")
            return None

        btn.disabled = True
        log.write(f"[cyan]● Minting PayPal Sandbox Order for {amt:.2f} {cur_str}...[/cyan]")
        return self._run_create_order_worker(amt, cur_str, desc_str)

    @work(thread=True, exclusive=True, group="create-order")
    def _run_create_order_worker(self, amt: float, cur_str: str, desc_str: str) -> None:
        try:
            res = self.pp_client.create_order(
                amount=amt,
                currency=cur_str,
                description=desc_str,
            )
            if res.success:
                self.store.record_live_order(res, "v1.2.1", "US")
            self.call_from_thread(self._finish_create_order_ui, res)
        except Exception as exc:
            self.call_from_thread(self._finish_create_order_error, str(exc))

    def _finish_create_order_ui(self, res: Any) -> None:
        log = self.query_one("#order-log", RichLog)
        btn = self.query_one("#btn-create-order", Button)
        btn.disabled = False
        if res.success:
            self._last_order_approve_url = res.approve_url
            browser_btn = self.query_one("#btn-open-order-browser", Button)
            browser_btn.disabled = False
            log.write(
                f"[bold green]✓ Live PayPal Sandbox Order Created![/bold green]\n"
                f"  • Order ID: [bold]{res.order_id}[/bold]\n"
                f"  • Status: {res.status}\n"
                f"  • Approval URL: [bold underline cyan]{res.approve_url}[/bold underline cyan]\n"
                f"  [dim]👉 Click the '[bold]🌐 Open in Browser[/bold]' button above to checkout immediately![/dim]"
            )
            self._populate_all()
        else:
            log.write(f"[bold red]✗ Order creation failed:[/bold red] {res.error}")

    @on(Button.Pressed, "#btn-open-order-browser")
    def handle_open_order_browser(self) -> None:
        log = self.query_one("#order-log", RichLog)
        if self._last_order_approve_url:
            log.write(f"[green]Opening default browser to:[/green] {self._last_order_approve_url}")
            webbrowser.open(self._last_order_approve_url)
        else:
            log.write("[yellow]No active order approval URL to open yet.[/yellow]")

    def _finish_create_order_error(self, err: str) -> None:
        log = self.query_one("#order-log", RichLog)
        btn = self.query_one("#btn-create-order", Button)
        btn.disabled = False
        log.write(f"[bold red]✗ Order creation failed:[/bold red] {err}")

    @on(Button.Pressed, "#btn-resolve-dispute")
    @on(Input.Submitted, "#dispute-id-input")
    def handle_resolve_dispute(self) -> Worker[None] | None:
        dispute_id = self.query_one("#dispute-id-input", Input).value.strip()
        log = self.query_one("#dispute-log", RichLog)
        btn = self.query_one("#btn-resolve-dispute", Button)
        dispute = self.store.get_dispute_by_id(dispute_id)
        if not dispute:
            log.write(f"[bold red]Dispute ID '{dispute_id}' not found.[/bold red]")
            return None

        btn.disabled = True
        log.write(f"[cyan]● Resolving {dispute_id} via PayPal Sandbox & Pulse AI...[/cyan]")
        return self._run_resolve_dispute_worker(dispute_id, dispute)

    @work(thread=True, exclusive=True, group="resolve-dispute")
    def _run_resolve_dispute_worker(self, dispute_id: str, dispute: dict[str, Any]) -> None:
        try:
            sku_val = dispute.get("platform_sku", dispute.get("device_model", "Ubuntu Linux 24.04 (CLI)"))
            sub_res = self.pp_client.submit_dispute_resolution(
                dispute_id=dispute_id,
                evidence_text=f"Automated remediation hotfix v1.2.2 deployed for {sku_val}",
                amount=float(dispute["amount"]),
                currency=dispute["currency"],
            )
            rep_order = sub_res["replacement_order"]
            evidence_text, model_used = draft_dispute_evidence(dispute, rep_order, self.settings)
            order_id = rep_order.order_id if rep_order.success else "LOCAL-PATCH-122"
            if rep_order.success:
                self.store.record_live_order(
                    rep_order,
                    release_version="v1.2.1",
                    country_code=dispute["buyer_country"],
                    platform_sku=sku_val,
                )
            self.store.mark_dispute_resolved(dispute_id, evidence_text, order_id)
            self.call_from_thread(self._finish_resolve_dispute_ui, dispute_id, order_id, model_used, evidence_text)
        except Exception as exc:
            self.call_from_thread(self._finish_resolve_dispute_error, str(exc))

    def _finish_resolve_dispute_ui(
        self,
        dispute_id: str,
        order_id: str,
        model_used: str,
        evidence_text: str,
    ) -> None:
        log = self.query_one("#dispute-log", RichLog)
        btn = self.query_one("#btn-resolve-dispute", Button)
        btn.disabled = False
        log.write(
            f"[bold green]✓ Dispute {dispute_id} Marked RESOLVED![/bold green]\n"
            f"  • Replacement Sandbox Order ID: [bold]{order_id}[/bold]\n"
            f"  • Engine: {model_used}\n"
            f"  • Evidence Draft:\n{evidence_text}"
        )
        self._populate_all()

    def _finish_resolve_dispute_error(self, err: str) -> None:
        log = self.query_one("#dispute-log", RichLog)
        btn = self.query_one("#btn-resolve-dispute", Button)
        btn.disabled = False
        log.write(f"[bold red]✗ Dispute resolution failed:[/bold red] {err}")

    @on(Button.Pressed, "#btn-simulate-dispute")
    def handle_simulate_dispute(self) -> None:
        sim_disp = self.store.simulate_incoming_dispute()
        log = self.query_one("#dispute-log", RichLog)
        disp_id = sim_disp.get("dispute_id", "PP-D-SIM")
        log.write(
            f"[bold #f85149]⚡ Simulated Real-World Chargeback Event:[/bold #f85149] [bold yellow]{disp_id}[/bold yellow]\n"
            f"  • Claim: {sim_disp.get('description')}\n"
            f"  • Amount: {sim_disp.get('amount', 5.99):.2f} {sim_disp.get('currency', 'EUR')} ({sim_disp.get('platform_sku')})\n"
            f"  • Impact: CFO Merchant Health Index dynamically penalized; active dispute surge triggered!\n"
            f"  [dim]👉 Input pre-filled with '{disp_id}'. Press 'Draft AI Evidence & Resolve' to remediate.[/dim]"
        )
        self.query_one("#dispute-id-input", Input).value = disp_id
        self._populate_all()

    @on(Button.Pressed, "#btn-sync-disputes")
    def handle_sync_disputes(self) -> Worker[None]:
        log = self.query_one("#dispute-log", RichLog)
        log.write("[cyan]● Polling PayPal Sandbox Disputes API (GET /v1/customer/disputes)...[/cyan]")
        btn = self.query_one("#btn-sync-disputes", Button)
        btn.disabled = True
        return self._run_sync_disputes_worker()

    @work(thread=True, exclusive=True, group="sync-disputes")
    def _run_sync_disputes_worker(self) -> None:
        try:
            live_res = self.pp_client.get_disputes()
            added = self.store.sync_external_disputes(live_res.get("items", []))
            self.call_from_thread(self._finish_sync_disputes_ui, live_res, added)
        except Exception as exc:
            self.call_from_thread(self._finish_sync_disputes_error, str(exc))

    def _finish_sync_disputes_ui(self, live_res: dict[str, Any], added: int) -> None:
        log = self.query_one("#dispute-log", RichLog)
        btn = self.query_one("#btn-sync-disputes", Button)
        btn.disabled = False
        if live_res.get("ok"):
            count = len(live_res.get("items", []))
            log.write(
                f"[bold green]✓ PayPal Sandbox Disputes API Synced (HTTP 200 OK)[/bold green]\n"
                f"  • Live Disputes from Sandbox: {count} ({added} new ingested into SQLite ledger)\n"
                f"  • Real-Time Gateway Status: Connected"
            )
        else:
            log.write(f"[yellow]⚠ Sandbox Disputes API returned:[/yellow] {live_res.get('error', 'No response')}")
        self._populate_all()

    def _finish_sync_disputes_error(self, err: str) -> None:
        log = self.query_one("#dispute-log", RichLog)
        btn = self.query_one("#btn-sync-disputes", Button)
        btn.disabled = False
        log.write(f"[bold red]✗ Dispute API sync failed:[/bold red] {err}")

    @on(Button.Pressed, ".prompt-chip")
    def handle_prompt_chip(self, event: Button.Pressed) -> Worker[None] | None:
        chip_id = event.button.id or ""
        prompt_text = QUICK_PROMPT_MAP.get(chip_id)
        if prompt_text:
            inp = self.query_one("#copilot-input", Input)
            inp.value = prompt_text
            return self.handle_ask_copilot()
        return None

    @on(Button.Pressed, "#btn-ask-copilot")
    @on(Input.Submitted, "#copilot-input")
    def handle_ask_copilot(self) -> Worker[None] | None:
        inp = self.query_one("#copilot-input", Input)
        q = inp.value.strip()
        chat_scroll = self.query_one("#copilot-chat-scroll", VerticalScroll)
        if not q:
            return None

        inp.value = ""
        self._turn_counter += 1
        turn_id = self._turn_counter

        user_widget = Static(
            Text.assemble(("❯ ", "bold #58a6ff"), (q, "bold white")),
            classes="claude-user-msg",
        )
        thinking_widget = Static(
            "[bold #d97757]● Pulse is thinking...[/bold #d97757] [dim](analyzing PayPal ledger & release telemetry)[/dim]",
            id=f"thinking-{turn_id}",
            classes="claude-thinking-msg",
        )
        chat_scroll.mount(user_widget)
        chat_scroll.mount(thinking_widget)
        chat_scroll.scroll_end(animate=False)

        return self._run_ask_copilot_worker(q, turn_id)

    @work(thread=True, exclusive=False, group="ask-copilot")
    def _run_ask_copilot_worker(self, question: str, turn_id: int) -> None:
        try:
            res = ask_copilot(
                question,
                store=self.store,
                paypal_client=self.pp_client,
                settings=self.settings,
            )
            self.call_from_thread(self._finish_ask_copilot_ui, res, turn_id)
        except Exception as exc:
            self.call_from_thread(self._finish_ask_copilot_error, str(exc), turn_id)

    def _finish_ask_copilot_ui(self, res: CopilotAnswer, turn_id: int) -> None:
        chat_scroll = self.query_one("#copilot-chat-scroll", VerticalScroll)
        for node in chat_scroll.query(f"#thinking-{turn_id}"):
            node.remove()

        if res.executed_action:
            tool_name = "paypal.orders.create"
            if res.created_invoice and res.created_invoice.success:
                tool_name = "paypal.invoicing.create + paypal.orders.create"
            tool_widget = Static(
                f"[bold #7ee787]⚡ Tool: {tool_name}[/bold #7ee787] -> {res.executed_action}",
                classes="claude-tool-pill",
            )
            chat_scroll.mount(tool_widget)
            self._populate_all()

        sec = res.latency_ms / 1000.0
        header = Text.from_markup("[bold #d97757]● Pulse[/bold #d97757]")
        footer = Text.from_markup(
            f"\n[dim]{sec:.2f}s • {res.tokens_per_sec:.0f} tok/s • PayPal Ledger + Release Telemetry[/dim]"
        )
        card = Static(
            Group(header, Markdown(res.answer), footer),
            classes="claude-assistant-card",
        )
        chat_scroll.mount(card)
        chat_scroll.scroll_end(animate=False)

    def _finish_ask_copilot_error(self, err: str, turn_id: int) -> None:
        chat_scroll = self.query_one("#copilot-chat-scroll", VerticalScroll)
        for node in chat_scroll.query(f"#thinking-{turn_id}"):
            node.remove()
        err_card = Static(
            f"[bold red]● Pulse Error:[/bold red] {err}",
            classes="claude-assistant-card",
        )
        chat_scroll.mount(err_card)
        chat_scroll.scroll_end(animate=False)

