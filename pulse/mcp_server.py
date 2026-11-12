"""Model Context Protocol (MCP) Stdio Server for PayPal Pulse.

Enables Claude Desktop, Cursor, Antigravity, Claude Code, and other AI agents
to query PayPal telemetry, observe fee drag, and mint live Sandbox orders via MCP.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from typing import Any

from pulse.config import PulseSettings, get_settings
from pulse.paypal_client import PayPalClient
from pulse.store import OPEN_DISPUTE_STATUSES, PulseStore, detect_project_context

PROTOCOL_VERSION = "2024-11-05"

ALL_PAYPAL_CURRENCIES = [
    "USD", "EUR", "PHP", "GBP", "CAD", "AUD", "JPY", "SGD", "HKD", "NZD",
    "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "ILS", "MXN", "BRL",
    "MYR", "TWD", "THB", "INR", "CNY",
]

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "pulse_get_financial_summary",
        "description": "Fetch 30-day PayPal revenue, fees, net margin, and multi-currency metrics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "demo": {"type": "boolean", "description": "Whether to query isolated demo database"}
            },
        },
    },
    {
        "name": "pulse_get_disputes",
        "description": "List active PayPal buyer disputes with reason, due date, and root cause.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "only_open": {"type": "boolean", "description": "Filter to only open/unresolved disputes"}
            },
        },
    },
    {
        "name": "pulse_create_order",
        "description": "Mint a live PayPal Sandbox checkout order for digital software licenses.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Numeric amount to charge (e.g. 14.99)"},
                "currency": {"type": "string", "enum": ALL_PAYPAL_CURRENCIES, "description": "Currency code"},
                "description": {"type": "string", "description": "License or tier description"},
                "sku": {"type": "string", "description": "Product SKU or version tag"},
                "release_version": {"type": "string", "description": "Release version tag to attribute this order to (e.g. v1.3.0)"},
                "confirm": {"type": "boolean", "description": "Explicit confirmation required for live financial mutations when PAYPAL_MODE=live"},
            },
            "required": ["amount", "currency"],
        },
    },
    {
        "name": "pulse_create_invoice",
        "description": "Draft a live PayPal Sandbox invoice to bill a client or partner.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipient_email": {"type": "string", "description": "Client email address"},
                "amount": {"type": "number", "description": "Invoice total amount"},
                "currency": {"type": "string", "enum": ALL_PAYPAL_CURRENCIES, "description": "Currency code"},
                "item_name": {"type": "string", "description": "Line item description"},
                "confirm": {"type": "boolean", "description": "Explicit confirmation required for live financial mutations when PAYPAL_MODE=live"},
            },
            "required": ["recipient_email", "amount", "currency"],
        },
    },
    {
        "name": "pulse_get_timeline",
        "description": "Correlate Git release tags (v1.1.0, v1.2.0, v1.2.1) with PayPal revenue, conversion rates, and dispute surges.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "Date filter (YYYY-MM-DD)"}
            },
        },
    },
    {
        "name": "pulse_draft_dispute_evidence",
        "description": "Draft a professional AI evidence package and response narrative for an active buyer dispute using Groq/Llama or deterministic fallback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dispute_id": {"type": "string", "description": "The PayPal Dispute ID (e.g. PP-D-90425-DE)"},
                "demo": {"type": "boolean", "description": "Whether to query isolated demo database"}
            },
            "required": ["dispute_id"],
        },
    },
    {
        "name": "pulse_simulate_webhook",
        "description": "Synthesize and ingest a realistic PayPal webhook event (e.g. payment.capture.completed, customer.dispute.created) with release attribution.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_type": {"type": "string", "enum": ["payment.capture.completed", "checkout.order.approved", "customer.dispute.created", "customer.dispute.resolved"], "description": "Webhook event name"},
                "amount": {"type": "number", "description": "Transaction or dispute amount"},
                "currency": {"type": "string", "enum": ALL_PAYPAL_CURRENCIES, "description": "PayPal 3-letter currency code"},
                "release_version": {"type": "string", "description": "Git release version to attribute the event to"},
                "sku": {"type": "string", "description": "Platform SKU or product tier"},
            },
        },
    },
]


def handle_tool_call(name: str, arguments: dict[str, Any], settings: PulseSettings) -> dict[str, Any]:
    """Execute the requested tool against PulseStore and PayPalClient."""
    use_demo = bool(arguments.get("demo", False))
    target_settings = get_settings(demo_mode=True) if (use_demo or settings.demo_mode) else settings
    store = PulseStore(target_settings.db_path)
    if store.should_auto_seed():
        store.seed_telemetry()
    pp_client = PayPalClient(target_settings)

    if name == "pulse_get_financial_summary":
        summary = store.get_dashboard_summary()
        return {"content": [{"type": "text", "text": json.dumps(summary, indent=2)}], "isError": False}

    if name == "pulse_get_disputes":
        only_open = arguments.get("only_open", True)
        disputes = store.get_disputes()
        if only_open:
            disputes = [
                d
                for d in disputes
                if str(d.get("status") or "").upper() in OPEN_DISPUTE_STATUSES
            ]
        return {"content": [{"type": "text", "text": json.dumps(disputes, indent=2)}], "isError": False}

    if name == "pulse_draft_dispute_evidence":
        dispute_id = str(arguments.get("dispute_id", "")).strip()
        disputes = store.get_disputes()
        matching = [d for d in disputes if d.get("dispute_id") == dispute_id]
        if not matching:
            return {"content": [{"type": "text", "text": f"Dispute not found: {dispute_id}"}], "isError": True}
        from pulse.ai_copilot import draft_dispute_evidence
        draft_text, engine = draft_dispute_evidence(matching[0], settings=target_settings)
        payload = {
            "dispute_id": dispute_id,
            "engine": engine,
            "evidence_draft": draft_text,
        }
        return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}], "isError": False}

    if name == "pulse_get_timeline":
        since = arguments.get("since")
        releases = store.get_releases(include_git_tags=True)
        if since:
            releases = [r for r in releases if r.get("released_at", "")[:10] >= since or since in r.get("released_at", "")]
        return {"content": [{"type": "text", "text": json.dumps(releases, indent=2)}], "isError": False}

    is_live_mode = target_settings.paypal_mode.lower() == "live"

    if name == "pulse_create_order":
        is_confirmed = bool(arguments.get("confirm", False))
        if is_live_mode and not is_confirmed:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[LIVE MODE GUARD (OWASP LLM06/LLM08)] Blocked autonomous live order creation. "
                            "Operating in live PayPal production environment. "
                            "Pass 'confirm': true in arguments to explicitly authorize this real financial mutation."
                        ),
                    }
                ],
                "isError": True,
            }
        ctx = detect_project_context()
        default_sku = str(ctx.get("default_sku") or "PulseCore Pro")
        amount = float(arguments.get("amount", ctx.get("default_price", 9.99)))
        currency = str(arguments.get("currency", ctx.get("default_currency", "USD"))).upper()
        desc = arguments.get("description", f"{default_sku} License")
        sku = arguments.get("sku", default_sku)
        rel_ver = str(arguments.get("release_version") or ctx.get("version") or "v1.2.1")
        res = pp_client.create_order(
            amount=amount,
            currency=currency,
            description=desc,
            custom_id=f"release:{rel_ver}|US|{sku}",
        )
        if res.success:
            store.record_live_order(order=res, release_version=rel_ver, platform_sku=sku)
            return {"content": [{"type": "text", "text": json.dumps(res.model_dump(), indent=2)}], "isError": False}
        return {"content": [{"type": "text", "text": f"PayPal API Error: {res.error}"}], "isError": True}

    if name == "pulse_simulate_webhook":
        import uuid

        ctx = detect_project_context()
        default_sku = str(ctx.get("default_sku") or "PulseCore Pro")
        evt_type = str(arguments.get("event_type", "payment.capture.completed"))
        amt = float(arguments.get("amount", ctx.get("default_price", 19.99)))
        cur = str(arguments.get("currency", ctx.get("default_currency", "USD"))).upper()
        sku = str(arguments.get("sku", default_sku))
        rel_ver = str(arguments.get("release_version") or ctx.get("version") or "v1.2.1")
        uniq_suffix = uuid.uuid4().hex[:7].upper()

        event_payload = {
            "id": f"WH-MCP-{uniq_suffix}",
            "event_type": evt_type.upper().replace(".", "_"),
            "create_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "resource": {
                "id": f"RES-MCP-{uniq_suffix}",
                "status": "COMPLETED",
                "amount": {"value": str(amt), "currency_code": cur},
                "release_version": rel_ver,
                "custom_id": f"release:{rel_ver}|US|{sku}",
            },
        }
        receipt = store.record_webhook_event(event_payload)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"status": "INGESTED", "event": event_payload, "receipt": receipt}, indent=2),
                }
            ],
            "isError": False,
        }

    if name == "pulse_create_invoice":
        is_confirmed = bool(arguments.get("confirm", False))
        if is_live_mode and not is_confirmed:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[LIVE MODE GUARD (OWASP LLM06/LLM08)] Blocked autonomous live invoice creation. "
                            "Operating in live PayPal production environment. "
                            "Pass 'confirm': true in arguments to explicitly authorize this real financial mutation."
                        ),
                    }
                ],
                "isError": True,
            }
        email = str(arguments["recipient_email"])
        amount = float(arguments["amount"])
        currency = str(arguments["currency"]).upper()
        item = arguments.get("item_name", "Enterprise License")
        res = pp_client.create_draft_invoice(recipient_email=email, amount=amount, currency=currency, item_name=item)
        if res.success:
            return {"content": [{"type": "text", "text": json.dumps(res.model_dump(), indent=2)}], "isError": False}
        return {"content": [{"type": "text", "text": f"PayPal Invoicing Error: {res.error}"}], "isError": True}

    return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}


def run_mcp_stdio(demo_mode: bool = False) -> None:
    """Run the JSON-RPC Model Context Protocol loop over stdin and stdout."""
    settings = get_settings(demo_mode=demo_mode)
    sys.stderr.write(f"[Pulse MCP] Server running on stdio (demo_mode={demo_mode})\n")
    if sys.stdin.isatty():
        sys.stderr.write(
            "[Pulse MCP] Note: This server is designed for AI agents (Antigravity, Claude, Cursor).\n"
            "[Pulse MCP] Listening on stdin for JSON-RPC 2.0 messages (press Ctrl+C to exit).\n"
            "[Pulse MCP] Quick test: '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}' | pp mcp\n"
        )
    sys.stderr.flush()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})

            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "paypal-pulse", "version": "0.1.0"},
                    },
                }
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

            elif method == "notifications/initialized":
                # No response expected for notifications
                pass

            elif method == "ping":
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

            elif method == "tools/list":
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

            elif method == "resources/list":
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

            elif method == "prompts/list":
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

            elif method == "tools/call":
                name = params.get("name", "")
                args = params.get("arguments", {})
                try:
                    call_res = handle_tool_call(name=name, arguments=args, settings=settings)
                except Exception as e:
                    call_res = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": call_res}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()

            elif msg_id is not None:
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method '{method}' not found"},
                }
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
    except KeyboardInterrupt:
        sys.stderr.write("\n[Pulse MCP] Shutdown requested. Exiting cleanly.\n")
        sys.stderr.flush()
