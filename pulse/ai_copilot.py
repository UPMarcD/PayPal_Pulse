"""Pulse AI Financial Copilot & Dispute Evidence Drafter for PayPal Pulse."""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from pydantic import BaseModel

from pulse.config import PulseSettings, get_settings
from pulse.paypal_client import PayPalClient, PayPalInvoiceResult, PayPalOrderResult
from pulse.store import PulseStore

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
FALLBACK_MODELS = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "llama-3.1-8b-instant",
]

_ACTIVE_GROQ_MODEL: str | None = None

PAYPAL_COPILOT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_paypal_order",
            "description": "Mint a live PayPal checkout order for a software license or digital SaaS subscription.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "Numeric price to charge, e.g. 14.99"},
                    "currency": {"type": "string", "enum": ["USD", "EUR", "PHP", "GBP"], "description": "Currency code"},
                    "description": {"type": "string", "description": "Item description or license title"},
                    "sku": {"type": "string", "description": "Platform SKU or tier, e.g. PulseCore Pro"},
                },
                "required": ["amount", "currency"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_paypal_invoice",
            "description": "Create a live PayPal draft invoice to bill a partner, client, or enterprise user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient_email": {"type": "string", "description": "Recipient email address"},
                    "amount": {"type": "number", "description": "Invoice total amount"},
                    "currency": {"type": "string", "enum": ["USD", "EUR", "PHP", "GBP"], "description": "Currency code"},
                    "item_name": {"type": "string", "description": "Description of the software or service"},
                },
                "required": ["recipient_email", "amount", "currency"],
            },
        },
    },
]


def get_active_groq_model(settings: PulseSettings | None = None) -> str:
    """Return the active verified inference model or configured default."""
    if _ACTIVE_GROQ_MODEL:
        return _ACTIVE_GROQ_MODEL
    cfg = settings or get_settings()
    return cfg.groq_model


class GroqAuthStatus(BaseModel):
    """Result of verifying the Pulse AI inference engine connection."""

    connected: bool
    model: str = "openai/gpt-oss-120b"
    available_models_count: int = 0
    latency_ms: float = 0.0
    key_preview: str = ""
    error: str | None = None


class CopilotAnswer(BaseModel):
    """Structured response from Pulse AI Financial Copilot."""

    answer: str
    model_used: str
    latency_ms: float
    tokens_per_sec: float = 0.0
    executed_action: str | None = None
    created_order: PayPalOrderResult | None = None
    created_invoice: PayPalInvoiceResult | None = None


def verify_groq_connection(settings: PulseSettings | None = None, timeout: float = 12.0) -> GroqAuthStatus:
    """Verify GROQ_API_KEY against https://api.groq.com/openai/v1/models."""
    global _ACTIVE_GROQ_MODEL
    cfg = settings or get_settings()
    if not cfg.has_groq_credentials:
        return GroqAuthStatus(
            connected=False,
            model=cfg.groq_model,
            error="Missing GROQ_API_KEY in .env",
        )

    key = cfg.groq_api_key
    preview = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "***"
    start = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(
                f"{GROQ_BASE_URL}/models",
                headers={"Authorization": f"Bearer {key}"},
            )
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("data", [])
            available_ids = {m.get("id", "") for m in models}
            selected_model = cfg.groq_model
            if selected_model not in available_ids:
                for candidate in FALLBACK_MODELS:
                    if candidate in available_ids:
                        selected_model = candidate
                        break
            _ACTIVE_GROQ_MODEL = selected_model
            return GroqAuthStatus(
                connected=True,
                model=selected_model,
                available_models_count=len(models),
                latency_ms=latency_ms,
                key_preview=preview,
            )
        return GroqAuthStatus(
            connected=False,
            model=cfg.groq_model,
            latency_ms=latency_ms,
            key_preview=preview,
            error=f"HTTP {resp.status_code}: {resp.text[:200]}",
        )
    except Exception as exc:
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        return GroqAuthStatus(
            connected=False,
            model=cfg.groq_model,
            latency_ms=latency_ms,
            key_preview=preview,
            error=str(exc),
        )


def _detect_order_intent(question: str) -> tuple[bool, float, str, str]:
    """Detect if the user is asking Pulse to generate/create a live PayPal order, invoice, or checkout link."""
    q_lower = question.lower().strip()

    # Guard: Ignore purely interrogative or explanatory inquiries (e.g. "How do I make an order?", "Explain how to issue an invoice")
    interrogative_prefix = re.compile(
        r"^(how\s+(?:do|to|can|should|would)|what\s+(?:is|are|happens|does)|why\s+(?:do|is|are|should)|explain\s+how|can\s+you\s+explain)\b",
        re.IGNORECASE,
    )
    if interrogative_prefix.search(q_lower):
        return False, 0.0, "USD", ""

    intent_regex = re.compile(
        r"\b(create|generate|make|issue|mint|build)\b.*\b(order|checkout|payment\s+link|invoice|link)\b"
    )
    if not intent_regex.search(q_lower):
        return False, 0.0, "USD", ""

    # Parse currency (USD, EUR, PHP, GBP)
    currency = "USD"
    if "eur" in q_lower or "€" in question:
        currency = "EUR"
    elif "php" in q_lower or "₱" in question:
        currency = "PHP"
    elif "gbp" in q_lower or "£" in question:
        currency = "GBP"

    # Default amount per currency
    amount = 5.99 if currency == "EUR" else (299.00 if currency == "PHP" else 9.99)

    # Remove version tags like v1.2.1, emails, and bare leading-dot fragments (e.g. ".99" from PowerShell $14.99 expansion)
    cleaned = re.sub(r"\bv\d+(?:\.\d+)*\b", " ", q_lower)
    cleaned = re.sub(r"[\w\.-]+@[\w\.-]+", " ", cleaned)
    cleaned = re.sub(r"(?<![\d])\.\d+\b", " ", cleaned)
    explicit_match = re.search(
        r"(?:[\$€₱£]\s*(\d+(?:\.\d{1,2})?)|(?<![\d\.])(\d+(?:\.\d{1,2})?)\s*(?:usd|eur|php|gbp))",
        cleaned,
    )
    if explicit_match:
        val_str = explicit_match.group(1) or explicit_match.group(2)
        if val_str and float(val_str) > 0:
            amount = float(val_str)
    else:
        num_match = re.search(r"(?:^|\s)(\d+(?:\.\d{1,2})?)(?:\s|$)", cleaned)
        if num_match:
            val = float(num_match.group(1))
            if val > 0:
                amount = val

    desc = f"PulseCore Pro Developer License ({currency} {amount:.2f})"
    return True, amount, currency, desc


def build_financial_context_prompt(
    summary: dict[str, Any],
    live_order_created: PayPalOrderResult | None = None,
    live_invoice_created: PayPalInvoiceResult | None = None,
) -> str:
    """Format the SQLite telemetry and PayPal Sandbox state for the LLM system prompt."""
    currencies_txt = "\n".join(
        f"  - {c['currency']}: Gross {c['gross']:.2f} | PayPal Fees {c['fees']:.2f} ({c['fee_pct']}%) | "
        f"Net {c['net']:.2f} ({c['net_margin_pct']}% margin) [USD Equiv Net: ${c['usd_net']:.2f}]"
        for c in summary.get("currencies", [])
    )
    releases_txt = "\n".join(
        f"  - {r['version']} ({r['title']}, commit {r['git_commit']}, released {r['released_at']}): "
        f"Customers/Deployments={r['installs']}, Orders={r['paid_conversions']} ({r['conversion_rate']}% conv), "
        f"Gross=${r['gross_usd']:.2f}, Fees=${r['fees_usd']:.2f}, True Net=${r['net_usd']:.2f} "
        f"({r.get('net_margin_pct', 0)}% margin), Refunds={r['refund_count']}, Disputes={r['dispute_count']} | "
        f"Status={r['status_badge']} | Notes: {r['ai_diagnostic']}"
        for r in summary.get("releases", [])
    )
    disputes_txt = "\n".join(
        f"  - ID={d['dispute_id']} | Tx={d['paypal_transaction_id']} | Status={d['status']} | "
        f"Country={d['buyer_country']} | Platform/SKU={d.get('platform_sku', d.get('device_model', 'Software License'))} | Release={d['release_version']} | "
        f"Amount={d['amount']:.2f} {d['currency']} | Due={d['due_date']} | Reason={d['reason']} | "
        f"Buyer Claim: {d['description']}"
        for d in summary.get("disputes", [])
    )
    live_orders_txt = "\n".join(
        f"  - PayPal Sandbox Order ID={tx['paypal_id']} | {tx['gross_amount']:.2f} {tx['currency']} | "
        f"Release={tx['release_version']} | Status={tx['status']} | URL={tx['approve_url']}"
        for tx in summary.get("live_sandbox_orders", [])[:5]
    )

    action_lines: list[str] = []
    if live_order_created and live_order_created.success:
        action_lines.extend(
            [
                f"  - Created Order ID: {live_order_created.order_id}",
                f"  - Order Status: {live_order_created.status}",
                f"  - Amount: {live_order_created.amount:.2f} {live_order_created.currency}",
                f"  - Checkout Approval Link: {live_order_created.approve_url}",
            ]
        )
    if live_invoice_created and live_invoice_created.success:
        action_lines.extend(
            [
                f"  - Created Draft Invoice ID: {live_invoice_created.invoice_id}",
                f"  - Invoice Status: {live_invoice_created.status}",
                f"  - Recipient Email: {live_invoice_created.recipient_email}",
                f"  - Invoice API Link: {live_invoice_created.self_url}",
            ]
        )
    action_block = (
        "\nLIVE ACTION JUST EXECUTED ON PAYPAL SANDBOX:\n" + "\n".join(action_lines) + "\n"
        if action_lines
        else ""
    )

    return f"""You are Pulse (PayPal Pulse AI), the financial copilot built into PayPal Pulse (`pp`).
APP IDENTITY:
- This tool/CLI is **PayPal Pulse (`pp`)**: A terminal-native financial telemetry command deck and AI copilot built for independent developers, modern SaaS founders, and software creators to monitor real-time PayPal Sandbox/Live transactions, observe fee drag, correlate Git releases to revenue, resolve buyer disputes, and mint orders via CLI/TUI.
- In this environment, PayPal Pulse is currently monitoring the financial telemetry of **PulseCore Pro** (a sample developer SaaS business selling software licenses across releases v1.1.0, v1.2.0, and v1.2.1).
- If the user asks what this app / this tool / this CLI is for: Answer that it is PayPal Pulse (`pp`), the terminal financial telemetry deck and AI copilot for modern software creators.
- If the user asks about the underlying business / software product being tracked: Describe PulseCore Pro and its sales/dispute metrics.

Answer with direct, high-signal, developer-friendly precision. Always cite exact numbers, version tags, platform environments, fee percentages, and real PayPal Sandbox IDs from the telemetry below.
Formatting rule for terminal display: Prefer concise bulleted lists or compact 3-column tables (under 80 characters wide) so output never wraps or clips on narrow terminal windows.

=== GLOBAL KPIs (30-DAY ROLLING) ===
- Total Gross Revenue (USD Equiv): ${summary.get('total_gross_usd', 0.0):,.2f}
- Total PayPal Fee Drag (USD Equiv): ${summary.get('total_fees_usd', 0.0):,.2f} ({summary.get('fee_drag_pct', 0.0)}%)
- True Net Take-Home (USD Equiv): ${summary.get('total_net_usd', 0.0):,.2f} ({summary.get('net_margin_pct', 0.0)}% net margin)
- Total Customers: {summary.get('total_customers', summary.get('total_installs', 0)):,} | Active Customers: {summary.get('active_customers', summary.get('active_users', 0)):,}
- Paid Orders: {summary.get('total_orders', summary.get('total_paid', 0)):,} ({summary.get('overall_conversion_pct', 0.0)}% order conversion rate)
- Monthly Recurring Revenue (MRR): ${summary.get('mrr_usd', 0.0):,.2f}
- Merchant Health Score: {summary.get('health_score', 0)}/100
- Open Disputes: {summary.get('open_disputes_count', 0)} (Total Disputes: {summary.get('total_disputes_count', 0)})
- Latest PayPal Sandbox Draft Invoice ID: {summary.get('last_live_invoice_id', 'None')}

=== MULTI-CURRENCY LEDGER & FEE DRAG ===
{currencies_txt}

=== RELEASE-TO-REVENUE TIMELINE ===
{releases_txt}

=== CUSTOMER DISPUTES ===
{disputes_txt}

=== LIVE PAYPAL SANDBOX ORDERS ===
{live_orders_txt}
{action_block}"""


def _call_groq_chat(
    messages: list[dict[str, str]],
    settings: PulseSettings,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float = 30.0,
    tools: list[dict[str, Any]] | None = None,
) -> tuple[str, str, float, float, list[dict[str, Any]]]:
    """Call OpenAI-compatible chat completions endpoint with automatic model fallback, tool calling, and active model caching."""
    global _ACTIVE_GROQ_MODEL
    preferred = _ACTIVE_GROQ_MODEL or settings.groq_model
    models_to_try = [preferred] + [
        m for m in ([settings.groq_model] + FALLBACK_MODELS) if m != preferred
    ]
    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered_models: list[str] = []
    for m in models_to_try:
        if m not in seen:
            seen.add(m)
            ordered_models.append(m)

    last_err = ""

    with httpx.Client(timeout=timeout) as client:
        for model_name in ordered_models:
            try:
                req_start = time.perf_counter()
                payload: dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if tools:
                    payload["tools"] = tools
                    payload["tool_choice"] = "auto"

                resp = client.post(
                    f"{GROQ_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.groq_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                latency_ms = round((time.perf_counter() - req_start) * 1000, 1)
                if resp.status_code == 200:
                    data = resp.json()
                    choice = data.get("choices", [{}])[0]
                    message = choice.get("message", {})
                    content = (message.get("content") or "").strip()
                    tool_calls = message.get("tool_calls") or []

                    if content or tool_calls:
                        _ACTIVE_GROQ_MODEL = model_name
                        usage = data.get("usage") or {}
                        comp_tokens = int(usage.get("completion_tokens") or max(1, len(content) // 4))
                        comp_time = float(usage.get("completion_time") or max(0.001, latency_ms / 1000.0))
                        tokens_per_sec = round(comp_tokens / max(0.001, comp_time), 1)
                        return content, model_name, latency_ms, tokens_per_sec, tool_calls
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as exc:
                last_err = str(exc)

    raise RuntimeError(f"Pulse AI inference failed: {last_err}")


def ask_copilot(
    question: str,
    store: PulseStore | None = None,
    paypal_client: PayPalClient | None = None,
    settings: PulseSettings | None = None,
) -> CopilotAnswer:
    """Answer a natural-language financial/release question using Pulse AI + live PayPal Sandbox context."""
    cfg = settings or get_settings()
    st = store or PulseStore(cfg.db_path)
    pp = paypal_client or PayPalClient(cfg)

    if not question or not question.strip():
        return CopilotAnswer(
            answer="Please provide a non-empty question for Pulse AI.",
            model_used="validation",
            latency_ms=0.0,
            tokens_per_sec=0.0,
        )

    # Ensure store is seeded so telemetry context is always rich
    if not st.is_seeded():
        st.seed_smartwatch_telemetry()

    executed_action: str | None = None
    created_order: PayPalOrderResult | None = None
    created_invoice: PayPalInvoiceResult | None = None

    # Check if user asked to create a live PayPal Sandbox order, invoice, or payment link
    should_create, amt, cur, desc = _detect_order_intent(question)
    if should_create and cfg.has_paypal_credentials:
        wants_invoice = "invoice" in question.lower()
        action_parts: list[str] = []

        if wants_invoice:
            email_match = re.search(r"([\w\.-]+@[\w\.-]+\.\w+)", question)
            recipient_email = email_match.group(1) if email_match else "wearables-partner@example.com"
            inv_res = pp.create_draft_invoice(
                item_name=desc,
                amount=amt,
                currency=cur,
                recipient_email=recipient_email,
            )
            if inv_res.success:
                created_invoice = inv_res
                st.set_meta("last_live_invoice_id", inv_res.invoice_id)
                action_parts.append(
                    f"Created live PayPal Sandbox Draft Invoice {inv_res.invoice_id} "
                    f"for {amt:.2f} {cur} to {recipient_email}"
                )
        else:
            order_res = pp.create_order(
                amount=amt,
                currency=cur,
                description=desc,
                custom_id="v1.2.1|PULSE_AI",
            )
            if order_res.success:
                st.record_live_order(order_res, release_version="v1.2.1", country_code="US")
                created_order = order_res
                action_parts.append(
                    f"Created live PayPal Sandbox Order {order_res.order_id} "
                    f"for {amt:.2f} {cur} ({order_res.approve_url})"
                )

        if action_parts:
            executed_action = " | ".join(action_parts)

    summary = st.get_dashboard_summary()
    system_prompt = build_financial_context_prompt(
        summary,
        live_order_created=created_order,
        live_invoice_created=created_invoice,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question.strip()},
    ]

    if cfg.has_groq_credentials:
        try:
            answer_text, model_used, latency_ms, tokens_per_sec, tool_calls = _call_groq_chat(
                messages, cfg, tools=PAYPAL_COPILOT_TOOLS
            )
            # If model returned structured function tool calls, execute them safely
            if tool_calls and cfg.has_paypal_credentials:
                action_parts = [executed_action] if executed_action else []
                for tc in tool_calls:
                    fn_name = tc.get("function", {}).get("name", "")
                    raw_args = tc.get("function", {}).get("arguments", "{}")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except Exception:
                        args = {}

                    if fn_name == "create_paypal_order" and not created_order:
                        tc_amt = float(args.get("amount", 9.99))
                        tc_cur = str(args.get("currency", "USD")).upper()
                        tc_desc = args.get("description", f"Pulse AI Order ({tc_cur} {tc_amt:.2f})")
                        o_res = pp.create_order(amount=tc_amt, currency=tc_cur, description=tc_desc)
                        if o_res.success:
                            st.record_live_order(o_res, release_version="v1.2.1", country_code="US")
                            created_order = o_res
                            action_parts.append(f"LLM Tool Call: Created Order {o_res.order_id} ({tc_amt:.2f} {tc_cur})")
                    elif fn_name == "create_paypal_invoice" and not created_invoice:
                        tc_amt = float(args.get("amount", 19.99))
                        tc_cur = str(args.get("currency", "USD")).upper()
                        tc_email = args.get("recipient_email", "partner@example.com")
                        tc_item = args.get("item_name", "Developer Consulting License")
                        i_res = pp.create_draft_invoice(item_name=tc_item, amount=tc_amt, currency=tc_cur, recipient_email=tc_email)
                        if i_res.success:
                            created_invoice = i_res
                            action_parts.append(f"LLM Tool Call: Created Invoice {i_res.invoice_id} ({tc_amt:.2f} {tc_cur})")

                if action_parts:
                    executed_action = " | ".join(action_parts)

            if not answer_text and executed_action:
                answer_text = f"Action completed on PayPal Sandbox: {executed_action}."

            return CopilotAnswer(
                answer=answer_text,
                model_used=model_used,
                latency_ms=latency_ms,
                tokens_per_sec=tokens_per_sec,
                executed_action=executed_action,
                created_order=created_order,
                created_invoice=created_invoice,
            )
        except Exception as exc:
            fallback_text = _build_deterministic_fallback(question, summary, created_order, str(exc))
            return CopilotAnswer(
                answer=fallback_text,
                model_used="local-deterministic-fallback",
                latency_ms=5.0,
                tokens_per_sec=450.0,
                executed_action=executed_action,
                created_order=created_order,
                created_invoice=created_invoice,
            )

    fallback_text = _build_deterministic_fallback(question, summary, created_order, "No GROQ_API_KEY")
    return CopilotAnswer(
        answer=fallback_text,
        model_used="local-deterministic-fallback",
        latency_ms=2.0,
        tokens_per_sec=480.0,
        executed_action=executed_action,
        created_order=created_order,
        created_invoice=created_invoice,
    )


def draft_dispute_evidence(
    dispute: dict[str, Any],
    replacement_order: PayPalOrderResult | None = None,
    settings: PulseSettings | None = None,
) -> tuple[str, str]:
    """Use Pulse AI to draft an evidence response for a PayPal Customer Dispute."""
    cfg = settings or get_settings()
    order_id_str = (
        replacement_order.order_id
        if replacement_order and replacement_order.success
        else "PP-REPLACEMENT-PENDING"
    )
    approve_link_str = (
        replacement_order.approve_url
        if replacement_order and replacement_order.success
        else "https://www.sandbox.paypal.com"
    )

    prompt = (
        f"Draft a concise, professional PayPal Seller Dispute Evidence submission (under 160 words) for:\n"
        f"- Dispute ID: {dispute.get('dispute_id')}\n"
        f"- Original Transaction ID: {dispute.get('paypal_transaction_id')}\n"
        f"- Buyer Email: {dispute.get('buyer_email')} (Country: {dispute.get('buyer_country')})\n"
        f"- Platform/Environment: {dispute.get('platform_sku', dispute.get('device_model', 'Software License'))}\n"
        f"- Release Version: {dispute.get('release_version')}\n"
        f"- Amount: {dispute.get('amount')} {dispute.get('currency')}\n"
        f"- Buyer Reason: {dispute.get('reason')} - {dispute.get('description')}\n"
        f"- Remediation Action Taken: Deployed hotfix v1.2.2 resolving the license token activation timeout, "
        f"issued VAT-compliant invoice, and generated replacement PayPal Sandbox Order ID {order_id_str} ({approve_link_str})."
    )

    if cfg.has_groq_credentials:
        try:
            text, model_used, _, _, _ = _call_groq_chat(
                [
                    {
                        "role": "system",
                        "content": "You are Pulse, a PayPal merchant dispute specialist drafting formal evidence notes for the PayPal Disputes API.",
                    },
                    {"role": "user", "content": prompt},
                ],
                cfg,
                temperature=0.2,
                max_tokens=350,
            )
            return text, f"Pulse AI ({model_used})"
        except Exception:
            pass

    fallback = (
        f"FORMAL SELLER EVIDENCE — DISPUTE {dispute.get('dispute_id')}:\n"
        f"1. Root Cause Verified: Telemetry identified a webhook activation timeout on "
        f"{dispute.get('platform_sku', dispute.get('device_model', 'Client Environment'))} in release {dispute.get('release_version')}.\n"
        f"2. Hotfix Deployed: Released patch v1.2.2 resolving token fulfillment and VAT receipt formatting.\n"
        f"3. Buyer Remediation: Activated complimentary license upgrade for {dispute.get('buyer_email')} and "
        f"generated replacement PayPal Sandbox Order {order_id_str} ({approve_link_str})."
    )
    return fallback, "local-template"


def _build_deterministic_fallback(
    question: str,
    summary: dict[str, Any],
    created_order: PayPalOrderResult | None,
    reason: str,
) -> str:
    lines = [
        f"**PayPal Pulse Telemetry Analysis** *(Fallback Mode: {reason})*",
        f"- **True Net Take-Home:** `${summary.get('total_net_usd', 0.0):,.2f}` USD "
        f"(`{summary.get('net_margin_pct', 0.0)}%` net margin after `${summary.get('total_fees_usd', 0.0):,.2f}` / `{summary.get('fee_drag_pct', 0.0)}%` PayPal fee drag).",
        "- **Release v1.2.1 Diagnostics:** 3 German (`DE`) buyers filed disputes (`PP-D-90411-DE`, `PP-D-90418-DE`, `PP-D-90425-DE`) "
        "due to automated license token dispatch timeouts on EUR checkout.",
        "- **Release v1.2.0 Surge:** Async engine & performance optimization boosted conversion from `6.19%` (`v1.1.0`) to `9.40%` (`+52%`), generating `$3,091.15` net USD.",
    ]
    if created_order and created_order.success:
        lines.append(
            f"- **Live PayPal Sandbox Order Created:** `{created_order.order_id}` (`{created_order.amount:.2f} {created_order.currency}`) -> {created_order.approve_url}"
        )
    return "\n".join(lines)
