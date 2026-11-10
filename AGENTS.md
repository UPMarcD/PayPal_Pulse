# AGENTS.md — PayPal Pulse (`pp`)

> **Operating Manual & System Context for AI Coding Agents, Contributors, and Autonomous Harnesses**  
> **Repository:** `MarcD25/PayPal_Pulse` (Public / Open-Source)  
> **Primary Author:** Marc Jacob Doria  
> **Hackathon Track:** [PayPal AI Hackathon (Devpost)](https://paypalaihackathon.devpost.com)  
> **License:** MIT

---

## 1. Project Identity & Hackathon Context

### What is PayPal Pulse?
**PayPal Pulse (`pp`)** is a terminal-native financial telemetry command deck and AI copilot built for indie developers, micro-SaaS founders, and software creators. It bridges the critical divide between rapid code deployments and fragmented payment dashboards.

### The Problem It Solves
1. **The Developer-Finance Disconnect:** Developers deploy software in seconds via Git and terminal workflows, but manage revenues in legacy, high-latency web portals requiring constant 2FA re-authentication and manual CSV reconciliation.
2. **Release-to-Revenue Blindness:** Developers release a new patch or pricing tier (e.g., `v1.2.1`) without real-time observability into whether that release triggered sales spikes, conversion drops, or chargeback waves.
3. **Dispute Panic & Friction:** Unanswered PayPal buyer disputes lead to account reserves and frozen funds. Pulse surfaces dispute surges immediately in the CLI/TUI and leverages AI to draft evidence packages for the PayPal Disputes API.

### PayPal AI Hackathon Alignment
- **Integration 1 (PayPal Developer Platform):** Live interaction with PayPal REST APIs on Sandbox (`https://api-m.sandbox.paypal.com`):
  - **Orders v2 API** (`POST /v2/checkout/orders`, `GET /v2/checkout/orders/{id}`)
  - **Customer Disputes API** (`GET /v1/customer/disputes`, `POST /v1/customer/disputes/{id}/provide-evidence`)
  - **Reporting & Balances APIs** (`GET /v1/reporting/balances`, `GET /v1/reporting/transactions`)
  - **Invoicing v2 API** (`POST /v2/invoicing/invoices`, `GET /v2/invoicing/invoices`)
- **Integration 2 (Artificial Intelligence):** 
  - **Pulse AI Copilot:** Local, private financial intelligence engine powered by Groq (`llama-3.3-70b-versatile`) with automatic Gemini / OpenAI fallback.
  - **Tool-Calling Architecture:** Conversational natural language queries trigger real actions: minting live sandbox orders, creating customer invoices, and compiling dispute evidence narratives from git logs.
- **Judging Criteria Target:**
  - **Technological Implementation:** Dual-Engine architecture pairing live PayPal Sandbox REST calls with a zero-latency SQLite local ledger to bypass the 3-hour reporting indexing delay.
  - **Design & Polish:** Zero-scroll full-screen Textual TUI (`Screen { overflow: hidden }`), strictly budgeted for standard 80x24 / 120x35 viewports using a Minimalist Slate palette (`#0d1117`, `#58a6ff`).
  - **Potential Impact:** Serves the \$26.5B global micro-SaaS and indie developer economy with zero bloated ERP dependencies.

---

## 2. System Architecture & Folder Layout

```
PayPal_Pulse/
├── AGENTS.md                  # Single Source of Truth for autonomous agents & contributors
├── LICENSE                    # MIT Open-Source License
├── README.md                  # Public developer documentation & quickstart
├── pyproject.toml             # Modern PEP 621 build configuration
├── requirements.txt           # Direct dependency lockfile
├── .env.example               # Template for PayPal & AI API keys
├── .gitignore                 # Strict secrecy enforcement (locks .env, .db, caches)
├── docs/                      # Research, pitch deck, and architecture specifications
│   ├── PAYPAL_API_RESEARCH.md            # Deep dive into PayPal REST APIs & SDKs
│   ├── PITCH_DECK_AND_MARKET_ANALYSIS.md # 7-slide deck, TAM/SAM/SOM, positioning
│   └── TUI_DESIGN_PATTERNS_RESEARCH.md   # Zero-scroll viewport budget & TUI design
├── pulse/                     # Core Python package
│   ├── __init__.py            # Version and package metadata (`0.1.0`)
│   ├── config.py              # Pydantic v2 settings loading from `.env`
│   ├── paypal_client.py       # Live PayPal Sandbox REST client (OAuth2, Orders, Disputes)
│   ├── store.py               # Dual-Engine SQLite ledger & CFO Health Index engine
│   ├── ai_copilot.py          # Pulse AI agent loop, prompt engine, and tool router
│   ├── cli.py                 # Typer-powered command-line interface (`pp`)
│   └── tui.py                 # Textual full-screen terminal command center (`pp tui`)
└── tests/                     # Automated test suite
    ├── __init__.py
    └── test_pulse.py          # 7 end-to-end integration, client, CLI, and headless pilot tests
```

---

## 3. Technology Stack & Key Libraries

| Component | Library / Framework | Version / Standard | Purpose |
| :--- | :--- | :--- | :--- |
| **Runtime** | Python | `>=3.10` (tested on 3.13) | Base execution environment |
| **CLI Framework** | `typer` + `rich` | `^0.9.0` / `^13.7.0` | Subcommand parsing, formatted tables, ASCII sparklines |
| **TUI Engine** | `textual` | `^0.47.0` | Asynchronous terminal dashboard with non-blocking workers |
| **HTTP Client** | `httpx` | `^0.26.0` | Async & sync HTTP requests to PayPal Sandbox |
| **Data Validation**| `pydantic` / `pydantic-settings` | `^2.6.0` | Type-safe environment variable parsing |
| **Database** | `sqlite3` (built-in standard) | SQLite 3 | Dual-Engine transaction ledger (`.pulse_cache/pulse.db`) |
| **AI Inference** | `groq`, `google-genai`, `openai` | Latest | Multi-provider LLM inference (default: Groq LLaMA 3.3 70B) |
| **Testing** | `pytest`, `pytest-asyncio` | Standard | Automated validation and headless TUI pilot testing |

---

## 4. Agent Operational Rules & Coding Directives

When modifying, extending, or maintaining this codebase, every agent must strictly obey these directives:

### 1. Zero-Scroll Viewport Discipline (`pulse/tui.py`)
- Standard terminal size baseline is **80 cols × 24 rows** (and comfort size **120 cols × 35 rows**).
- Tab 1 (`Dashboard`) must **never trigger a vertical scrollbar**.
- Keep `#dash-container` with `overflow: hidden`.
- Follow the strict vertical budget:
  - Header (1 row) + TabBar (3 rows) + GlobalBar (1 row) + KPI Deck (5 rows) + Sparkline (3 rows) + DataTable (`1fr`, min 5 rows) + Footer (1 row) $\approx$ 21 rows total.
- The active dispute warning (`#dash-dispute-alert`) is adaptive: collapse it (`display = False`) when `open_disputes == 0`.

### 2. Minimalist Slate Palette Restraint
- **Prohibited:** "Rainbow" UI badges with competing saturated primaries (green, cyan, magenta, yellow, red all at once).
- **Approved Palette:**
  - Canvas: `#0d1117` | Panels/Cards: `#161b22` | Borders: `#21262d`
  - Text Primary: `#f0f6fc` / `#c9d1d9` | Text Dim/Labels: `#8b949e`
  - Accent / Brand: Steel Blue `#58a6ff`
  - Semantic Profit / OK: Muted Green `#3fb950`
  - Semantic Dispute / Critical: Coral `#f85149` (strictly reserved for active disputes).

### 3. Non-Blocking Async Worker Pattern
- Textual UI methods that touch network APIs (`pp_client.create_order`, `pp_client.authenticate`, `ask_copilot`) **must always run on background threads** using `@work(thread=True, exclusive=True)` or `@work(exclusive=True)`.
- UI updates from workers must cross the thread boundary via `self.call_from_thread(...)`. Never execute synchronous network calls inside a UI event handler.

### 4. Dual-Engine Ledger Principle
- PayPal Reporting APIs have an inherent **up-to-3-hour indexing delay**. Newly created orders via `/v2/checkout/orders` will not appear in `/v1/reporting/transactions` immediately.
- The local SQLite engine (`pulse/store.py`) immediately records live created orders with `platform_sku` attribution to provide zero-second feedback in the dashboard.
- Always maintain backwards compatibility between `platform_sku` and legacy `device_model` column names.

### 5. ATS & STE Communication Standard
- All user-facing explanations, documentation, and commit messages should follow Simplified Technical English (STE): active voice, present tense, concise statements, zero marketing buzzword bloat.

---

## 5. Build, Test, and Execution Commands

```bash
# Install in editable mode with development dependencies
pip install -e .

# Run the complete test suite (includes CLI, DB, client, and headless Textual pilot)
pytest -v

# Launch the interactive terminal UI
pp tui

# Run CLI telemetry commands
pp pulse          # Financial & take-home breakdown
pp timeline       # Git release-to-revenue correlation
pp disputes       # Active buyer disputes & chargeback risk
pp auth           # Verify PayPal Sandbox & Pulse AI status
pp seed           # Seed local dual-engine ledger with realistic telemetry
pp ask "What is our net margin after PayPal fees?"
```

---

## 6. Git & Security Guardrails
- **Never commit credentials:** `.env` and `.pulse_cache/*.db` are strictly ignored by `.gitignore`. Check with `git status --ignored` before staging.
- **Repository Remote:** `https://github.com/MarcD25/PayPal_Pulse.git`
- **License Compliance:** Keep all distributed source files licensed under the root `LICENSE` (MIT).
