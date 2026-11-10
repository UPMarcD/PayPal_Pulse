# PayPal Pulse (`pp`)

**Terminal-Native Financial Telemetry & Pulse AI Copilot for Indie Developers and Micro-Businesses**

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
   - Stores real PayPal Sandbox Order/Invoice/Transaction IDs in a local SQLite database (`.pulse_cache/pulse.db`) alongside smartwatch micro-business telemetry (`v1.1.0`, `v1.2.0`, `v1.2.1`, install counts, device models, multi-currency fee drag) so newly created sandbox orders appear instantaneously without waiting for the Reporting API's 3-hour indexing latency.
3. **Pulse AI Financial Copilot (`pulse/ai_copilot.py`)**
   - Claude-inspired interactive terminal financial copilot (`Pulse`) with live tool execution.
   - Answers natural language questions over live PayPal telemetry and executes live PayPal Sandbox actions (e.g., generating live checkout links or drafting dispute evidence).

---

## Quick Start

```bash
# 1. Install package in editable mode
pip install -e .

# 2. Configure credentials in .env (see .env.example)
# PAYPAL_CLIENT_ID=...
# PAYPAL_CLIENT_SECRET=...
# GROQ_API_KEY=...

# 3. Verify live PayPal Sandbox OAuth2 & Pulse AI connection
pp auth

# 4. Seed real Orders/Invoices on PayPal Sandbox + 30-day smartwatch telemetry
pp seed

# 5. View the rich terminal financial & smartwatch dashboard
pp pulse

# 6. Inspect Git release-to-revenue correlation timeline
pp timeline

# 7. View active disputes & resolve a dispute with Pulse AI + live Sandbox order
pp disputes
pp disputes --resolve PP-D-90411-DE

# 8. Ask Pulse AI Financial Copilot (or ask it to create a live order!)
pp ask "Why did German users dispute v1.2.1 and what is my true net take-home after fees?"
pp ask "Create a $9.99 USD PayPal order for ChronoPulse Pro"

# 9. Launch the full-screen interactive Textual TUI
pp tui
```
