"""First-run onboarding UI helpers for PayPal Pulse (welcome, selectors, live checklist, animations)."""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, Optional, Sequence, TypeVar

import typer
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from pulse.mascot import (
    PULSE_MASCOT_ALERT,
    PULSE_MASCOT_IDLE,
    PULSE_MASCOT_SUCCESS,
    PULSE_MASCOT_THINK,
    format_mascot_header,
)

T = TypeVar("T")

ACCENT = "#00a8ea"
BORDER = "#0070ba"
SUCCESS = "#3fb950"
WARN = "#e5a824"
ALERT = "#f85149"

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

ECG_FRAMES = (
    "───────────────",
    "───ﮩ───────────",
    "───ﮩ٨ـ⚡────────",
    "──────ﮩ٨ـ⚡─────",
    "─────────ﮩ٨ـ⚡──",
    "───ﮩ٨ـ⚡───ﮩ٨ـ⚡─",
)

PAYPAL_KEYS_HELP = (
    "1. Visit [bold #00a8ea]https://developer.paypal.com/dashboard/applications/sandbox[/bold #00a8ea]\n"
    "2. Select or create an app under [bold]Apps & Credentials[/bold].\n"
    "3. Copy your [bold]Client ID[/bold] and [bold]Secret[/bold]."
)

MODE_HELP = (
    "[bold #00a8ea]sandbox[/bold #00a8ea]  Test sandbox mode (safe for testing & mock transactions)\n"
    "[bold #e5a824]live[/bold #e5a824]     Live production mode (real payments & revenue)"
)


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def should_animate() -> bool:
    """Return True only on real interactive TTYs when animations are not disabled."""
    if not is_interactive():
        return False
    if os.getenv("NO_COLOR") or os.getenv("PULSE_NO_ANIM") or os.getenv("CI"):
        return False
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False
    return True


def _questionary_style():
    from questionary import Style

    return Style(
        [
            ("qmark", f"fg:{ACCENT} bold"),
            ("question", "fg:#f0f6fc bold"),
            ("answer", f"fg:{SUCCESS} bold"),
            ("pointer", f"fg:{ACCENT} bold"),
            ("highlighted", f"fg:{ACCENT} bold"),
            ("selected", f"fg:{SUCCESS} bold"),
            ("separator", "fg:#8b949e"),
            ("instruction", "fg:#8b949e italic"),
            ("text", "fg:#c9d1d9"),
        ]
    )


def select(
    message: str,
    choices: Sequence[tuple[str, str]],
    default: Optional[str] = None,
) -> str:
    """Ask a single-choice question with Claude Code style pointer & highlighted rows.

    Uses arrow keys (questionary) on a real TTY, and falls back to numbered prompts otherwise.
    """
    values = [v for _, v in choices]
    labels_by_val = {v: label for label, v in choices}
    if is_interactive():
        try:
            import questionary

            items = [questionary.Choice(title=label, value=value) for label, value in choices]
            picked = questionary.select(
                message,
                choices=items,
                default=next((i for i in items if i.value == default), None),
                qmark="❯",
                pointer="❯",
                instruction="(↑/↓ navigate • enter select)",
                style=_questionary_style(),
            ).ask()
            if picked is None:
                raise typer.Exit(code=0)
            return picked
        except ImportError:
            pass
    con = Console()
    for idx, (label, _) in enumerate(choices, 1):
        con.print(f"  [bold {ACCENT}][{idx}][/bold {ACCENT}] {label}")
    default_idx = str(values.index(default) + 1) if default in values else "1"
    raw = typer.prompt(f"{message} [1-{len(choices)}]", default=default_idx).strip()
    if raw.isdigit() and 1 <= int(raw) <= len(choices):
        chosen = values[int(raw) - 1]
    else:
        chosen = values[int(default_idx) - 1]
    if is_interactive():
        con.print(f"[bold {SUCCESS}]✔[/bold {SUCCESS}] [bold]{message}:[/bold] [{ACCENT}]{labels_by_val.get(chosen, chosen)}[/{ACCENT}]")
    return chosen


def step_header(console: Console, number: int, total: int, title: str) -> None:
    """Render a Claude Code style segmented progress bar and rule header."""
    filled = "■" * number
    empty = "□" * max(0, total - number)
    bar = f"[bold {ACCENT}][{filled}[/bold {ACCENT}][dim]{empty}][/dim]"
    console.print()
    console.print(
        Rule(
            f"{bar} [bold {ACCENT}]Step {number}/{total}[/bold {ACCENT}] · [bold #f0f6fc]{title}[/bold #f0f6fc]",
            style=BORDER,
            align="left",
        )
    )


def _build_welcome_panel(first_run: bool, mascot_state: str = "idle", ecg_wave: str = "───ﮩ٨ـ⚡───") -> Panel:
    sprite = {
        "idle": PULSE_MASCOT_IDLE,
        "think": PULSE_MASCOT_THINK,
        "success": PULSE_MASCOT_SUCCESS,
        "alert": PULSE_MASCOT_ALERT,
    }.get(mascot_state, PULSE_MASCOT_IDLE)
    lines = sprite.split("\n")
    title_str = "Welcome to PayPal Pulse" if first_run else "PayPal Pulse"
    header = (
        f"{lines[0]}   [bold #f0f6fc]{title_str}[/bold #f0f6fc] [bold {WARN}]•[/bold {WARN}] [dim]financial telemetry for developers[/dim]  [bold {ACCENT}]{ecg_wave}[/bold {ACCENT}]\n"
        f"{lines[1]}   [dim]Real-time orders, fee analytics, and dispute protection — right in your terminal.[/dim]\n"
        f"{lines[2]}   [dim]Local zero-latency SQLite ledger • Native Git release correlation • AI Copilot[/dim]\n"
        f"{lines[3]}"
    )
    body = (
        header
        + "\n\n[bold #f0f6fc]Quick Tour[/bold #f0f6fc]\n"
        "  • [bold #00a8ea]Instant Visibility:[/bold #00a8ea] Track gross revenue, fee drag, and take-home net in real-time.\n"
        "  • [bold #00a8ea]Release Attribution:[/bold #00a8ea] Correlate git releases and tags directly to customer payments.\n"
        "  • [bold #00a8ea]Dispute Copilot:[/bold #00a8ea] Detect disputes early and auto-draft evidence with AI.\n"
        "  • [bold #00a8ea]Private & Local:[/bold #00a8ea] Credentials stay on your machine in [dim]~/.pulse/config.json[/dim].\n\n"
        "[dim]Common commands: [bold #f0f6fc]pp tui[/bold #f0f6fc] (full deck), [bold #f0f6fc]pp pulse[/bold #f0f6fc] (quick stats), [bold #f0f6fc]pp doctor[/bold #f0f6fc] (health check)[/dim]"
    )
    return Panel(body, title=f"[bold {ACCENT}]PAYPAL PULSE[/bold {ACCENT}]", border_style=BORDER, padding=(1, 2))


def welcome(console: Console, first_run: bool = True) -> None:
    """Print the welcome panel with an animated mascot & ECG heartbeat intro on interactive TTYs."""
    console.print()
    if should_animate():
        sequence = [
            ("idle", ECG_FRAMES[0]),
            ("think", ECG_FRAMES[1]),
            ("idle", ECG_FRAMES[2]),
            ("success", ECG_FRAMES[3]),
            ("idle", ECG_FRAMES[4]),
            ("idle", ECG_FRAMES[5]),
        ]
        with Live(_build_welcome_panel(first_run, "idle", ECG_FRAMES[0]), console=console, refresh_per_second=15, transient=False) as live:
            for state, wave in sequence:
                time.sleep(0.11)
                live.update(_build_welcome_panel(first_run, state, wave))
    else:
        console.print(_build_welcome_panel(first_run, "idle", ECG_FRAMES[-1]))


class LiveChecklist:
    """Multi-step live checklist that ticks off items (`⠋` -> `✔` / `⚠` / `✘`) without a mascot."""

    def __init__(self, console: Console, title: Optional[str] = None) -> None:
        self.console = console
        self.title = title
        self.items: list[tuple[str, str, str]] = []  # (label, status: running|ok|warn|fail, detail)
        self._live: Optional[Live] = None
        self._frame = 0

    def _render(self) -> Table:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(width=2)
        grid.add_column(style="bold #f0f6fc")
        grid.add_column()
        spin = SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]
        icons = {
            "running": f"[bold {ACCENT}]{spin}[/bold {ACCENT}]",
            "ok": f"[bold {SUCCESS}]✔[/bold {SUCCESS}]",
            "warn": f"[bold {WARN}]![/bold {WARN}]",
            "fail": f"[bold {ALERT}]✘[/bold {ALERT}]",
        }
        for label, status, detail in self.items:
            detail_styled = f"[dim]{detail}[/dim]" if status == "running" else detail
            grid.add_row(icons.get(status, "•"), label, detail_styled)
        return grid

    def __enter__(self) -> "LiveChecklist":
        if should_animate():
            self._live = Live(self._render(), console=self.console, refresh_per_second=12, transient=False)
            self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._live is not None:
            self._live.update(self._render())
            self._live.__exit__(exc_type, exc_val, exc_tb)
            self._live = None
        elif not should_animate() and self.items:
            self.console.print(self._render())

    def start_step(self, label: str, detail: str = "in progress...") -> int:
        idx = len(self.items)
        self.items.append((label, "running", detail))
        self._frame += 1
        if self._live is not None:
            self._live.update(self._render())
        return idx

    def finish_step(self, idx: int, status: str, detail: str = "") -> None:
        label, _, _ = self.items[idx]
        self.items[idx] = (label, status, detail)
        self._frame += 1
        if self._live is not None:
            self._live.update(self._render())

    def run_step(
        self,
        label: str,
        fn: Callable[[], T],
        running_detail: str = "checking...",
    ) -> T:
        idx = self.start_step(label, running_detail)
        if should_animate():
            for _ in range(3):
                time.sleep(0.05)
                self._frame += 1
                if self._live is not None:
                    self._live.update(self._render())
        return fn()


def celebration_banner(console: Console, title: str, subtitle: str) -> None:
    """Render a celebratory happy Pulsey header (`\\ ▄█▄ ▄█ /`) on setup completion."""
    console.print()
    console.print(
        Panel(
            format_mascot_header(title=title, subtitle=subtitle, state="success"),
            border_style=SUCCESS,
            padding=(0, 2),
        )
    )


def summary_table(
    console: Console,
    rows: Sequence[tuple[str, str]],
    title: str = "Setup summary",
    latency_ms: Optional[float] = None,
) -> None:
    def _build_table(lat_frac: float = 1.0) -> Table:
        table = Table(title=title, show_header=False, border_style=BORDER, title_style=f"bold {ACCENT}", box=box.ROUNDED)
        table.add_column(style="bold #c9d1d9")
        table.add_column()
        for key, val in rows:
            if key == "Latency" and latency_ms is not None and lat_frac < 1.0:
                table.add_row(key, f"{latency_ms * lat_frac:.1f} ms")
            else:
                table.add_row(key, val)
        return table

    if should_animate() and latency_ms is not None:
        steps = 6
        with Live(_build_table(0.2), console=console, refresh_per_second=20, transient=False) as live:
            for i in range(1, steps + 1):
                time.sleep(0.04)
                live.update(_build_table(i / steps))
    else:
        console.print(_build_table(1.0))


def try_now_cards(console: Console, sandbox_mode: bool = True) -> None:
    """Render styled Try-Now command cards after setup."""
    grid = Table.grid(padding=(0, 3))
    grid.add_column(style=f"bold {ACCENT}")
    grid.add_column(style="#c9d1d9")
    grid.add_row("pp tui", "Launch full-screen 6-tab terminal command deck")
    grid.add_row("pp pulse", "Instant revenue, fee drag & take-home breakdown")
    grid.add_row('pp ask "What is our net margin?"', "Query Pulse AI copilot in plain English")
    grid.add_row("pp init", "Link local Git release tags to payment telemetry")
    grid.add_row("pp doctor", "Run live diagnostic health checklist anytime")
    if sandbox_mode:
        grid.add_row("pp seed", "Mint realistic sample orders into your sandbox")
    console.print()
    console.print(Panel(grid, title=f"[bold {ACCENT}]Try Now[/bold {ACCENT}]", border_style=BORDER, padding=(0, 2)))
