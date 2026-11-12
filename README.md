# PayPal Pulse (`pp`)

[![License: MIT](https://img.shields.io/badge/License-MIT-58a6ff.svg)](LICENSE)

**Terminal-Native Financial Telemetry & Pulse AI Copilot for Modern SaaS & Software Creators**

PayPal Pulse (`pp`) brings real-time PayPal Sandbox/Live financial observability, Git release-to-revenue correlation, proactive dispute resolution, and **Pulse AI** (Financial Intelligence Copilot) directly into your developer terminal.

---

## Features & Architecture

1. **Real PayPal Sandbox Integration (`pulse/paypal_client.py`)**
   - Authenticates via OAuth 2.0 Client Credentials (`POST /v1/oauth2/token` against `https://api-m.sandbox.paypal.com`).
   - Integrates with PayPal REST APIs:
     - **Balances API** (`GET /v1/reporting/balances`)
     - **Transaction Search API** (`GET /v1/reporting/transactions`)
     - **Customer Disputes API** (`GET /v1/customer/disputes` & `/provide-evidence`)
     - **Invoicing v2 API** (`GET /v2/invoicing/invoices` & `POST /v2/invoicing/invoices`)
     - **Orders v2 API** (`POST /v2/checkout/orders` & `GET /v2/checkout/orders/{id}`)
2. **Dual-Engine Instant Storage (`pulse/store.py`)**
   - Stores real PayPal Sandbox Order/Invoice/Transaction IDs in a local SQLite database (`.pulse_cache/pulse.db`) alongside modern SaaS software telemetry (`v1.1.0`, `v1.2.0`, `v1.2.1`, install counts, platform SKUs, multi-currency fee drag) so newly created sandbox orders appear instantaneously without waiting for the Reporting API's 3-hour indexing latency.
3. **Pulse AI Financial Copilot (`pulse/ai_copilot.py`)**
   - Claude-inspired interactive terminal financial copilot (`Pulse`) with live tool execution.
   - Answers natural language questions over live PayPal telemetry and executes live PayPal Sandbox actions (e.g., generating live checkout links or drafting dispute evidence).

---

## Quick Start

### 1. Instant Exploration (Zero-Setup Demo Tour)
If you want to immediately explore PayPal Pulse with realistic SaaS telemetry without creating any accounts or entering API keys:
```bash
# Install in editable mode
pip install -e .

# Launch the guided interactive onboarding demo tour
pp demo

# Or directly open the full-screen interactive Terminal UI in demo mode
pp tui --demo
```

---

### 2. Live PayPal Sandbox Mode
When you are ready to connect to your live PayPal Developer Sandbox:
```bash
# Run the interactive setup wizard (saves verified credentials to ~/.pulse/config.json)
pp setup

# Or verify existing credentials
pp auth

# View the rich terminal financial & software telemetry dashboard
pp pulse

# Inspect Git release-to-revenue correlation timeline
pp timeline

# View active buyer disputes & auto-resolve with Pulse AI + replacement order
pp disputes
pp disputes --resolve PP-D-90411-DE --yes

# Simulate an inbound PayPal webhook into the local dual-engine ledger
pp trigger payment.capture.completed

# Chat with Pulse AI Financial Copilot
pp ask "What is our net margin after PayPal fees?"

# Launch the full-screen interactive Textual command center
pp tui

# Run Model Context Protocol (MCP) stdio server for Antigravity, Claude, & Cursor
pp mcp

# Record a shipped commit and inspect the delivery ledger
pp deliver <commit_sha> --release v1.0.0 --channel github
pp deliveries
pp deliveries deliveries.jsonl
```

On a machine with no ledger yet, `pp pulse` shows a first-run card with a short list of next steps. It never fabricates revenue figures.

---

## Release-to-Revenue Telemetry: Hooking Up Your App

PayPal Pulse connects your local Git releases directly to real-time PayPal revenue, conversion velocity, and dispute risk.

### 1. Initialize Your Repository (`pp init`)
Inside your project directory (Python, Node.js, Go, Rust, or any Git worktree):
```bash
pp init
```
`pp init` automatically detects your Git branch, latest tag, and project metadata, creates `.pulse.json`, and outputs copy-paste checkout integration snippets.

### 2. Tag Your PayPal Checkout Orders
When creating an order via the PayPal Orders v2 API (`POST /v2/checkout/orders`), include your Git release version inside `purchase_units[0].custom_id`:

#### Python (Backend / FastAPI / Django / Flask):
```python
order_payload = {
    "intent": "CAPTURE",
    "purchase_units": [{
        "amount": {"currency_code": "USD", "value": "29.99"},
        "custom_id": f"release:{CURRENT_VERSION}",  # Attributes revenue to this release!
        "description": "Pro Developer License",
    }]
}
```

#### Node.js / TypeScript (Next.js / Express):
```typescript
const orderPayload = {
  intent: "CAPTURE",
  purchase_units: [{
    amount: { currency_code: "USD", value: "29.99" },
    custom_id: `release:${process.env.APP_VERSION || "v1.0.0"}`,
    description: "Pro Developer License"
  }]
};
```

#### cURL / Direct REST API:
```bash
curl -v -X POST https://api-m.sandbox.paypal.com/v2/checkout/orders \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -d '{"intent":"CAPTURE","purchase_units":[{"amount":{"currency_code":"USD","value":"29.99"},"custom_id":"release:v1.0.0"}]}'
```

### 3. Observe Release Conversions in Real Time
As orders and webhooks arrive, PayPal Pulse's Dual-Engine ledger automatically parses `custom_id`, creates release records dynamically, and computes take-home margin, PayPal fee drag, and conversion rate per Git tag:
```bash
# View release telemetry table
pp timeline

# Mint orders for a specific release
pp order 29.99 --release v1.3.0

# Simulate webhooks for a specific release
pp trigger payment.capture.completed --release v1.3.0 --amount 29.99
```

---

## Model Context Protocol (MCP) Server

PayPal Pulse exposes a native JSON-RPC Model Context Protocol (MCP) server over `stdio`, allowing Antigravity, Claude Desktop, Cursor, and other AI agents to autonomously query financial telemetry, monitor chargeback disputes, draft evidence responses, and mint Sandbox orders.

### Exposed MCP Tools

| Tool | Description | Key Parameters |
| :--- | :--- | :--- |
| `pulse_get_financial_summary` | Fetch 30-day revenue, fee drag, net margins, and Merchant Health Index. | `demo` (bool, opt) |
| `pulse_get_disputes` | List active PayPal buyer disputes with root-cause reason and due date. | `only_open` (bool, opt) |
| `pulse_draft_dispute_evidence` | Draft professional seller dispute evidence package using Pulse AI / Groq. | `dispute_id` (str, req) |
| `pulse_get_timeline` | Correlate Git releases with revenue spikes and dispute surges. | `since` (str, opt) |
| `pulse_create_order` | Mint a live PayPal Sandbox checkout order for software licenses. | `amount`, `currency` (req) |
| `pulse_create_invoice` | Draft a live PayPal Sandbox invoice to bill a client. | `recipient_email`, `amount`, `currency` (req) |
| `pulse_simulate_webhook` | Synthesize a PayPal webhook event into the ledger with release attribution. | `event_type`, `amount`, `currency`, `release_version`, `sku` (all opt) |

### Antigravity & Agent Configuration

Add the following to `~/.gemini/config/mcp_config.json` (or `.agents/mcp_config.json` in workspace root):

```json
{
  "mcpServers": {
    "paypal-pulse": {
      "command": "python",
      "args": ["-m", "pulse", "mcp"],
      "env": {
        "PYTHONPATH": "c:\\Users\\marcj\\PayPal_Pulse",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

---

## License

This project is open source and licensed under the [MIT License](LICENSE) (c) 2026 Marc Doria.


