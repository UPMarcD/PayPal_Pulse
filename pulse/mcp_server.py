"""Model Context Protocol (MCP) Stdio Server for PayPal Pulse.

Enables Claude Desktop, Cursor, Antigravity, Claude Code, and other AI agents
to query PayPal telemetry, observe fee drag, and mint live Sandbox orders via MCP.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from pulse.config import PulseSettings, get_settings
from pulse.paypal_client import PayPalClient
from pulse.store import PulseStore

PROTOCOL_VERSION = "2024-11-05"

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
                "currency": {"type": "string", "enum": ["USD", "EUR", "PHP", "GBP"], "description": "Currency code"},
                "description": {"type": "string", "description": "License or tier description"},
                "sku": {"type": "string", "description": "Product SKU or version tag"},
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
                "currency": {"type": "string", "enum": ["USD", "EUR", "PHP", "GBP"], "description": "Currency code"},
                "item_name": {"type": "string", "description": "Line item description"},
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
]


def handle_tool_call(name: str, arguments: dict[str, Any], settings: PulseSettings) -> dict[str, Any]:
    """Execute the requested tool against PulseStore and PayPalClient."""
    store = PulseStore(settings.db_path)
    if not store.is_seeded():
        store.seed_telemetry()
    pp_client = PayPalClient(settings)

    if name == "pulse_get_financial_summary":
        summary = store.get_dashboard_summary()
        return {"content": [{"type": "text", "text": json.dumps(summary, indent=2)}], "isError": False}

    if name == "pulse_get_disputes":
        only_open = arguments.get("only_open", True)
        disputes = store.get_disputes()
        if only_open:
            disputes = [d for d in disputes if d.get("status") == "OPEN"]
        return {"content": [{"type": "text", "text": json.dumps(disputes, indent=2)}], "isError": False}

    if name == "pulse_get_timeline":
        since = arguments.get("since")
        releases = store.get_releases(include_git_tags=True)
        if since:
            releases = [r for r in releases if r.get("released_at", "")[:10] >= since or since in r.get("released_at", "")]
        return {"content": [{"type": "text", "text": json.dumps(releases, indent=2)}], "isError": False}

    if name == "pulse_create_order":
        amount = float(arguments["amount"])
        currency = str(arguments["currency"]).upper()
        desc = arguments.get("description", "PulseCore Pro License")
        sku = arguments.get("sku", "PulseCore Pro")
        res = pp_client.create_order(
            amount=amount,
            currency=currency,
            description=desc,
            custom_id=f"mcp_{sku.lower().replace(' ', '_')}",
        )
        if res.success:
            store.record_live_order(order=res, release_version="v1.2.1", platform_sku=sku)
            return {"content": [{"type": "text", "text": json.dumps(res.model_dump(), indent=2)}], "isError": False}
        return {"content": [{"type": "text", "text": f"PayPal API Error: {res.error}"}], "isError": True}

    if name == "pulse_create_invoice":
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
    sys.stderr.flush()

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

        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {})
            call_res = handle_tool_call(name=name, arguments=args, settings=settings)
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
