"""ASCII & Unicode block-element mascot sprites for PayPal Pulse ("Pulsey").

Drawn in the retro quadrant-block style of Anthropic's Clawd (Claude Code mascot),
modeled after an ECG heartbeat pulse waveform with stateful terminal expressions.
"""

from __future__ import annotations

# 1. Idle / Welcome (Default companion state)
PULSE_MASCOT_IDLE = (
    "[bold #00a8ea]    ▄█▄  ▄█\n"
    " ·▛·█▀█·█▀█·▜·\n"
    "──▜·[bold #f0f6fc]●···●[/bold #f0f6fc]·▛·──\n"
    "   ▘▘ ▀█▀ ▝▝[/bold #00a8ea]"
)

# 2. Thinking / Ruminating (Groq LPU inference state)
PULSE_MASCOT_THINK = (
    "[bold #58a6ff]    ▄█▄  ▄█\n"
    " ·▛·█▀█·█▀█·▜·\n"
    "──▜·[bold #f0f6fc]-···-[/bold #f0f6fc]·▛·──\n"
    "   ▘▘ ▀█▀ ▝▝[/bold #58a6ff]"
)

# 3. Success / Order Minted (Happy state with raised arms)
PULSE_MASCOT_SUCCESS = (
    "[bold #3fb950]  \\ ▄█▄  ▄█ /\n"
    " ·▛·█▀█·█▀█·▜·\n"
    "──▜·[bold #f0f6fc]^···^[/bold #f0f6fc]·▛·──\n"
    "   ▘▘ ▀█▀ ▝▝[/bold #3fb950]"
)

# 4. Alert / Dispute Detected (Concerned state)
PULSE_MASCOT_ALERT = (
    "[bold #f85149]    ▄█▄  ▄█\n"
    " ·▛·█▀█·█▀█·▜·\n"
    "──▜·[bold #f0f6fc]!···![/bold #f0f6fc]·▛·──\n"
    "   ▘▘ ▀█▀ ▝▝[/bold #f85149]"
)

# Single-line micro glyph for tight terminal tables and status bars
PULSE_GLYPH = "[bold #00a8ea]Pulse[/bold #00a8ea] [bold #0070ba]ﮩ٨ـ⚡[/bold #0070ba]"


def format_mascot_header(
    title: str = "Meet Pulse ﮩ٨ـ⚡",
    subtitle: str = "Terminal-Native Financial Telemetry & AI Copilot",
    state: str = "idle",
) -> str:
    """Format a 4-line side-by-side header combining the mascot sprite and text."""
    sprite = {
        "idle": PULSE_MASCOT_IDLE,
        "think": PULSE_MASCOT_THINK,
        "success": PULSE_MASCOT_SUCCESS,
        "alert": PULSE_MASCOT_ALERT,
    }.get(state, PULSE_MASCOT_IDLE)

    lines = sprite.split("\n")
    return (
        f"{lines[0]}   [bold #f0f6fc]{title}[/bold #f0f6fc]\n"
        f"{lines[1]}   [dim]{subtitle}[/dim]\n"
        f"{lines[2]}   [dim]PayPal Sandbox Orders • Dispute Evidence • Git Correlation[/dim]\n"
        f"{lines[3]}"
    )
