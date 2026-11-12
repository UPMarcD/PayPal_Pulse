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
    Select,
    Static,
    TabbedContent,
    TabPane,
)
from textual.worker import Worker

from pulse.ai_copilot import (
    CopilotAnswer,
    ask_copilot,
    diagnose_dispute_and_remediate,
    draft_dispute_evidence,
    get_active_groq_model,
    verify_groq_connection,
)
from pulse.config import PulseSettings, get_settings, save_global_config
from pulse.mascot import format_mascot_header
from pulse.paypal_client import PayPalAuthResult, PayPalClient
from pulse.prompt_suggester import PromptSuggestion, get_session_prompt_suggestions
from pulse.store import (
    ZERO_DECIMAL_CURRENCIES,
    PulseStore,
    detect_project_context,
    format_currency_amount,
    parse_release_version,
)

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
            yield Static("PayPal Pulse — Keyboard Shortcuts & Navigation", id="help-title")
            yield Static("[bold #e5a824]Tabs & Views[/bold #e5a824]")
            yield Static("  [bold #f0f6fc]1[/bold #f0f6fc] or [bold #f0f6fc]d[/bold #f0f6fc]    Dashboard & 30-Day Financial KPIs")
            yield Static("  [bold #f0f6fc]2[/bold #f0f6fc] or [bold #f0f6fc]t[/bold #f0f6fc]    Git Release-to-Revenue Correlation Timeline")
            yield Static("  [bold #f0f6fc]3[/bold #f0f6fc] or [bold #f0f6fc]o[/bold #f0f6fc]    Live PayPal Sandbox Orders & Invoicing")
            yield Static("  [bold #f0f6fc]4[/bold #f0f6fc] or [bold #f0f6fc]x[/bold #f0f6fc]    Customer Disputes & Evidence Drafter")
            yield Static("  [bold #f0f6fc]5[/bold #f0f6fc] or [bold #f0f6fc]a[/bold #f0f6fc]    Pulse AI Copilot Interactive Log")
            yield Static("  [bold #f0f6fc]6[/bold #f0f6fc] or [bold #f0f6fc]s[/bold #f0f6fc]    Engine Settings & BYOK AI Configuration\n")
            yield Static("[bold #e5a824]Actions & Navigation[/bold #e5a824]")
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
        background: #0a101d;
        color: #c9d1d9;
        overflow: hidden;
    }
    #global-context-bar {
        height: auto;
        min-height: 1;
        border-bottom: solid #1c2e4a;
        background: #111c2e;
        padding: 0 1;
        margin: 0;
        color: #8b9cb0;
    }
    TabbedContent {
        background: #0a101d;
        height: 1fr;
    }
    Tabs {
        background: #0a101d;
        border-bottom: solid #1c2e4a;
        height: 3;
    }
    Tab {
        background: #0a101d;
        color: #8b9cb0;
        padding: 0 2;
    }
    Tab:hover {
        color: #c9d1d9;
        background: #111c2e;
    }
    Tab.-active {
        color: #f0f6fc;
        text-style: bold;
        background: #111c2e;
    }
    Underline {
        color: #e5a824;
    }
    TabPane {
        background: #0a101d;
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
        border: round #1c2e4a;
        background: #111c2e;
        padding: 0 1;
        margin: 0 1 0 0;
    }
    #kpi-health {
        margin: 0;
    }
    .kpi-card:hover {
        border: round #00a8ea;
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
        border: round #1c2e4a;
        background: #111c2e;
    }
    .section-box {
        border: round #1c2e4a;
        background: #111c2e;
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
        background: #111c2e;
        border: round #1c2e4a;
        color: #e6edf3;
    }
    .form-row Input:focus {
        border: round #00a8ea;
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
        border-left: thick #e5a824;
        background: #111c2e;
        padding: 1 2;
        margin: 0 0 1 0;
    }
    .claude-user-msg {
        height: auto;
        border-left: thick #00a8ea;
        background: #111c2e;
        padding: 0 2;
        margin: 1 0;
    }
    .claude-thinking-msg {
        height: auto;
        border-left: thick #e5a824;
        background: #161b22;
        color: #f5d082;
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
        border-left: thick #00a8ea;
        background: #111c2e;
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
        background: #161b22;
        border: round #21262d;
        color: #8b949e;
    }
    .claude-chip:hover {
        color: #e5a824;
        border: round #e5a824;
    }
    #copilot-prompt-bar {
        height: auto;
        margin: 0;
        align: left middle;
    }
    #copilot-prompt-prefix {
        width: auto;
        padding: 0 1;
        color: #e5a824;
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
    #settings-scroll {
        height: 1fr;
        padding: 0 1;
    }
    .settings-card {
        border: round #21262d;
        background: #11161d;
        padding: 1 2;
        margin: 0 0 1 0;
        height: auto;
    }
    .settings-label {
        color: #8b949e;
        margin: 1 0 0 0;
    }
    .settings-input {
        background: #161b22;
        border: round #30363d;
        margin: 0 0 1 0;
    }
    .settings-status-box {
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1;
    }
    .settings-btn-row {
        height: auto;
        margin: 1 0 0 0;
        align: left middle;
    }
    Button.action-btn,
    .settings-btn-row Button {
        height: 3;
        min-height: 3;
        max-height: 3;
        width: auto;
        min-width: 20;
        margin: 0 2 0 0;
        padding: 0 1;
        background: #21262d !important;
        border: round #38434f !important;
        color: #f0f6fc !important;
        text-style: bold;
    }
    Button.action-btn:hover,
    .settings-btn-row Button:hover {
        background: #30363d !important;
        border: round #58a6ff !important;
        color: #ffffff !important;
    }
    Button.action-btn.-error,
    .settings-btn-row Button.-error,
    #btn-reset-ledger {
        background: #da3633 !important;
        border: round #b62324 !important;
        color: #ffffff !important;
        text-style: bold;
    }
    Button.action-btn.-error:hover,
    .settings-btn-row Button.-error:hover,
    #btn-reset-ledger:hover {
        background: #b62324 !important;
        border: round #da3633 !important;
    }
    #btn-save-settings {
        height: 3;
        min-height: 3;
        max-height: 3;
        width: auto;
        min-width: 24;
        margin: 0 2 0 0;
        padding: 0 1;
        background: #238636 !important;
        border: round #3fb950 !important;
        color: #ffffff !important;
        text-style: bold;
    }
    #btn-save-settings:hover {
        background: #2ea043 !important;
        border: round #3fb950 !important;
    }
    #settings-actions-bar {
        height: auto;
        margin: 1 0 2 0;
        align: left middle;
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
        Binding("6", "switch_tab('tab-settings')", "6:Settings", show=True),
        Binding("question_mark", "show_help", "Help (?)", show=True),
        Binding("L", "switch_to_live", "Live Setup", show=False),
        Binding("s", "switch_tab('tab-settings')", "Settings", show=False),
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
        if self.store.should_auto_seed():
            self.store.seed_telemetry()
        self.pp_client = pp_client or PayPalClient(self.settings)
        self._auth_result: PayPalAuthResult | None = None
        self._git_tags_loaded: bool = False
        self._turn_counter: int = 0
        self._last_order_id: str | None = None
        self._last_order_approve_url: str | None = None
        self._last_cur_data: list[tuple] | None = None
        self._last_tl_data: list[tuple] | None = None
        self._last_disputes_data: list[tuple] | None = None
        self._populating_disputes: bool = False
        self.prompt_suggestions: list[PromptSuggestion] = get_session_prompt_suggestions(self.store)
        self.quick_prompt_map: dict[str, str] = {s.chip_id: s.full_prompt for s in self.prompt_suggestions}
        if self.demo_mode:
            self.SUB_TITLE = "DEMO MODE (Sample Telemetry) — Press '6' or 's' for Settings"

    def action_switch_to_live(self) -> None:
        """Switch directly to Settings tab."""
        self.action_switch_tab("tab-settings")

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
                    proj_ctx = detect_project_context()
                    default_cur = str(proj_ctx.get("default_currency") or "USD").upper()
                    raw_price = float(proj_ctx.get("default_price", 9.99))
                    default_amt = format_currency_amount(raw_price, default_cur)
                    default_sku = str(proj_ctx.get("default_sku") or "PulseCore Pro")
                    default_desc = f"{default_sku} License" if proj_ctx.get("config_file") else "PulseCore Pro Developer License"
                    with Horizontal(classes="form-row"):
                        yield Input(value=default_amt, placeholder="Amount (e.g. 9.99)", id="order-amount")
                        yield Input(value=default_cur, placeholder="Currency (USD/EUR/PHP/GBP/...)", id="order-currency")
                        yield Input(
                            value=proj_ctx.get("version", "v1.2.1"),
                            placeholder="Release Tag (e.g. v1.3.0)",
                            id="order-release",
                        )
                        yield Input(
                            value=default_desc,
                            placeholder="Description",
                            id="order-desc",
                        )
                        yield Button("Create Live Sandbox Order", variant="primary", id="btn-create-order")
                        yield Button("Checkout in PayPal ↗", variant="default", id="btn-open-order-browser", disabled=True)
                        yield Button("Capture Order ✓", variant="success", id="btn-capture-order", disabled=True)
                    yield RichLog(id="order-log", highlight=True, markup=True, wrap=True)

            with TabPane("[4] Disputes", id="tab-disputes"):
                with Vertical():
                    yield Static(
                        "[bold #58a6ff]Customer Disputes & Chargeback Resolution (GET /v1/customer/disputes)[/bold #58a6ff]\n"
                        "[dim]Monitors open buyer claims, drafts automated evidence packages, and settles disputes.[/dim]",
                        classes="section-box",
                    )
                    yield DataTable(id="disputes-table", cursor_type="row")
                    with Horizontal(classes="form-row"):
                        yield Input(
                            value="PP-D-90411-DE",
                            placeholder="Dispute ID (e.g. PP-D-90411-DE) — Press Enter to Resolve",
                            id="dispute-id-input",
                        )
                        yield Button(
                            "Resolve with AI",
                            variant="warning",
                            id="btn-resolve-dispute",
                        )
                        yield Button(
                            "View in PayPal ↗",
                            variant="default",
                            id="btn-open-dispute-browser",
                        )
                        yield Button(
                            "Reload",
                            variant="default",
                            id="btn-sync-disputes",
                        )
                    yield RichLog(id="dispute-log", highlight=True, markup=True, wrap=True)

            with TabPane("[5] Pulse AI", id="tab-copilot"):
                with Vertical(id="copilot-shell"):
                    yield Static(id="copilot-context-bar")
                    with VerticalScroll(id="copilot-chat-scroll"):
                        yield Static(
                            format_mascot_header(
                                title="Pulse AI Copilot",
                                subtitle="Real-time Financial Telemetry & PayPal Sandbox Bridge",
                                state="idle",
                            )
                            + "\n\nAsk anything about your 30-day multi-currency ledger, release conversion velocity, "
                            "or active buyer disputes — or instruct Pulse to mint live PayPal Sandbox orders & invoices.",
                            classes="claude-welcome-card",
                        )
                    with Vertical(id="copilot-bottom-dock"):
                        with Horizontal(id="copilot-chips-row"):
                            for suggestion in self.prompt_suggestions:
                                yield Button(
                                    suggestion.button_label,
                                    id=suggestion.chip_id,
                                    classes="claude-chip prompt-chip",
                                )
                        with Horizontal(id="copilot-prompt-bar"):
                            yield Static("❯", id="copilot-prompt-prefix")
                            yield Input(
                                placeholder="Ask Pulse anything about your revenue, releases, or type an action...",
                                id="copilot-input",
                            )
                            yield Button("Send ↵", variant="success", id="btn-ask-copilot")

            with TabPane("[6] Settings", id="tab-settings"):
                with VerticalScroll(id="settings-scroll"):
                    yield Static(
                        "[bold #58a6ff]Engine Configuration & AI Bring-Your-Own-Key (BYOK)[/bold #58a6ff]\n"
                        "[dim]Manage live PayPal Sandbox / Production credentials, select AI Provider (Groq, OpenAI, Gemini, Claude, xAI, Ollama), and local ledger database.[/dim]",
                        classes="section-box",
                    )
                    with Vertical(classes="settings-card"):
                        yield Static("[bold #58a6ff]1. PayPal REST Gateway Credentials[/bold #58a6ff]")
                        yield Static("Environment Mode:", classes="settings-label")
                        yield Select(
                            [("Sandbox (Developer / Test)", "sandbox"), ("Live (Production)", "live")],
                            value="sandbox",
                            id="settings-paypal-mode",
                            allow_blank=False,
                        )
                        yield Static("PayPal Client ID:", classes="settings-label")
                        yield Input(placeholder="PayPal Client ID...", id="settings-paypal-id", classes="settings-input")
                        yield Static("PayPal Client Secret:", classes="settings-label")
                        yield Input(placeholder="PayPal Client Secret...", password=True, id="settings-paypal-secret", classes="settings-input")
                        with Horizontal(classes="settings-btn-row"):
                            yield Button("Test PayPal Connection", id="btn-test-paypal", classes="action-btn")
                        yield Static("", id="settings-paypal-status", classes="settings-status-box")

                    with Vertical(classes="settings-card"):
                        yield Static("[bold #58a6ff]2. Pulse AI Copilot Engine (BYOK)[/bold #58a6ff]")
                        yield Static("AI Provider:", classes="settings-label")
                        yield Select(
                            [
                                ("Groq", "groq"),
                                ("OpenAI", "openai"),
                                ("Google Gemini", "gemini"),
                                ("Anthropic Claude", "anthropic"),
                                ("xAI Grok", "xai"),
                                ("Ollama / Local (http://localhost:11434/v1)", "ollama"),
                                ("Custom OpenAI-Compatible", "custom"),
                            ],
                            value="groq",
                            id="settings-ai-provider",
                            allow_blank=False,
                        )
                        yield Static("Model ID (leave blank for recommended default):", classes="settings-label")
                        yield Input(placeholder="e.g. llama-3.3-70b-versatile, gpt-4o, claude-3-5-sonnet...", id="settings-ai-model", classes="settings-input")
                        yield Static("API Key:", classes="settings-label")
                        yield Input(placeholder="API Key for selected provider...", password=True, id="settings-ai-key", classes="settings-input")
                        yield Static("Base URL (optional, for Ollama or Custom Endpoint):", classes="settings-label")
                        yield Input(placeholder="e.g. http://localhost:11434/v1 or https://api.openai.com/v1", id="settings-ai-base-url", classes="settings-input")
                        with Horizontal(classes="settings-btn-row"):
                            yield Button("Test AI Connection", id="btn-test-ai", classes="action-btn")
                        yield Static("", id="settings-ai-status", classes="settings-status-box")

                    with Vertical(classes="settings-card"):
                        yield Static("[bold #58a6ff]3. Checkout Automation & Order Settlement[/bold #58a6ff]")
                        yield Static("Auto-Capture Approved Orders:", classes="settings-label")
                        yield Select(
                            [
                                ("Enabled (Standard / Immediate Settlement)", "true"),
                                ("Disabled (Manual Two-Phase Review)", "false"),
                            ],
                            value="true" if self.settings.auto_capture else "false",
                            id="settings-auto-capture",
                            allow_blank=False,
                        )
                        yield Static(
                            "[dim]When enabled, Pulse monitors buyer authorization in PayPal Sandbox and automatically captures funds into merchant balance without manual clicks.[/dim]",
                            classes="settings-hint",
                        )

                    with Vertical(classes="settings-card"):
                        yield Static("[bold #58a6ff]4. Local Transaction Ledger & History[/bold #58a6ff]")
                        yield Static("", id="settings-db-info")
                        with Horizontal(classes="settings-btn-row"):
                            yield Button("Reset Ledger", variant="error", id="btn-reset-ledger", classes="action-btn")
                            yield Button("Switch Demo / Live", id="btn-toggle-demo-mode", classes="action-btn")
                        yield Static("", id="settings-db-status", classes="settings-status-box")

                    with Horizontal(id="settings-actions-bar"):
                        yield Button("Save Changes", variant="primary", id="btn-save-settings")
                    yield Static("", id="settings-save-feedback", classes="settings-status-box")
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()
        self._populate_all()
        self._populate_settings(force=True)
        self._run_auth_check_worker(force_refresh=False)
        order_log = self.query_one("#order-log", RichLog)
        order_log.write(
            "[dim]Ready to mint live PayPal Sandbox Orders (`POST /v2/checkout/orders`). Click 'Checkout in PayPal ↗' to test live payment flow.[/dim]"
        )
        dispute_log = self.query_one("#dispute-log", RichLog)
        dispute_log.write(
            "[dim]Select any dispute above or enter a Dispute ID. Click 'Resolve with AI' to settle or 'View in PayPal ↗' to inspect Resolution Center.[/dim]"
        )
        # Check background updates (such as pp trigger) every 3 seconds
        self.set_interval(3.0, self._run_refresh_worker)

    def on_resize(self, event: events.Resize) -> None:
        """Protect layout and show minimum-dimension warning on viewports below 80x24."""
        is_too_small = event.size.width < 80 or event.size.height < 24
        try:
            overlay = self.query_one("#resize-warning-overlay", Static)
            overlay.display = is_too_small
            if is_too_small:
                overlay.update(
                    f"\n\n[bold red][ALERT] TERMINAL VIEWPORT TOO SMALL[/bold red]\n\n"
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
        term_width = self.size.width if self.size.width > 0 else 120
        is_wide = term_width >= 110

        env_label = "Production" if (auth and auth.mode == "live") else "Sandbox"
        if self.demo_mode:
            sb_badge = "[bold #d29922]●[/bold #d29922] [bold #e3b341]Demo Mode[/bold #e3b341]"
        elif auth is None:
            sb_badge = f"[bold #58a6ff]●[/bold #58a6ff] [bold #c9d1d9]{env_label}: Connecting...[/bold #c9d1d9]"
        elif auth.authenticated:
            if is_wide:
                lat_str = f"{auth.latency_ms / 1000:.1f}s" if auth.latency_ms >= 1000 else f"{auth.latency_ms:.0f}ms"
                sb_badge = f"[#3fb950]●[/#3fb950] [bold #c9d1d9]{env_label}: Live[/bold #c9d1d9] [dim]({lat_str})[/dim]"
            else:
                sb_badge = f"[#3fb950]●[/#3fb950] [bold #c9d1d9]{env_label}: Live[/bold #c9d1d9]"
        else:
            sb_badge = f"[#8b949e]●[/#8b949e] [bold #c9d1d9]{env_label}: Offline[/bold #c9d1d9]"

        if open_disputes_count is None:
            open_disputes_count = len(self.store.get_disputes(status_filter="OPEN"))

        lat_val = auth.latency_ms if auth and auth.authenticated else 180.0
        conn_val = auth.authenticated if auth else False
        summary_peek = self.store.get_dashboard_summary(api_latency_ms=lat_val, api_connected=conn_val)
        score_val = summary_peek["health_score"]
        rating_str = summary_peek.get("health_rating", "HEALTHY")
        rating_color = (
            "#3fb950" if score_val >= 90
            else ("#58a6ff" if score_val >= 75
                  else ("#d29922" if score_val >= 60 else "#f85149"))
        )

        disp_status = f"[bold #f85149]{open_disputes_count} Open[/bold #f85149]" if open_disputes_count > 0 else "[#3fb950]0 Open[/#3fb950]"

        ledger_state = self.store.detect_state()
        ledger_label = {
            "fresh": "Empty",
            "sample": "Sample",
            "connected": "30d Synced",
        }.get(ledger_state, "30d Synced")

        left_content = (
            f" {sb_badge}  [dim]│[/dim]  "
            f"[dim]Ledger:[/dim] [bold #c9d1d9]{ledger_label}[/bold #c9d1d9]"
        )

        rels = [] if ledger_state == "fresh" else self.store.get_releases(include_git_tags=True)
        if len(rels) >= 2:
            rel_range_str = f"{rels[0]['version']}..{rels[-1]['version']}"
        elif len(rels) == 1:
            rel_range_str = str(rels[0]["version"])
        else:
            rel_range_str = "none"

        if is_wide:
            right_content = (
                f"[dim]Health:[/dim] [bold {rating_color}]{score_val}/100[/bold {rating_color}] [dim]({rating_str})[/dim]  [dim]│[/dim]  "
                f"[dim]Releases:[/dim] [bold #c9d1d9]{rel_range_str}[/bold #c9d1d9]  [dim]│[/dim]  "
                f"[dim]Disputes:[/dim] {disp_status} "
            )
        else:
            right_content = (
                f"[dim]Health:[/dim] [bold {rating_color}]{score_val}/100[/bold {rating_color}]  [dim]│[/dim]  "
                f"[dim]Disputes:[/dim] {disp_status} "
            )

        grid = Table.grid(expand=True)
        grid.add_column(justify="left", no_wrap=True)
        grid.add_column(justify="right", no_wrap=True)
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
        if auth_res.authenticated and not self.demo_mode:
            try:
                live_res = self.pp_client.get_disputes()
                if live_res.get("ok") and live_res.get("items"):
                    self.store.sync_external_disputes(live_res["items"])
            except Exception:
                pass
        self.call_from_thread(self._update_auth_banner, auth_res)
        self.call_from_thread(self._populate_all)

    def _populate_all(self) -> None:
        if not self.is_running or not self.query("#dash-dispute-alert"):
            return
        lat_val = self._auth_result.latency_ms if self._auth_result and self._auth_result.authenticated else 180.0
        conn_val = self._auth_result.authenticated if self._auth_result else False
        summary = self.store.get_dashboard_summary(api_latency_ms=lat_val, api_connected=conn_val)

        # Non-blocking auth check: use cached memory state or disk token cache; otherwise authenticate on background thread
        if self._auth_result is None:
            if not self.settings.has_paypal_credentials or self.pp_client._load_cached_token() is not None:
                self._auth_result = self.pp_client.authenticate(force_refresh=False)
            else:
                self._run_auth_check_worker(force_refresh=False)

        if summary.get("state") == "fresh":
            self.query_one("#dash-dispute-alert", Static).display = False
            self.query_one("#kpi-net", Static).update(
                "[bold #8b949e]TRUE NET TAKE-HOME[/bold #8b949e]\n"
                "[bold #f0f6fc]No ledger data yet[/bold #f0f6fc]\n"
                "[dim]Record an order to start the dashboard.[/dim]"
            )
            self.query_one("#kpi-watch", Static).update(
                "[bold #8b949e]CUSTOMERS & SALES[/bold #8b949e]\n"
                "[dim]Nothing recorded yet.[/dim]"
            )
            self.query_one("#kpi-health", Static).update(
                "[bold #8b949e]MERCHANT HEALTH[/bold #8b949e]\n"
                "[dim]Awaiting first order.[/dim]"
            )
            self.query_one("#dash-sparkline", Static).update(
                "[bold #8b949e]14-Day True Net Velocity:[/bold #8b949e] [dim]No data[/dim]"
            )
            for table_id in ("#currency-table", "#timeline-table", "#disputes-table"):
                try:
                    self.query_one(table_id, DataTable).clear()
                except Exception:
                    pass
            self._update_auth_banner(self._auth_result, open_disputes_count=0)
            return

        open_cnt = summary["open_disputes_count"]
        self._update_auth_banner(self._auth_result, open_disputes_count=open_cnt)

        if open_cnt > 0:
            open_disputes = self.store.get_disputes(status_filter="OPEN")
            first_disp = open_disputes[0] if open_disputes else {}
            disp_id = first_disp.get("dispute_id", "PP-D-90411-DE")
            disp_rel = first_disp.get("release_version", "v1.2.1")
            disp_country = first_disp.get("buyer_country", "DE")
            self.query_one("#dash-dispute-alert", Static).display = True
            self.query_one("#dash-dispute-alert", Static).update(
                f"[bold #f85149][ALERT] ACTION REQUIRED:[/bold #f85149] {open_cnt} open buyer dispute ({disp_id}) on release [bold #f0f6fc]{disp_rel} ({disp_country})[/bold #f0f6fc] "
                f"[bold #e5a824]•[/bold #e5a824] [dim]Press 4 to inspect and auto-draft evidence with Pulse AI[/dim]"
            )
        else:
            self.query_one("#dash-dispute-alert", Static).display = False

        self.query_one("#kpi-net", Static).update(
            f"[bold #8b949e]TRUE NET TAKE-HOME[/bold #8b949e]\n"
            f"[bold #3fb950]${summary['total_net_usd']:,.2f}[/bold #3fb950] [dim]({summary['net_margin_pct']}% margin)[/dim]\n"
            f"[dim]Gross:[/dim] [bold #f0f6fc]${summary['total_gross_usd']:,.0f}[/bold #f0f6fc]  [dim]Fees:[/dim] [bold #8b949e]-${summary['total_fees_usd']:,.0f} ({summary['fee_drag_pct']}%)[/bold #8b949e]"
        )

        cust_count = summary.get("total_customers", summary.get("total_installs", 0))
        active_cust = summary.get("active_customers", summary.get("active_users", 0))
        paid_orders = summary.get("total_orders", summary.get("total_paid", 0))
        mrr_val = summary.get("mrr_usd", 0.0)
        conv_pct = summary.get("overall_conversion_pct", 0.0)
        mrr_fmt = f"${mrr_val:,.0f}/mo" if mrr_val < 100_000 else f"${mrr_val/1000:,.1f}k/mo"

        self.query_one("#kpi-watch", Static).update(
            f"[bold #8b949e]CUSTOMERS & SALES[/bold #8b949e]\n"
            f"[bold #f0f6fc]{cust_count:,}[/bold #f0f6fc] [dim]Users ({active_cust:,} active)[/dim]\n"
            f"[dim]Orders:[/dim] [bold #f0f6fc]{paid_orders:,}[/bold #f0f6fc] [dim]({conv_pct:.1f}%)[/dim] [dim]MRR:[/dim] [bold #58a6ff]{mrr_fmt}[/bold #58a6ff]"
        )

        telemetry = summary.get("health_telemetry", {})
        score_val = summary["health_score"]
        rating_str = summary.get("health_rating", "HEALTHY")
        health_color = (
            "#3fb950" if score_val >= 90
            else ("#58a6ff" if score_val >= 75
                  else ("#d29922" if score_val >= 60 else "#f85149"))
        )
        self.query_one("#kpi-health", Static).update(
            f"[bold #8b949e]MERCHANT HEALTH[/bold #8b949e]\n"
            f"[bold {health_color}]{score_val} / 100[/bold {health_color}] [dim][{rating_str}][/dim]\n"
            f"[dim]Disputes:[/dim] [bold #f0f6fc]{telemetry.get('dispute_ratio_pct', 0.26)}%[/bold #f0f6fc]  [dim]Latency:[/dim] [bold #f0f6fc]{lat_val:.0f}ms[/bold #f0f6fc]"
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
        cur_tuples = []
        for c in summary["currencies"]:
            c_code = c["currency"]
            is_zero_dec = c_code in ZERO_DECIMAL_CURRENCIES
            gross_str = f"{int(round(c['gross'])):,}" if is_zero_dec else f"{c['gross']:,.2f}"
            fees_str = f"-{int(round(c['fees'])):,}" if is_zero_dec else f"-{c['fees']:,.2f}"
            net_str = f"{int(round(c['net'])):,}" if is_zero_dec else f"{c['net']:,.2f}"
            cur_tuples.append(
                (
                    c_code,
                    str(c["tx_count"]),
                    gross_str,
                    fees_str,
                    f"{c['fee_pct']}%",
                    net_str,
                    f"{c['net_margin_pct']}%",
                    f"${c['usd_net']:,.2f}",
                )
            )
        if cur_tuples != getattr(self, "_last_cur_data", None):
            self._last_cur_data = cur_tuples
            cur_table.clear()
            for r in cur_tuples:
                cur_table.add_row(*r)

        tl_table = self.query_one("#timeline-table", DataTable)
        diag_lines = []
        releases = self.store.get_releases(include_git_tags=not self._git_tags_loaded)
        self._git_tags_loaded = True
        tl_tuples = []
        for r in releases:
            cust_val = r.get("customers", r.get("installs", 0))
            order_val = r.get("paid_orders", r.get("paid_conversions", 0))
            tl_tuples.append(
                (
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
            )
            diag_lines.append(f"• [bold]{r['version']}[/bold] ({r['title']}): {r['ai_diagnostic']}")
        self.query_one("#timeline-diagnostics", Static).update("\n".join(diag_lines))

        if tl_tuples != getattr(self, "_last_tl_data", None):
            self._last_tl_data = tl_tuples
            tl_table.clear()
            for r in tl_tuples:
                tl_table.add_row(*r)

        disp_table = self.query_one("#disputes-table", DataTable)
        disp_tuples = [
            (
                d["dispute_id"],
                d["status"],
                d["buyer_country"],
                d.get("platform_sku", d.get("device_model", "")),
                d["release_version"],
                f"{d['amount']:.2f} {d['currency']}",
                d["due_date"],
                d["reason"],
            )
            for d in summary["disputes"]
        ]
        if disp_tuples != getattr(self, "_last_disputes_data", None):
            self._last_disputes_data = disp_tuples
            self._populating_disputes = True
            try:
                # Capture current selection so focus and selection are preserved
                current_id = None
                inps = self.query("#dispute-id-input")
                if inps:
                    current_id = inps.first().value.strip()

                disp_table.clear()
                target_row = 0
                for idx, r in enumerate(disp_tuples):
                    disp_table.add_row(*r)
                    if current_id and r[0] == current_id:
                        target_row = idx

                if disp_table.row_count > 0:
                    disp_table.move_cursor(row=target_row)
                    if inps and (not current_id or not any(r[0] == current_id for r in disp_tuples)):
                        inps.first().value = str(disp_tuples[target_row][0])
            finally:
                self._populating_disputes = False
        self._update_settings_db_telemetry()

    def action_refresh_data(self) -> Worker[None]:
        self.notify("Refreshing telemetry from PayPal...", title="Pulse Sync", timeout=2.0)
        return self._run_refresh_worker()

    @work(thread=True, exclusive=True, group="refresh")
    def _run_refresh_worker(self) -> None:
        self._auth_result = self.pp_client.authenticate(force_refresh=False)
        if self._auth_result.authenticated and not self.demo_mode:
            try:
                live_res = self.pp_client.get_disputes()
                if live_res.get("ok") and live_res.get("items"):
                    self.store.sync_external_disputes(live_res["items"])
            except Exception:
                pass
            try:
                tx_res = self.pp_client.get_transactions()
                if tx_res.get("ok") and tx_res.get("transaction_details"):
                    self.store.sync_external_transactions(tx_res["transaction_details"])
            except Exception:
                pass
        self.call_from_thread(self._populate_all)

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one("#main-tabs", TabbedContent).active = tab_id

    def action_show_help(self) -> None:
        """Display the interactive keyboard shortcuts modal dialog."""
        self.push_screen(HelpModal())

    @on(Button.Pressed, "#btn-create-order")
    @on(Input.Submitted, "#order-amount")
    @on(Input.Submitted, "#order-currency")
    @on(Input.Submitted, "#order-release")
    @on(Input.Submitted, "#order-desc")
    def handle_create_order(self) -> Worker[None] | None:
        proj_ctx = detect_project_context()
        default_sku = str(proj_ctx.get("default_sku") or "PulseCore Pro")
        default_desc = f"{default_sku} License" if proj_ctx.get("config_file") else "PulseCore Pro Developer License"
        amt_str = self.query_one("#order-amount", Input).value.strip()
        cur_str = self.query_one("#order-currency", Input).value.strip().upper() or str(proj_ctx.get("default_currency") or "USD").upper()
        rel_str = self.query_one("#order-release", Input).value.strip()
        desc_str = self.query_one("#order-desc", Input).value.strip() or default_desc
        log = self.query_one("#order-log", RichLog)
        btn = self.query_one("#btn-create-order", Button)

        try:
            amt = float(amt_str)
        except ValueError:
            log.write("[bold red]Invalid amount. Please enter a numeric value (e.g. 9.99).[/bold red]")
            return None

        effective_release = rel_str or proj_ctx.get("version") or parse_release_version(desc_str, default="v1.2.1")
        btn.disabled = True
        log.write(f"[cyan]● Minting PayPal Sandbox Order for {amt:.2f} {cur_str} ({effective_release})...[/cyan]")
        return self._run_create_order_worker(amt, cur_str, desc_str, effective_release, default_sku)

    @work(thread=True, exclusive=True, group="create-order")
    def _run_create_order_worker(self, amt: float, cur_str: str, desc_str: str, rel_ver: str, sku_str: str = "PulseCore Pro") -> None:
        try:
            res = self.pp_client.create_order(
                amount=amt,
                currency=cur_str,
                description=desc_str,
                custom_id=f"release:{rel_ver}|US|{sku_str}",
            )
            if res.success:
                self.store.record_live_order(res, rel_ver, "US", platform_sku=sku_str)
            self.call_from_thread(self._finish_create_order_ui, res)
        except Exception as exc:
            self.call_from_thread(self._finish_create_order_error, str(exc))
            self.call_from_thread(self._finish_create_order_error, str(exc))

    def _finish_create_order_ui(self, res: Any) -> None:
        log = self.query_one("#order-log", RichLog)
        btn = self.query_one("#btn-create-order", Button)
        btn.disabled = False
        if res.success:
            self._last_order_id = res.order_id
            self._last_order_approve_url = res.approve_url
            browser_btn = self.query_one("#btn-open-order-browser", Button)
            browser_btn.disabled = False
            capture_btn = self.query_one("#btn-capture-order", Button)
            capture_btn.disabled = False
            if self.settings.auto_capture and not self.settings.demo_mode:
                log.write(
                    f"[bold green][OK] Live PayPal Sandbox Order Created![/bold green]\n"
                    f"  • Order ID: [bold]{res.order_id}[/bold]\n"
                    f"  • Status: {res.status}\n"
                    f"  • Approval URL: [bold underline cyan]{res.approve_url}[/bold underline cyan]\n"
                    f"  [cyan]● Auto-Capture is ENABLED (Standard): Click 'Checkout in PayPal ↗' to pay in browser; Pulse will capture funds automatically upon approval.[/cyan]"
                )
            else:
                log.write(
                    f"[bold green][OK] Live PayPal Sandbox Order Created![/bold green]\n"
                    f"  • Order ID: [bold]{res.order_id}[/bold]\n"
                    f"  • Status: {res.status}\n"
                    f"  • Approval URL: [bold underline cyan]{res.approve_url}[/bold underline cyan]\n"
                    f"  [dim]1. Click 'Checkout in PayPal ↗' to approve payment in browser.[/dim]\n"
                    f"  [dim]2. After approving, click 'Capture Order ✓' to settle funds into recent activity & ledger.[/dim]"
                )
            self._populate_all()
        else:
            log.write(f"[bold red][FAIL] Order creation failed:[/bold red] {res.error}")

    @on(Button.Pressed, "#btn-open-order-browser")
    def handle_open_order_browser(self) -> None:
        log = self.query_one("#order-log", RichLog)
        if self._last_order_approve_url:
            log.write(f"[green]Opening PayPal checkout in browser:[/green] {self._last_order_approve_url}")
            webbrowser.open(self._last_order_approve_url)
            if getattr(self, "_last_order_id", None) and self.settings.auto_capture and not self.settings.demo_mode:
                log.write(
                    f"[dim cyan]● Auto-Capture active: Monitoring Order {self._last_order_id} for buyer approval...[/dim cyan]"
                )
                self._run_auto_capture_watcher(self._last_order_id)
        else:
            log.write("[yellow]No active order approval URL to open yet.[/yellow]")

    @work(thread=True, exclusive=False, group="auto-capture-watcher")
    def _run_auto_capture_watcher(self, order_id: str) -> None:
        """Rate-limit safe background watcher (polls single active order at 4s intervals, max 20 attempts)."""
        import time
        if not self.settings.auto_capture or self.settings.demo_mode:
            return
        for _ in range(20):
            time.sleep(4.0)
            try:
                chk = self.pp_client.get_order(order_id)
                if not chk.success:
                    continue
                if chk.status == "COMPLETED":
                    self.store.update_order_status(paypal_id=order_id, status="COMPLETED")
                    self.call_from_thread(self._finish_auto_capture_ui, chk)
                    break
                if chk.status == "APPROVED":
                    cap_res = self.pp_client.capture_order(order_id)
                    if cap_res.success:
                        self.store.update_order_status(
                            paypal_id=cap_res.order_id,
                            status=cap_res.status,
                            amount=cap_res.amount,
                            currency=cap_res.currency,
                        )
                        self.call_from_thread(self._finish_auto_capture_ui, cap_res)
                        break
            except Exception:
                pass

    def _finish_auto_capture_ui(self, res: Any) -> None:
        log = self.query_one("#order-log", RichLog)
        try:
            capture_btn = self.query_one("#btn-capture-order", Button)
            capture_btn.disabled = True
        except Exception:
            pass
        log.write(
            f"[bold green][AUTO-CAPTURE ✓] Order {res.order_id} approved by customer![/bold green]\n"
            f"  • Status: [bold green]COMPLETED[/bold green] (Settled automatically via Auto-Capture engine)\n"
            f"  • Captured: [cyan]{res.amount:.2f} {res.currency}[/cyan]\n"
            f"  [green]Funds settled into merchant balance and recorded in ledger without manual button click.[/green]"
        )
        self._populate_all()

    @on(Button.Pressed, "#btn-capture-order")
    def handle_capture_order(self) -> Worker[None] | None:
        log = self.query_one("#order-log", RichLog)
        btn = self.query_one("#btn-capture-order", Button)
        oid = getattr(self, "_last_order_id", None)
        if not oid:
            log.write("[yellow]No active order to capture. Mint an order first.[/yellow]")
            return None
        btn.disabled = True
        log.write(f"[cyan]● Executing PayPal Orders v2 Capture for Order {oid}...[/cyan]")
        return self._run_capture_order_worker(oid)

    @work(thread=True, exclusive=True, group="capture-order")
    def _run_capture_order_worker(self, order_id: str) -> None:
        try:
            res = self.pp_client.capture_order(order_id)
            if res.success:
                self.store.update_order_status(
                    paypal_id=res.order_id,
                    status=res.status,
                    amount=res.amount,
                    currency=res.currency,
                )
            self.call_from_thread(self._finish_capture_order_ui, res)
        except Exception as exc:
            self.call_from_thread(self._finish_capture_order_error, str(exc))

    def _finish_capture_order_ui(self, res: Any) -> None:
        log = self.query_one("#order-log", RichLog)
        btn = self.query_one("#btn-capture-order", Button)
        btn.disabled = False
        if res.success:
            log.write(
                f"[bold green][OK] PayPal Order Captured & Settled Successfully![/bold green]\n"
                f"  • Order ID: [bold]{res.order_id}[/bold]\n"
                f"  • Status: [bold green]{res.status}[/bold green] (COMPLETED)\n"
                f"  • Captured Amount: [cyan]{res.amount:.2f} {res.currency}[/cyan]\n"
                f"  [green]Funds deducted from buyer and settled to merchant! Visible in PayPal Recent Activity.[/green]"
            )
            self._populate_all()
        else:
            log.write(f"[bold red][FAIL] Order capture failed:[/bold red] {res.error}\n[dim]Ensure the buyer has completed authorization in the browser first.[/dim]")

    def _finish_capture_order_error(self, err: str) -> None:
        log = self.query_one("#order-log", RichLog)
        btn = self.query_one("#btn-capture-order", Button)
        btn.disabled = False
        log.write(f"[bold red][FAIL] Order capture error:[/bold red] {err}")

    def _finish_create_order_error(self, err: str) -> None:
        log = self.query_one("#order-log", RichLog)
        btn = self.query_one("#btn-create-order", Button)
        btn.disabled = False
        log.write(f"[bold red][FAIL] Order creation failed:[/bold red] {err}")

    @on(Button.Pressed, "#btn-open-dispute-browser")
    def handle_open_dispute_browser(self) -> None:
        log = self.query_one("#dispute-log", RichLog)
        dispute_id = self.query_one("#dispute-id-input", Input).value.strip()
        base_url = (
            "https://www.sandbox.paypal.com/disputes"
            if self.settings.paypal_mode == "sandbox"
            else "https://www.paypal.com/disputes"
        )
        url = f"{base_url}/{dispute_id}" if dispute_id else base_url
        log.write(f"[green]Opening PayPal Resolution Center in browser:[/green] {url}")
        webbrowser.open(url)

    @on(DataTable.RowSelected, "#disputes-table")
    @on(DataTable.RowHighlighted, "#disputes-table")
    @on(DataTable.CellSelected, "#disputes-table")
    @on(DataTable.CellHighlighted, "#disputes-table")
    def handle_dispute_row_selected(
        self,
        event: DataTable.RowSelected | DataTable.RowHighlighted | DataTable.CellSelected | DataTable.CellHighlighted,
    ) -> None:
        if getattr(self, "_populating_disputes", False):
            return
        table = getattr(event, "control", None)
        if not table or getattr(table, "id", None) != "disputes-table":
            return
        try:
            row = None
            if hasattr(event, "row_key") and event.row_key is not None:
                row = table.get_row(event.row_key)
            elif hasattr(event, "coordinate") and event.coordinate is not None:
                row = table.get_row_at(event.coordinate.row)
            elif hasattr(event, "cursor_row") and event.cursor_row >= 0:
                row = table.get_row_at(event.cursor_row)
            if row and len(row) > 0:
                dispute_id = str(row[0])
                inps = self.query("#dispute-id-input")
                if inps:
                    inps.first().value = dispute_id
                self._update_dispute_action_ui(dispute_id)
        except Exception:
            pass

    @on(Input.Changed, "#dispute-id-input")
    def handle_dispute_input_changed(self, event: Input.Changed) -> None:
        self._update_dispute_action_ui(event.value.strip())

    def _update_dispute_action_ui(self, dispute_id: str) -> None:
        try:
            btn = self.query_one("#btn-resolve-dispute", Button)
            dispute = self.store.get_dispute_by_id(dispute_id) if dispute_id else None
            if not dispute:
                btn.label = "Resolve with AI"
                btn.variant = "warning"
                btn.refresh(layout=True)
                return

            status = str(dispute.get("status", "OPEN")).upper()
            is_officially_resolved = status in ("RESOLVED", "CLOSED", "CANCELLED")
            has_evidence = bool(dispute.get("ai_evidence_draft")) or bool(dispute.get("resolved_at")) or bool(dispute.get("resolution_order_id"))

            if is_officially_resolved:
                btn.label = "View Evidence"
                btn.variant = "success"
                btn.refresh(layout=True)
                log = self.query_one("#dispute-log", RichLog)
                log.clear()
                rep_order = dispute.get("resolution_order_id") or dispute.get("replacement_order_id", "N/A")
                log.write(
                    f"[bold green][OK] Dispute {dispute_id} is RESOLVED[/bold green]\n"
                    f"  • Status: Closed & Settled in PayPal Resolution Center\n"
                    f"  • Replacement Order ID: [bold]{rep_order}[/bold]\n"
                    f"  • Formal Evidence Package on File:"
                )
                evidence = dispute.get("ai_evidence_draft") or dispute.get("evidence_text") or "No evidence statement recorded."
                log.write(Markdown(evidence))
            elif has_evidence:
                btn.label = "View Evidence"
                btn.variant = "success"
                btn.refresh(layout=True)
                log = self.query_one("#dispute-log", RichLog)
                log.clear()
                rep_order = dispute.get("resolution_order_id") or dispute.get("replacement_order_id", "N/A")
                log.write(
                    f"[bold yellow][WAITING] Dispute {dispute_id} Response on File[/bold yellow]\n"
                    f"  • Gateway Status: [bold cyan]{status}[/bold cyan] (Evidence Transmitted — Pending Review)\n"
                    f"  • Temporary Hold: Active until PayPal adjudication or buyer confirms closure\n"
                    f"  • Replacement Order ID: [bold]{rep_order}[/bold]\n"
                    f"  • Formal Evidence Package on File:"
                )
                evidence = dispute.get("ai_evidence_draft") or dispute.get("evidence_text") or "No evidence statement recorded."
                log.write(Markdown(evidence))
            else:
                btn.label = "Resolve with AI"
                btn.variant = "warning"
                btn.refresh(layout=True)
        except Exception:
            pass

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

        status = str(dispute.get("status", "OPEN")).upper()
        if status in ("RESOLVED", "CLOSED", "CANCELLED"):
            self._update_dispute_action_ui(dispute_id)
            log.write(f"[dim]Dispute {dispute_id} is already officially RESOLVED by PayPal.[/dim]")
            return None

        has_evidence = bool(dispute.get("ai_evidence_draft")) or bool(dispute.get("resolved_at")) or bool(dispute.get("resolution_order_id"))
        if has_evidence:
            self._update_dispute_action_ui(dispute_id)
            log.write(f"[dim]Evidence package already submitted to PayPal for {dispute_id} (Status: {dispute.get('status')}).[/dim]")
            return None

        btn.disabled = True
        log.write(f"[cyan]● Resolving {dispute_id} via PayPal Sandbox & Pulse AI...[/cyan]")
        return self._run_resolve_dispute_worker(dispute_id, dispute)

    @work(thread=True, exclusive=True, group="resolve-dispute")
    def _run_resolve_dispute_worker(self, dispute_id: str, dispute: dict[str, Any]) -> None:
        try:
            # Refresh full dispute conversation & metadata from PayPal Sandbox if online
            if not self.settings.demo_mode and self.settings.has_paypal_credentials:
                det_res = self.pp_client.get_dispute_details(dispute_id)
                if det_res.get("ok") and det_res.get("dispute"):
                    self.store.sync_external_disputes([det_res["dispute"]])
                    refreshed = self.store.get_dispute_by_id(dispute_id)
                    if refreshed:
                        dispute = refreshed

            sku_val = dispute.get("platform_sku", dispute.get("device_model", "Ubuntu Linux 24.04 (CLI)"))
            # 1. Mint live PayPal Sandbox remediation entitlement reference
            rep_order = self.pp_client.create_order(
                amount=float(dispute.get("amount", 9.99)),
                currency=dispute.get("currency", "USD"),
                description=f"Dispute {dispute_id} Remediation Entitlement ({sku_val})",
                custom_id=f"RESOLVE|{dispute_id}",
            )
            # 2. Triage buyer problem and draft tailored resolution + evidence package
            plan = diagnose_dispute_and_remediate(dispute, rep_order)
            evidence_text, model_used = draft_dispute_evidence(dispute, rep_order, self.settings)

            # 3. Dispatch multi-channel resolution (evidence + direct buyer message + claim/offer)
            sub_res = self.pp_client.submit_dispute_resolution(
                dispute_id=dispute_id,
                evidence_text=evidence_text,
                amount=float(dispute["amount"]),
                currency=dispute["currency"],
                reason=str(dispute.get("reason", "MERCHANDISE_OR_SERVICE_NOT_RECEIVED")),
                buyer_message=plan["buyer_message"],
                resolution_strategy=plan["resolution_strategy"],
                replacement_order=rep_order,
            )
            order_id = rep_order.order_id if rep_order.success else plan["remediation_id"]
            if rep_order.success:
                self.store.record_live_order(
                    rep_order,
                    release_version=dispute.get("release_version") or "v1.2.1",
                    country_code=dispute["buyer_country"],
                    platform_sku=sku_val,
                )
            updated_status = sub_res.get("updated_status") or "UNDER_REVIEW"
            self.store.mark_dispute_resolved(
                dispute_id,
                evidence_text,
                order_id,
                new_status=updated_status,
            )
            self.call_from_thread(
                self._finish_resolve_dispute_ui,
                dispute_id,
                order_id,
                model_used,
                evidence_text,
                updated_status,
            )
        except Exception as exc:
            self.call_from_thread(self._finish_resolve_dispute_error, str(exc))

    def _finish_resolve_dispute_ui(
        self,
        dispute_id: str,
        order_id: str,
        model_used: str,
        evidence_text: str,
        updated_status: str = "UNDER_REVIEW",
    ) -> None:
        log = self.query_one("#dispute-log", RichLog)
        btn = self.query_one("#btn-resolve-dispute", Button)
        btn.disabled = False

        is_officially_closed = updated_status.upper() in ("RESOLVED", "CLOSED", "CANCELLED")
        if is_officially_closed:
            btn.label = "View Settlement"
            btn.variant = "success"
            btn.refresh(layout=True)
            log.write(
                f"[bold green][OK] Dispute {dispute_id} is Officially RESOLVED![/bold green]\n"
                f"  • Gateway Status: Closed by PayPal Resolution Center\n"
                f"  • Replacement Sandbox Order ID: [bold]{order_id}[/bold]\n"
                f"  • AI Engine: {model_used}\n"
                f"  • Formal Evidence Package on File:"
            )
        else:
            btn.label = "View Evidence"
            btn.variant = "default"
            btn.refresh(layout=True)
            log.write(
                f"[bold yellow][SUBMITTED] Evidence Package Transmitted for {dispute_id}[/bold yellow]\n"
                f"  • Gateway Status: [bold cyan]{updated_status}[/bold cyan] (Pending Buyer / PayPal Adjudication)\n"
                f"  • Settlement Hold: Funds remain held until PayPal or buyer confirms resolution.\n"
                f"  • Replacement Sandbox Order ID: [bold]{order_id}[/bold]\n"
                f"  • AI Engine: {model_used}\n"
                f"  • Formal Evidence Package on File:"
            )
        log.write(Markdown(evidence_text))
        self._populate_all()

    def _finish_resolve_dispute_error(self, err: str) -> None:
        log = self.query_one("#dispute-log", RichLog)
        btn = self.query_one("#btn-resolve-dispute", Button)
        btn.disabled = False
        btn.label = "Resolve with AI"
        btn.variant = "warning"
        btn.refresh(layout=True)
        log.write(f"[bold red][FAIL] Dispute resolution failed:[/bold red] {err}")

    @on(Button.Pressed, "#btn-simulate-dispute")
    def handle_simulate_dispute(self) -> None:
        sim_disp = self.store.simulate_incoming_dispute()
        log = self.query_one("#dispute-log", RichLog)
        disp_id = sim_disp.get("dispute_id", "PP-D-SIM")
        log.write(
            f"[bold #f85149][ALERT] Simulated Real-World Chargeback Event:[/bold #f85149] [bold yellow]{disp_id}[/bold yellow]\n"
            f"  • Claim: {sim_disp.get('description')}\n"
            f"  • Amount: {sim_disp.get('amount', 5.99):.2f} {sim_disp.get('currency', 'EUR')} ({sim_disp.get('platform_sku')})\n"
            f"  • Impact: Merchant Health Index dynamically penalized; active dispute surge triggered!\n"
            f"  [dim]Input pre-filled with '{disp_id}'. Press 'Resolve with AI' to remediate.[/dim]"
        )
        self.query_one("#dispute-id-input", Input).value = disp_id
        self._update_dispute_action_ui(disp_id)
        self._populate_all()

    @on(Button.Pressed, "#btn-sync-disputes")
    def handle_sync_disputes(self) -> Worker[None]:
        log = self.query_one("#dispute-log", RichLog)
        log.write("[cyan]● Polling PayPal Disputes API (GET /v1/customer/disputes)...[/cyan]")
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
                f"[bold green][OK] PayPal Disputes API Synced (HTTP 200 OK)[/bold green]\n"
                f"  • Live Disputes from PayPal: {count} ({added} new ingested into SQLite ledger)\n"
                f"  • Real-Time Gateway Status: Connected"
            )
        else:
            log.write(f"[yellow][WARN] Disputes API returned:[/yellow] {live_res.get('error', 'No response')}")
        self._populate_all()

    def _finish_sync_disputes_error(self, err: str) -> None:
        log = self.query_one("#dispute-log", RichLog)
        btn = self.query_one("#btn-sync-disputes", Button)
        btn.disabled = False
        log.write(f"[bold red][FAIL] Dispute API sync failed:[/bold red] {err}")

    @on(Button.Pressed, ".prompt-chip")
    def handle_prompt_chip(self, event: Button.Pressed) -> Worker[None] | None:
        chip_id = event.button.id or ""
        prompt_text = getattr(self, "quick_prompt_map", {}).get(chip_id) or QUICK_PROMPT_MAP.get(chip_id)
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
                f"[bold #7ee787][TOOL] {tool_name}[/bold #7ee787] -> {res.executed_action}",
                classes="claude-tool-pill",
            )
            chat_scroll.mount(tool_widget)
            self._populate_all()

        sec = res.latency_ms / 1000.0
        header = Text.from_markup("[bold #00a8ea]Pulse Copilot[/bold #00a8ea] [bold #e5a824]•[/bold #e5a824]")
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

    def _update_settings_db_telemetry(self) -> None:
        """Update active DB telemetry summary without touching input or select widgets."""
        try:
            db_info = self.query_one("#settings-db-info", Static)
            summary = self.store.get_dashboard_summary()
            tx_count = sum(c.get("tx_count", 0) for c in summary.get("currencies", []))
            gross_usd = summary.get("total_gross_usd", 0.0)
            open_disp = summary.get("open_disputes_count", 0)
            db_label = "Demo Mode (Sample Data)" if self.demo_mode else "Live Mode (Real Account)"
            db_info.update(
                f"[bold]Local Database:[/bold] `{self.settings.db_path}`\n"
                f"[bold]Account Status:[/bold] {db_label} | [bold]30-Day Activity:[/bold] {tx_count} transactions\n"
                f"[bold]Gross Revenue:[/bold] ${gross_usd:,.2f} USD | [bold]Open Disputes:[/bold] {open_disp}"
            )
        except Exception:
            pass

    def _populate_settings(self, force: bool = False) -> None:
        """Populate settings form controls with active configuration."""
        try:
            mode_select = self.query_one("#settings-paypal-mode", Select)
            active_mode = self.settings.paypal_mode.lower()
            if active_mode in ("sandbox", "live") and (force or not mode_select.has_focus):
                mode_select.value = active_mode

            pp_id_input = self.query_one("#settings-paypal-id", Input)
            if force or not pp_id_input.has_focus:
                pp_id_input.value = self.settings.paypal_client_id

            pp_sec_input = self.query_one("#settings-paypal-secret", Input)
            if force or not pp_sec_input.has_focus:
                pp_sec_input.value = self.settings.paypal_client_secret

            ai_prov_select = self.query_one("#settings-ai-provider", Select)
            active_prov = self.settings.active_ai_provider.lower()
            if active_prov in ("groq", "openai", "gemini", "anthropic", "xai", "ollama", "custom") and (force or not ai_prov_select.has_focus):
                ai_prov_select.value = active_prov

            ai_model_input = self.query_one("#settings-ai-model", Input)
            if force or not ai_model_input.has_focus:
                ai_model_input.value = self.settings.active_ai_model

            ai_key_input = self.query_one("#settings-ai-key", Input)
            if force or not ai_key_input.has_focus:
                ai_key_input.value = self.settings.active_ai_api_key

            ai_url_input = self.query_one("#settings-ai-base-url", Input)
            if force or not ai_url_input.has_focus:
                ai_url_input.value = self.settings.active_ai_base_url

            auto_cap_select = self.query_one("#settings-auto-capture", Select)
            if force or not auto_cap_select.has_focus:
                auto_cap_select.value = "true" if self.settings.auto_capture else "false"

            self._update_settings_db_telemetry()
        except Exception:
            pass

    @on(Button.Pressed, "#btn-test-paypal")
    def on_btn_test_paypal(self) -> None:
        status_box = self.query_one("#settings-paypal-status", Static)
        status_box.update("[dim yellow]Pinging PayPal OAuth 2.0 endpoint...[/dim yellow]")
        pp_id = self.query_one("#settings-paypal-id", Input).value.strip()
        pp_sec = self.query_one("#settings-paypal-secret", Input).value.strip()
        mode_val = str(self.query_one("#settings-paypal-mode", Select).value)
        self._run_paypal_ping(pp_id, pp_sec, mode_val)

    @work(thread=True, exclusive=True, group="ping-paypal")
    def _run_paypal_ping(self, client_id: str, client_secret: str, mode: str) -> None:
        temp_settings = PulseSettings(
            paypal_client_id=client_id,
            paypal_client_secret=client_secret,
            paypal_mode=mode,
        )
        test_client = PayPalClient(temp_settings)
        auth_res = test_client.authenticate(force_refresh=True)
        self.call_from_thread(self._finish_paypal_ping, auth_res)

    def _finish_paypal_ping(self, auth_res: PayPalAuthResult) -> None:
        status_box = self.query_one("#settings-paypal-status", Static)
        if auth_res.authenticated:
            status_box.update(
                f"[bold green]● PayPal OAuth 2.0 Connected[/bold green] (App ID: `{auth_res.app_id}`, Latency: {auth_res.latency_ms:.0f}ms, Mode: {auth_res.mode.upper()})"
            )
            self.notify("PayPal OAuth 2.0 ping succeeded!", title="PayPal Connected", severity="information")
        else:
            status_box.update(
                f"[bold red][FAIL] PayPal Authentication Error:[/bold red] {auth_res.error}"
            )
            self.notify(f"PayPal Ping Failed: {auth_res.error}", title="PayPal Error", severity="error")

    @on(Button.Pressed, "#btn-test-ai")
    def on_btn_test_ai(self) -> None:
        status_box = self.query_one("#settings-ai-status", Static)
        prov_val = str(self.query_one("#settings-ai-provider", Select).value)
        status_box.update(f"[dim yellow]Pinging {prov_val.upper()} AI inference endpoint...[/dim yellow]")
        key_val = self.query_one("#settings-ai-key", Input).value.strip()
        model_val = self.query_one("#settings-ai-model", Input).value.strip()
        url_val = self.query_one("#settings-ai-base-url", Input).value.strip()
        self._run_ai_ping(prov_val, key_val, model_val, url_val)

    @work(thread=True, exclusive=True, group="ping-ai")
    def _run_ai_ping(self, provider: str, key: str, model: str, base_url: str) -> None:
        kwargs = {
            "ai_provider": provider,
            "ai_api_key": key,
            "ai_model": model,
            "ai_base_url": base_url,
        }
        if provider == "groq":
            kwargs["groq_api_key"] = key
        elif provider == "openai":
            kwargs["openai_api_key"] = key
        elif provider == "gemini":
            kwargs["gemini_api_key"] = key
        elif provider == "anthropic":
            kwargs["anthropic_api_key"] = key
        elif provider == "xai":
            kwargs["xai_api_key"] = key
        temp_settings = PulseSettings(**kwargs)
        res = verify_groq_connection(temp_settings, timeout=10.0)
        self.call_from_thread(self._finish_ai_ping, res, provider)

    def _finish_ai_ping(self, res: Any, provider: str) -> None:
        status_box = self.query_one("#settings-ai-status", Static)
        if res.connected:
            status_box.update(
                f"[bold green]● {provider.upper()} AI Connected[/bold green] (Model: `{res.model}`, Latency: {res.latency_ms:.0f}ms)"
            )
            self.notify(f"{provider.upper()} AI Connected ({res.latency_ms:.0f}ms)", title="AI Online", severity="information")
        else:
            status_box.update(
                f"[bold red][FAIL] {provider.upper()} Inference Error:[/bold red] {res.error}"
            )
            self.notify(f"AI Ping Failed: {res.error}", title="AI Offline", severity="error")

    @on(Button.Pressed, "#btn-save-settings")
    def on_btn_save_settings(self) -> None:
        pp_id = self.query_one("#settings-paypal-id", Input).value.strip()
        pp_sec = self.query_one("#settings-paypal-secret", Input).value.strip()
        mode_val = str(self.query_one("#settings-paypal-mode", Select).value)
        prov_val = str(self.query_one("#settings-ai-provider", Select).value)
        model_val = self.query_one("#settings-ai-model", Input).value.strip()
        key_val = self.query_one("#settings-ai-key", Input).value.strip()
        url_val = self.query_one("#settings-ai-base-url", Input).value.strip()
        auto_cap_val = str(self.query_one("#settings-auto-capture", Select).value).lower() == "true"

        kwargs = {
            "paypal_client_id": pp_id,
            "paypal_client_secret": pp_sec,
            "paypal_mode": mode_val,
            "ai_provider": prov_val,
            "ai_base_url": url_val,
            "ai_api_key": key_val,
            "ai_model": model_val,
            "auto_capture": auto_cap_val,
        }
        if prov_val == "groq":
            kwargs["groq_api_key"] = key_val
        elif prov_val == "openai":
            kwargs["openai_api_key"] = key_val
        elif prov_val == "gemini":
            kwargs["gemini_api_key"] = key_val
        elif prov_val == "anthropic":
            kwargs["anthropic_api_key"] = key_val
        elif prov_val == "xai":
            kwargs["xai_api_key"] = key_val

        cfg_path = save_global_config(**kwargs)

        # Hot-reload in memory while preserving isolated test fixtures and custom cache paths
        old_cache = self.settings.cache_dir
        old_db = self.settings.db_path
        default_settings = get_settings(demo_mode=self.demo_mode)
        self.settings = default_settings
        if old_cache != default_settings.cache_dir or old_db != default_settings.db_path:
            self.settings.cache_dir = old_cache
            self.settings.db_path = old_db

        self.pp_client = PayPalClient(self.settings)
        self.store = PulseStore(self.settings.db_path, settings=self.settings)
        self._auth_result = None

        self._populate_all()
        self._populate_settings(force=True)
        feedback = self.query_one("#settings-save-feedback", Static)
        feedback.update(
            f"[bold green]✓ Configuration saved to `{cfg_path}` and engine hot-reloaded successfully![/bold green]"
        )
        self.notify("Settings saved and hot-reloaded!", title="Saved", severity="information")

    @on(Button.Pressed, "#btn-reset-ledger")
    def on_btn_reset_ledger(self) -> None:
        self.store.reset(mark_clean=True)
        self._populate_all()
        db_status = self.query_one("#settings-db-status", Static)
        db_status.update("[bold green]✓ Local transaction history reset cleanly to zero records.[/bold green]")
        self.notify("Ledger reset to clean state.", title="Ledger Reset", severity="warning")

    @on(Button.Pressed, "#btn-toggle-demo-mode")
    def on_btn_toggle_demo_mode(self) -> None:
        self.demo_mode = not self.demo_mode
        old_cache = self.settings.cache_dir
        old_custom_db = self.settings.custom_db_path
        default_settings = get_settings(demo_mode=self.demo_mode)
        self.settings = default_settings
        if old_custom_db is not None:
            self.settings.custom_db_path = old_custom_db.parent / ("demo.db" if self.demo_mode else "pulse.db")
        elif old_cache != default_settings.cache_dir:
            self.settings.cache_dir = old_cache
            self.settings.db_path = old_cache / ("demo.db" if self.demo_mode else "pulse.db")

        self.pp_client = PayPalClient(self.settings)
        self.store = PulseStore(self.settings.db_path, settings=self.settings)
        self._auth_result = None
        self._populate_all()
        self._update_settings_db_telemetry()
        mode_label = "DEMO (Isolated)" if self.demo_mode else "LIVE (Local Dual-Engine)"
        db_status = self.query_one("#settings-db-status", Static)
        db_status.update(f"[bold cyan]Switched operating mode to: {mode_label}[/bold cyan]")
        self.notify(f"Switched database to {mode_label}", title="Mode Switched", severity="information")


