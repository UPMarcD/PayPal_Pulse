# PayPal AI Hackathon: Developer Documentation, APIs & Ecosystem Research

## 1. Executive Summary
This document synthesizes primary developer documentation, API specifications, modern SDK requirements, and AI agent ecosystem tools for the **PayPal AI Hackathon** (https://paypalaihackathon.devpost.com/resources) and the broader PayPal Developer Platform.

---

## 2. Core PayPal REST APIs Relevant to Pulse

### 2.1 Reporting / Transaction Search API
- **Endpoint:** `GET /v1/reporting/transactions`
- **Sandbox Base URL:** `https://api-m.sandbox.paypal.com`
- **Live Base URL:** `https://api-m.paypal.com`
- **Required OAuth Scope:** `https://uri.paypal.com/services/reporting/search/read`
- **Operational Characteristics:**
  - **Data Latency:** Up to 3 hours for freshly executed transactions to reflect.
  - **History Window:** Returns transaction history for up to the previous 3 years.
  - **Query Parameters:**
    - `start_date` / `end_date` (ISO-8601 UTC timestamps, required range).
    - `transaction_id`: Specific 17-character PayPal transaction ID or 19-character order ID.
    - `transaction_type`: Event code filter (e.g., T0000 series).
    - `transaction_status`: Filter by `S` (Success), `D` (Denied), `P` (Pending), `V` (Reversed).
    - `page` / `page_size`: Pagination parameters (up to 500 records per page).
    - `fields`: Field selection (e.g., `all`, `transaction_info`, `payer_info`, `shipping_info`).
  - **Payload Structure:** Returns `transaction_details` array containing `transaction_info` (`transaction_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_updated_date`, `transaction_amount`, `fee_amount`), `payer_info`, and `cart_info`.

### 2.2 Balances API
- **Endpoint:** `GET /v1/reporting/balances`
- **Sandbox Base URL:** `https://api-m.sandbox.paypal.com`
- **Required OAuth Scope:** `https://uri.paypal.com/services/reporting/balances/read`
- **Operational Characteristics:**
  - Retrieves ledger balances across all active merchant currencies.
  - Query parameters:
    - `as_of_time`: ISO-8601 datetime to retrieve historical balance snapshot.
    - `currency_code`: 3-letter ISO-4217 code (e.g., `USD`, `EUR`, `PHP`).
  - Response includes: `total_balance`, `available_balance`, `withheld_balance`, and currency breakdowns.

### 2.3 Subscriptions / Billing API
- **Base Endpoint:** `/v1/billing/subscriptions`
- **Key Routes:**
  - `POST /v1/billing/plans`: Create billing product plans (pricing tiers, billing intervals).
  - `POST /v1/billing/subscriptions`: Create recurring subscription agreement.
  - `GET /v1/billing/subscriptions/{id}`: Fetch subscription lifecycle details and status (`APPROVAL_PENDING`, `APPROVED`, `ACTIVE`, `SUSPENDED`, `CANCELLED`, `EXPIRED`).
  - `POST /v1/billing/subscriptions/{id}/cancel`: Reason-based cancellation.
  - `GET /v1/billing/subscriptions/{id}/transactions`: Historical billing executions for the subscription.
- **Role in Pulse:** Provides MRR (Monthly Recurring Revenue), churn signals, renewal projections, and subscription health diagnostics.

### 2.4 Disputes API
- **Base Endpoint:** `/v1/customer/disputes`
- **Required OAuth Scope:** `https://uri.paypal.com/services/disputes/read-buyer` / `update-seller`
- **Key Routes:**
  - `GET /v1/customer/disputes`: List merchant disputes with filters (`dispute_state`, `dispute_life_cycle_stage`).
  - `GET /v1/customer/disputes/{dispute_id}`: Retrieve dispute timeline, buyer claims, transaction ID, dispute amount, and due dates.
  - `POST /v1/customer/disputes/{dispute_id}/accept-claim`: Concede claim.
  - `POST /v1/customer/disputes/{dispute_id}/provide-evidence`: Programmatically submit evidence documents, tracking numbers, and narrative justification.
- **Role in Pulse:** Proactive dispute surge detection, automated evidence compilation, chargeback risk scoring.

### 2.5 Invoicing & Orders APIs
- **Invoicing API (`/v2/invoicing/invoices`):**
  - Replaces legacy `/v1/invoicing`.
  - Routes: `POST /v2/invoicing/invoices` (Draft), `POST /v2/invoicing/invoices/{id}/send` (Dispatch with payment link), `GET /v2/invoicing/invoices` (List/filter unpaid/overdue).
  - Supports automatic payment reminders, invoice QR codes, partial payments.
- **Orders API (`/v2/checkout/orders`):**
  - Modern v2 payments standard.
  - Routes: `POST /v2/checkout/orders` (Intent `CAPTURE` or `AUTHORIZE`), `GET /v2/checkout/orders/{id}`, `POST /v2/checkout/orders/{id}/capture`.
  - Returns instant breakdown of gross transaction amounts, buyer shipping addresses, payment instrument details.

---

## 3. Python SDK & Integration Standards (2026 Status)

### 3.1 Modern vs. Deprecated SDKs
- **Current Official SDK:** **paypal-server-sdk** (Package: `paypal-server-sdk`, GitHub: `paypal/PayPal-Python-Server-SDK`).
  - Actively maintained by PayPal for modern Python (3.8+ / 3.10+ / 3.12+).
  - Fully supports Orders v2, Payments v2, Subscriptions, Vault, and Webhooks.
  - Includes type hints, automated token refresh lifecycle, and retry handlers.
- **Deprecated SDKs (DO NOT USE):**
  - `paypalcheckoutsdk` (Legacy Checkout SDK - officially deprecated).
  - `paypalrestsdk` (Legacy REST SDK v1 - long deprecated).

### 3.2 Authentication Protocol
- **Endpoint:** `POST https://api-m.sandbox.paypal.com/v1/oauth2/token`
- **Auth Type:** HTTP Basic Authentication using `PAYPAL_CLIENT_ID` (username) and `PAYPAL_CLIENT_SECRET` (password).
- **Form Body:** `grant_type=client_credentials`
- **SDK Pattern (`paypal-server-sdk`):**
```python
import os
from paypalserversdk.configuration import Environment
from paypalserversdk.http.auth.o_auth_2 import ClientCredentialsAuthCredentials
from paypalserversdk.paypal_serversdk_client import PaypalServersdkClient

client = PaypalServersdkClient(
    environment=Environment.SANDBOX,
    client_credentials_auth_credentials=ClientCredentialsAuthCredentials(
        o_auth_client_id=os.getenv('PAYPAL_CLIENT_ID'),
        o_auth_client_secret=os.getenv('PAYPAL_CLIENT_SECRET')
    )
)
```
- **Direct HTTP Pattern (for Reporting & Disputes APIs not in server SDK):**
  Because `paypal-server-sdk` primarily wraps core Checkout/Orders/Payments/Vault, direct REST calls via `httpx` or `requests` against `/v1/reporting/transactions` and `/v1/customer/disputes` with a cached Bearer token are standard practice and fully supported.

---

## 4. PayPal Agent Toolkit & AI Ecosystem

### 4.1 `paypal/agent-toolkit` & `paypal/AI-Toolkit`
- **GitHub Repositories:**
  - `https://github.com/paypal/agent-toolkit` (Core toolkit with TypeScript & Python bindings)
  - `https://github.com/paypal/AI-Toolkit` (Coding assistant plugins for Claude Code, Cursor)
- **PyPI Distribution:** `paypal-agent-toolkit` (pip package) and `@paypal/agent-toolkit` (npm package).
- **Model Context Protocol (MCP):**
  - PayPal provides an official MCP Server interface enabling AI IDEs and agents (Claude Code, Cursor, OpenAI Agents, LangChain, CrewAI) to invoke PayPal operations natively as agent tools.
- **Toolkit Tools Included:**
  - **Invoicing:** `create_invoice`, `list_invoices`, `gEt_invoice`, `send_invoice`, `send_invoice_reminder`, `cancel_sent_invoice`, `generate_invoice_qr_code`, `delete_invoice`, `generate_invoice_number`, `search_invoicing`, `update_invoicing`, `cancel_invoice_auto_reminder`, `record_payment_for_invoice`, `record_refund_for_invoice`, `create_conditional_rules_for_invoice`, `create_recurring_series`, `activate_recurring_series`, `get_recurring_series`, `cancel_recurring_series`, `delete_recurring_series`.
  - **Payments:** `create_order`, `get_order`, `pay_order`, `create_refund`, `get_refund`.
  - **Dispute Management:** `list_disputes`, `get_dispute`, `accept_dispute_claim`.
  - **Shipment Tracking:** `create_shipment_tracking`, `get_shipment_tracking`, `update_shipment_tracking`.
  - **Catalog Management:** `create_product`, `list_products`, `show_product_details`.
  - **Subscription Management:** `create_subscription_plan`, `list_subscription_plans`, `show_subscription_plan_details`, `create_subscription`, `show_subscription_details`, `update_subscription`, `cancel_subscription`.
  - **Reporting & Insights:** `list_transactions`, `get_merchant_insights`.

### 4.2 Partner Resources & Credits (PayPal AI Hackathon Devpost)
The hackathon partner lineup provides dedicated resources and credits accessible via the participant dashboard (https://paypalaihackathon.devpost.com/resources):
1. **APIMatic:** API context engine, OpenAPI specification validation, and automated client generation tools for AI agents.
2. **Render:** Cloud application and background worker hosting platform with free compute/hosting tiers for fast zero-devops agent deployment.
3. **Postman:** Pre-built PayPal REST API collection workspaces and automated test runners.
4. **Elastic:** Search AI platform providing Elasticsearch vector search, hybrid retrieval, and semantic memory layers for financial querying.
5. **Additional Partners:** AG Grid (data grids for financial tables), Zapier (workflow automations), Astropods, Bryntum, Channel3, KERNEL.

---

## 5. Architectural Implications for PayPal Pulse

1. **Dual Access Strategy:**
  - Use `paypal-agent-toolkit` / MCP server for conversational actions (drafting invoices, resolving disputes, initiating refunds).
  - Use direct REST client (`httpx`) with OAuth2 client credentials against `/v1/reporting/transactions`, `/v1/reporting/balances`, and `/v1/customer/disputes` for high-throughput batch analytics and real-time dashboard telemetry.
2. **Sandbox Provisioning:**
  - Target `https://api-m.sandbox.paypal.com` with seeded sandbox merchant accounts containing mock transactions, disputes, and subscription agreements.
3. **Agent Integration:**
  - Integrate LangChain/CrewAI tools powered by `paypal-agent-toolkit` to allow natural language financial questions: *"What is my net revenue after refund deductions this week/"*, *"Which disputes require evidence within the next 48 hours?"*.
