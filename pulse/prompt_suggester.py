"""Session-rotated suggested auto-chats for Pulse AI.

Provides archetype-balanced and telemetry-aware prompt suggestions for the
Textual TUI, dynamically selected per session.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pulse.store import PulseStore


@dataclass(frozen=True)
class PromptSuggestion:
    """A suggested quick-prompt chip for the Pulse AI interface."""

    chip_id: str
    button_label: str
    full_prompt: str
    category: str


# Pool definitions for each functional archetype
DISPUTES_OPEN_POOL: list[tuple[str, str]] = [
    (r"\[Why did DE dispute v1.2.1?]", "Why did German users open disputes on v1.2.1?"),
    (r"\[Dispute chargeback risk]", "Analyze open buyer dispute chargeback risk and financial exposure"),
    (r"\[Draft dispute evidence]", "Draft evidence package for open PayPal dispute with git commit history"),
    (r"\[Disputes by release]", "Which release version caused the highest rate of chargebacks and buyer disputes?"),
]

DISPUTES_CLEAN_POOL: list[tuple[str, str]] = [
    (r"\[Dispute rate audit]", "What is our current dispute rate compared to PayPal's 1% merchant threshold?"),
    (r"\[Refund rate check]", "Analyze our historical refund and chargeback rate across all software releases"),
    (r"\[Seller protection check]", "Are all recent orders covered under PayPal Seller Protection policy?"),
    (r"\[High-risk order audit]", "Identify any high-risk or unusual transaction spikes in the ledger"),
]

RELEASES_POOL: list[tuple[str, str]] = [
    (r"\[Who is our core userbase?]", "Who makes up the bulk of the userbase?"),
    (r"\[Compare v1.2.0 vs v1.2.1]", "Compare sales conversion and revenue velocity between v1.2.0 and v1.2.1"),
    (r"\[Top SKU conversion velocity]", "Which platform SKU or license tier generates the fastest revenue growth?"),
    (r"\[Release revenue spike]", "How do sales spike following new production git release tags?"),
    (r"\[Cohort retention rate]", "What is the repeat buyer rate across software version upgrades?"),
]

ECONOMICS_POOL: list[tuple[str, str]] = [
    (r"\[True net margin]", "What is our true net margin after PayPal fees?"),
    (r"\[PayPal fee drag breakdown]", "Break down total PayPal fees and effective percentage take-rate across currencies"),
    (r"\[Multi-currency FX drag]", "How much revenue drag comes from non-USD currencies and FX conversions?"),
    (r"\[CFO Health Index audit]", "Explain our CFO Merchant Health score and top margin improvement levers"),
    (r"\[30-day runway & revenue]", "Summarize our 30-day gross revenue, net take-home, and daily run-rate"),
]

ACTIONS_POOL: list[tuple[str, str]] = [
    (r"\[+ Mint $9.99 Order]", "Create a 9.99 USD order for PulseCore Pro Developer License"),
    (r"\[+ Mint €29 EUR License]", "Create a 29.00 EUR order for Pulse Enterprise Seat License"),
    (r"\[Draft $150 Client Invoice]", "Create an invoice for 150 USD to client@example.com for Custom Integration Support"),
    (r"\[+ Mint $49 Team Order]", "Create a 49.00 USD order for Developer Team 5-Seat License"),
    (r"\[Draft $500 SLA Invoice]", "Create an invoice for 500 USD to enterprise@corp.com for Annual SLA Tier 1"),
]

DEFAULT_PROMPT_SUGGESTIONS: list[PromptSuggestion] = [
    PromptSuggestion("chip-disputes", r"\[Why did DE dispute v1.2.1?]", "Why did German users open disputes on v1.2.1?", "disputes"),
    PromptSuggestion("chip-userbase", r"\[Who is our core userbase?]", "Who makes up the bulk of the userbase?", "releases"),
    PromptSuggestion("chip-margin", r"\[True net margin]", "What is our true net margin after PayPal fees?", "economics"),
    PromptSuggestion("chip-order", r"\[+ Mint $9.99 Order]", "Create a 9.99 USD order for PulseCore Pro Developer License", "actions"),
]


def get_session_prompt_suggestions(store: PulseStore | None = None) -> list[PromptSuggestion]:
    """Generate a balanced, telemetry-aware set of 4 quick-prompt suggestions for the session.

    Balances four distinct archetypes:
      1. Risk & Disputes (`chip-disputes`) - Context-aware based on active open disputes
      2. Releases & Cohorts (`chip-userbase`) - Conversion, version velocity, and buyer cohorts
      3. Economics & Margins (`chip-margin`) - Fee drag, multi-currency impact, and CFO health
      4. Sandbox Actions & Tools (`chip-order`) - Interactive order minting and invoice drafting
    """
    has_open_disputes = True  # Safe default

    if store is not None:
        try:
            disputes = store.get_disputes()
            open_count = sum(
                1 for d in disputes
                if str(d.get("status", "")).upper() in ("UNDER_REVIEW", "REQUIRED_ACTION", "OPEN")
            )
            has_open_disputes = open_count > 0
        except Exception:
            has_open_disputes = True

    dispute_pool = DISPUTES_OPEN_POOL if has_open_disputes else DISPUTES_CLEAN_POOL

    label_dispute, prompt_dispute = random.choice(dispute_pool)
    label_release, prompt_release = random.choice(RELEASES_POOL)
    label_econ, prompt_econ = random.choice(ECONOMICS_POOL)
    label_action, prompt_action = random.choice(ACTIONS_POOL)

    return [
        PromptSuggestion(
            chip_id="chip-disputes",
            button_label=label_dispute,
            full_prompt=prompt_dispute,
            category="disputes",
        ),
        PromptSuggestion(
            chip_id="chip-userbase",
            button_label=label_release,
            full_prompt=prompt_release,
            category="releases",
        ),
        PromptSuggestion(
            chip_id="chip-margin",
            button_label=label_econ,
            full_prompt=prompt_econ,
            category="economics",
        ),
        PromptSuggestion(
            chip_id="chip-order",
            button_label=label_action,
            full_prompt=prompt_action,
            category="actions",
        ),
    ]
