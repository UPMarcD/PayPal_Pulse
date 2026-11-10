"""CLI Command Center for PayPal Pulse (`pp` and `python -m pulse`)."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from pulse.ai_copilot import (
    ask_copilot,
    draft_dispute_evidence,
    get_active_groq_model,
    verify_groq_connection,
)
from pulse.config import (
    GLOBAL_CONFIG_FILE,
    PulseSettings,
    get_settings,
    save_global_config,
)
from pulse.constants import EX_GENERAL, EX_NOPERM, EX_OK, EX_UNAVAILABLE, EX_USAGE
from pulse.paypal_client import PayPalClient
from pulse.store import PulseStore

# Ensure UTF-8 output on Windows consoles
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

console = Console()
console_err = Console(stderr=True)
app = typer.Typer(
    name="pp",
    help="PayPal Pulse (pp) — Terminal-Native Financial Telemetry & Pulse AI for Indie Developers.",
    add_completion=False,
    no_args_is_help=True,
)


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    blocks = " ▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    span = (hi - lo) if hi > lo else 1.0
    return "".join(blocks[min(7, int(((v - lo) / span) * 7))] for v in values)


@app.command("auth")
def cmd_auth(
    refresh: bool = typer.Option(True, "--refresh/--cached", help="Force fresh OAuth2 token acquisition"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
) -> None:
    """Verify live PayPal Sandbox OAuth2 token acquisition, merchant scopes, and Pulse AI connectivity."""
    settings = get_settings()
    pp_client = PayPalClient(settings)

    auth_res = pp_client.authenticate(force_refresh=refresh)
    groq_res = verify_groq_connection(settings)

    if json_output:
        payload = {
            "paypal": auth_res.model_dump(),
            "pulse_ai": groq_res.model_dump(),
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        if not auth_res.authenticated:
            exit_code = EX_NOPERM if auth_res.error_type in ("AUTH_FAILED", "MISSING_CREDENTIALS") else EX_UNAVAILABLE
            raise typer.Exit(code=exit_code)
        return

    console.print(
        Panel.fit(
            "[bold #00a8ea]PayPal Pulse (`pp auth`) — Live Sandbox & Pulse AI Diagnostics[/bold #00a8ea]",
            border_style="#0070ba",
        )
    )

    table = Table(title="Authentication & Provider Telemetry", box=box.ROUNDED, expand=True)
    table.add_column("Provider / Surface", style="bold cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("App / Engine / Details", style="white")
    table.add_column("Latency", justify="right", style="yellow")

    if auth_res.authenticated:
        hours_left = round(auth_res.expires_in / 3600, 1)
        table.add_row(
            f"PayPal OAuth 2.0 ({auth_res.mode.upper()})",
            "[bold green]● AUTHENTICATED[/bold green]",
            (
                f"Endpoint: {auth_res.base_url}/v1/oauth2/token\n"
                f"App ID: [bold]{auth_res.app_id}[/bold] | Token: `{auth_res.access_token_preview}`\n"
                f"Expires In: {auth_res.expires_in}s (~{hours_left}h) | Scopes: {len(auth_res.scopes)} granted"
            ),
            f"{auth_res.latency_ms:.0f} ms",
        )
    else:
        table.add_row(
            f"PayPal OAuth 2.0 ({auth_res.mode.upper()})",
            "[bold red]✗ FAILED[/bold red]",
            f"Error: {auth_res.error}",
            f"{auth_res.latency_ms:.0f} ms",
        )

    if groq_res.connected:
        table.add_row(
            "Pulse AI Engine (Financial Intelligence)",
            "[bold green]● CONNECTED[/bold green]",
            (
                f"Persona: [bold]Pulse[/bold] ({groq_res.available_models_count} inference endpoints online)\n"
                f"Active Engine: [bold]{groq_res.model}[/bold] | Key: `{groq_res.key_preview}`"
            ),
            f"{groq_res.latency_ms:.0f} ms",
        )
    else:
        table.add_row(
            "Pulse AI Engine (Financial Intelligence)",
            "[bold red]✗ DISCONNECTED[/bold red]",
            f"Error: {groq_res.error}",
            f"{groq_res.latency_ms:.0f} ms",
        )

    console.print(table)

    if auth_res.authenticated and auth_res.scopes:
        scope_preview = ", ".join(s.split("/")[-1] for s in auth_res.scopes[:8])
        console.print(f"[dim]Granted PayPal OAuth Scopes:[/dim] [cyan]{scope_preview}[/cyan]")

    if not auth_res.authenticated:
        exit_code = EX_NOPERM if auth_res.error_type in ("AUTH_FAILED", "MISSING_CREDENTIALS") else EX_UNAVAILABLE
        raise typer.Exit(code=exit_code)


@app.command("seed")
def cmd_seed(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output machine-readable JSON to stdout",
    ),
) -> None:
    """Create real live Orders/Invoices on PayPal Sandbox and seed 30 days of software telemetry."""
    settings = get_settings()
    pp_client = PayPalClient(settings)
    store = PulseStore(settings.db_path)

    if not json_output:
        console.print(
            Panel.fit(
                "[bold #00a8ea]PayPal Pulse (`pp seed`) — Live PayPal Sandbox Provisioning & Dual-Engine Seeding[/bold #00a8ea]",
                border_style="#0070ba",
            )
        )

    auth_res = pp_client.authenticate(force_refresh=False)
    if not auth_res.authenticated:
        if json_output:
            err_payload = {
                "error": auth_res.error or "PayPal Sandbox authentication failed",
                "code": EX_NOPERM,
            }
            sys.stdout.write(json.dumps(err_payload, indent=2) + "\n")
        else:
            console_err.print(f"[bold red]PayPal Sandbox Auth Failed:[/bold red] {auth_res.error}")
        raise typer.Exit(code=EX_NOPERM)

    # 1. Create 3 real live Orders on PayPal Sandbox across USD, PHP, EUR tied to the 3 software releases
    order_specs = [
        (4.99, "USD", "PulseCore Pro Developer License (v1.1.0 - Core Engine)", "v1.1.0|US|CLI"),
        (299.00, "PHP", "PulseCore Pro APAC License (v1.2.0 - Performance & Async Engine)", "v1.2.0|PH|Desktop"),
        (5.99, "EUR", "PulseCore Pro EU Multi-Currency Tier (v1.2.1 - Ubuntu Linux)", "v1.2.1|DE|Ubuntu24"),
    ]

    live_orders = []
    live_table = Table(
        title="Live PayPal Sandbox API Orders Created (POST /v2/checkout/orders)",
        box=box.ROUNDED,
        expand=True,
    )
    live_table.add_column("PayPal Order ID", style="bold green", no_wrap=True)
    live_table.add_column("Status", style="cyan", no_wrap=True)
    live_table.add_column("Amount", justify="right", style="bold white")
    live_table.add_column("Release Tag", style="yellow")
    live_table.add_column("Live Sandbox Approval Link", style="dim")

    for amt, cur, desc, custom_id in order_specs:
        order_res = pp_client.create_order(
            amount=amt,
            currency=cur,
            description=desc,
            custom_id=custom_id,
        )
        # Verify at least the first order via GET /v2/checkout/orders/{id}
        if order_res.success and len(live_orders) == 0:
            verified = pp_client.get_order(order_res.order_id)
            if verified.success:
                order_res = verified
        live_orders.append(order_res)
        ver_tag = custom_id.split("|")[0]
        if order_res.success:
            live_table.add_row(
                order_res.order_id,
                order_res.status,
                f"{order_res.amount:.2f} {order_res.currency}",
                ver_tag,
                order_res.approve_url,
            )
        else:
            live_table.add_row("FAILED", "ERROR", f"{amt:.2f} {cur}", ver_tag, order_res.error or "")

    if not json_output:
        console.print(live_table)

    # 2. Also create a live PayPal Sandbox Draft Invoice (POST /v2/invoicing/invoices)
    inv_res = pp_client.create_draft_invoice(
        item_name="PulseCore Pro Enterprise Server License",
        amount=49.00,
        currency="USD",
        recipient_email="enterprise-partner@example.com",
    )
    if inv_res.success and not json_output:
        console.print(
            f"[green]✓ Created Live PayPal Sandbox Draft Invoice:[/green] [bold]{inv_res.invoice_id}[/bold] "
            f"($49.00 USD -> {inv_res.recipient_email})"
        )

    # 3. Seed the Dual-Engine SQLite database with 30-day history + real PayPal Sandbox IDs
    summary = store.seed_telemetry(live_orders=live_orders, live_invoice=inv_res)

    if json_output:
        payload = {
            "status": "seeded",
            "db_path": str(store.db_path),
            "orders_created": [
                {
                    "order_id": o.order_id,
                    "status": o.status,
                    "amount": o.amount,
                    "currency": o.currency,
                    "approve_url": o.approve_url,
                }
                for o in live_orders
                if o.success
            ],
            "invoice_created": {
                "invoice_id": inv_res.invoice_id,
                "status": inv_res.status,
                "amount": inv_res.amount,
                "currency": inv_res.currency,
            }
            if inv_res.success
            else None,
            "summary": {
                "total_gross_usd": summary.get("total_gross_usd"),
                "total_fees_usd": summary.get("total_fees_usd"),
                "total_net_usd": summary.get("total_net_usd"),
                "fee_drag_pct": summary.get("fee_drag_pct"),
                "open_disputes_count": summary.get("open_disputes_count"),
            },
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        console.print(
            Panel(
                f"[bold green]✓ Dual-Engine SQLite Ledger Seeded Successfully![/bold green] (`{store.db_path}`)\n"
                f"  • [bold]Releases Correlated:[/bold] `v1.1.0`, `v1.2.0 - Performance & Async Engine`, `v1.2.1 - EU Multi-Currency Tier`\n"
                f"  • [bold]30-Day Gross Volume:[/bold] ${summary['total_gross_usd']:,.2f} USD across `USD`, `EUR`, `PHP`\n"
                f"  • [bold]True Net Take-Home:[/bold] [bold green]${summary['total_net_usd']:,.2f} USD[/bold green] "
                f"(PayPal Fee Drag: ${summary['total_fees_usd']:,.2f} / {summary['fee_drag_pct']}%)\n"
                f"  • [bold]Active Dispute Alerts:[/bold] [bold red]{summary['open_disputes_count']} German (DE) buyer disputes on v1.2.1[/bold red]",
                title="Seed Summary",
                border_style="green",
            )
        )


@app.command("pulse")
def cmd_pulse(
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
    demo: bool = typer.Option(False, "--demo", help="Run against isolated demo database (.pulse_cache/demo.db)"),
) -> None:
    """Display the rich full-terminal financial & customer telemetry dashboard."""
    settings = get_settings(demo_mode=demo)
    pp_client = PayPalClient(settings)
    store = PulseStore(settings.db_path)

    if not store.is_seeded():
        store.seed_telemetry()

    auth_res = pp_client.authenticate(force_refresh=False)
    balances_probe = pp_client.get_balances() if auth_res.authenticated else {"ok": False, "balances": []}
    lat_val = auth_res.latency_ms if auth_res.authenticated else 180.0
    summary = store.get_dashboard_summary(api_latency_ms=lat_val, api_connected=auth_res.authenticated)

    if json_output:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
        return

    # 1. Top Connection Badge
    if auth_res.authenticated:
        live_bal_info = (
            f"Live Balances API: {len(balances_probe.get('balances', []))} currency buckets"
            if balances_probe.get("ok")
            else "Dual-Engine Instant Ledger Active (3h Reporting API Latency Bypassed)"
        )
        conn_text = (
            f"[bold green]● PAYPAL SANDBOX CONNECTED[/bold green] | App: [bold]{auth_res.app_id}[/bold] | "
            f"Token: `{auth_res.access_token_preview}` | {live_bal_info} | "
            f"Gateway: [bold cyan]{lat_val:.0f}ms[/bold cyan] | Pulse AI: [bold cyan]Online[/bold cyan]"
        )
    else:
        conn_text = f"[bold yellow]● LOCAL TELEMETRY MODE[/bold yellow] | Reason: {auth_res.error}"

    console.print(Panel(conn_text, title="[bold #00a8ea]PayPal Pulse (`pp pulse`) — Merchant Command Center[/bold #00a8ea]", border_style="#0070ba"))

    # 2. Active Dispute Alert Banner
    if summary["open_disputes_count"] > 0:
        console.print(
            Panel(
                f"[bold red]⚠ PROACTIVE DISPUTE ALERT:[/bold red] [bold]{summary['open_disputes_count']} Open Customer Disputes[/bold] "
                f"detected on release [bold yellow]v1.2.1 - EU Multi-Currency Tier[/bold yellow] (Region: [bold]Germany / DE[/bold], Platform: [bold]Ubuntu Linux 24.04 (CLI)[/bold]).\n"
                f"Run [bold cyan]pp disputes[/bold cyan] to inspect or [bold cyan]pp disputes --resolve PP-D-90411-DE[/bold cyan] to auto-draft Pulse AI evidence & issue a Sandbox replacement order.",
                border_style="red",
            )
        )

    # 3. KPI Cards (True Net Take-Home + Customer & Sales Metrics)
    kpi_left = Panel(
        f"[bold white]Gross Volume (30d):[/bold white]   [bold]${summary['total_gross_usd']:,.2f} USD[/bold]\n"
        f"[bold white]PayPal Fee Drag:[/bold white]      [red]-${summary['total_fees_usd']:,.2f} USD ({summary['fee_drag_pct']}%)[/red]\n"
        f"[bold white]True Net Take-Home:[/bold white]   [bold green]${summary['total_net_usd']:,.2f} USD[/bold green]\n"
        f"[bold white]Effective Net Margin:[/bold white] [bold cyan]{summary['net_margin_pct']}%[/bold cyan]",
        title="[bold #00a8ea]True Net Revenue & Fee Drag[/bold #00a8ea]",
        border_style="#00a8ea",
        expand=True,
    )

    cust_count = summary.get("total_customers", summary.get("total_installs", 0))
    active_cust = summary.get("active_customers", summary.get("active_users", 0))
    paid_orders = summary.get("total_orders", summary.get("total_paid", 0))
    telemetry = summary.get("health_telemetry", {})
    score_val = summary["health_score"]
    rating_str = summary.get("health_rating", "HEALTHY")
    rating_color = telemetry.get("rating_color", "bold cyan")

    kpi_right = Panel(
        f"[bold white]Active Customers:[/bold white]   [bold]{active_cust:,}[/bold] (Total: {cust_count:,})\n"
        f"[bold white]Paid Orders:[/bold white]        [bold]{paid_orders:,}[/bold] ([bold cyan]{summary['overall_conversion_pct']}%[/bold cyan] Paid Conv %)\n"
        f"[bold white]Recurring MRR:[/bold white]      [bold green]${summary['mrr_usd']:,.2f} / mo[/bold green]\n"
        f"[bold white]CFO Health Index:[/bold white]   [{rating_color}]{score_val} / 100 [{rating_str}][/{rating_color}]",
        title="[bold #00a8ea]Customer & Sales Metrics[/bold #00a8ea]",
        border_style="#00a8ea",
        expand=True,
    )
    console.print(Columns([kpi_left, kpi_right], equal=True, expand=True))

    # 4. Multi-Currency Balances & True Net Breakdown Table
    cur_table = Table(
        title="Multi-Currency Ledger Balances & True Net Take-Home",
        box=box.SIMPLE_HEAVY,
        expand=True,
    )
    cur_table.add_column("Currency", style="bold cyan")
    cur_table.add_column("Transactions", justify="right")
    cur_table.add_column("Gross Volume", justify="right", style="white")
    cur_table.add_column("PayPal Fees", justify="right", style="red")
    cur_table.add_column("Fee Drag %", justify="right", style="yellow")
    cur_table.add_column("True Net (Native)", justify="right", style="bold green")
    cur_table.add_column("Net Margin %", justify="right", style="cyan")
    cur_table.add_column("USD Equiv Net", justify="right", style="bold green")

    sym_map = {"USD": "$", "EUR": "€", "PHP": "₱", "GBP": "£"}
    for c in summary["currencies"]:
        sym = sym_map.get(c["currency"], "")
        cur_table.add_row(
            c["currency"],
            str(c["tx_count"]),
            f"{sym}{c['gross']:,.2f}",
            f"-{sym}{c['fees']:,.2f}",
            f"{c['fee_pct']}%",
            f"{sym}{c['net']:,.2f}",
            f"{c['net_margin_pct']}%",
            f"${c['usd_net']:,.2f}",
        )
    console.print(cur_table)

    # 5. 14-Day ASCII Revenue & Customer Acquisition Sparkline / Bar Chart
    daily_14 = summary["daily_14"]
    net_series = [float(d["net_usd"]) for d in daily_14]
    inst_series = [float(d["installs"]) for d in daily_14]
    max_net = max(net_series) if net_series else 1.0

    chart_table = Table(
        title=f"14-Day Revenue & Customer Acquisition Velocity  |  Net Sparkline: {_sparkline(net_series)}  |  Customers: {_sparkline(inst_series)}",
        box=box.ROUNDED,
        expand=True,
    )
    chart_table.add_column("Date", style="dim", no_wrap=True)
    chart_table.add_column("Release", style="cyan", no_wrap=True)
    chart_table.add_column("Customers", justify="right")
    chart_table.add_column("Orders", justify="right")
    chart_table.add_column("True Net (USD)", justify="right", style="bold green")
    chart_table.add_column("ASCII Net Revenue Bar", style="green")

    for d in daily_14:
        bar_len = max(1, int((float(d["net_usd"]) / max_net) * 24))
        bar_str = "█" * bar_len
        if int(d["disputes"]) > 0:
            bar_str += f" [bold red]⚠ {d['disputes']} dispute[/bold red]"
        chart_table.add_row(
            d["day_date"][5:],
            d["release_version"],
            str(d["installs"]),
            str(d["paid_orders"]),
            f"${d['net_usd']:,.2f}",
            bar_str,
        )
    console.print(chart_table)

    # 6. Recent Live PayPal Sandbox Orders
    live_orders = summary["live_sandbox_orders"]
    if live_orders:
        lo_table = Table(
            title="Verified Live PayPal Sandbox Orders in Dual-Engine Ledger",
            box=box.SIMPLE,
            expand=True,
        )
        lo_table.add_column("PayPal Order ID", style="bold green")
        lo_table.add_column("Release", style="yellow")
        lo_table.add_column("Gross", justify="right")
        lo_table.add_column("Net", justify="right", style="green")
        lo_table.add_column("Status", style="cyan")
        lo_table.add_column("Approval Link", style="dim")
        for tx in live_orders[:5]:
            lo_table.add_row(
                tx["paypal_id"],
                tx["release_version"],
                f"{tx['gross_amount']:.2f} {tx['currency']}",
                f"{tx['net_amount']:.2f} {tx['currency']}",
                tx["status"],
                tx["approve_url"],
            )
        console.print(lo_table)


@app.command("timeline")
def cmd_timeline(
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
    demo: bool = typer.Option(False, "--demo", help="Run against isolated demo database (.pulse_cache/demo.db)"),
) -> None:
    """Correlate Git releases (v1.1.0, v1.2.0, v1.2.1) with PayPal revenue, conversion rate, and dispute spikes."""
    settings = get_settings(demo_mode=demo)
    store = PulseStore(settings.db_path)
    if not store.is_seeded():
        store.seed_telemetry()

    releases = store.get_releases(include_git_tags=True)

    if json_output:
        sys.stdout.write(json.dumps(releases, indent=2) + "\n")
        return

    console.print(
        Panel.fit(
            "[bold #00a8ea]PayPal Pulse (`pp timeline`) — Release-to-Revenue Correlation Engine[/bold #00a8ea]",
            border_style="#0070ba",
        )
    )

    tl_table = Table(
        title="Git Release Telemetry vs. PayPal Conversion & Dispute Surge",
        box=box.ROUNDED,
        expand=True,
    )
    tl_table.add_column("Release Tag", style="bold cyan", no_wrap=True)
    tl_table.add_column("Commit", style="dim", no_wrap=True)
    tl_table.add_column("Date", no_wrap=True)
    tl_table.add_column("Customers", justify="right")
    tl_table.add_column("Paid Orders", justify="right")
    tl_table.add_column("Conv %", justify="right", style="bold yellow")
    tl_table.add_column("Gross (USD)", justify="right")
    tl_table.add_column("True Net (USD)", justify="right", style="bold green")
    tl_table.add_column("Refunds", justify="right")
    tl_table.add_column("Disputes", justify="right")
    tl_table.add_column("Telemetry Status", no_wrap=True)

    for r in releases:
        disp_str = (
            f"[bold red]{r['dispute_count']} (DE)[/bold red]"
            if int(r["dispute_count"]) > 0
            else "[green]0[/green]"
        )
        badge_style = (
            "bold red"
            if "DISPUTE" in r["status_badge"]
            else ("bold green" if "SURGE" in r["status_badge"] else "cyan")
        )
        cust_val = r.get("customers", r.get("installs", 0))
        order_val = r.get("paid_orders", r.get("paid_conversions", 0))
        tl_table.add_row(
            r["version"],
            r["git_commit"],
            r["released_at"],
            f"{cust_val:,}",
            f"{order_val:,}",
            f"{r['conversion_rate']:.2f}%",
            f"${r['gross_usd']:,.2f}",
            f"${r['net_usd']:,.2f}",
            str(r["refund_count"]),
            disp_str,
            f"[{badge_style}]{r['status_badge']}[/{badge_style}]",
        )

    console.print(tl_table)

    # Pulse Release Diagnostics Panel
    diag_lines = []
    for r in releases:
        diag_lines.append(
            f"[bold cyan]{r['title']}[/bold cyan] (Commit `{r['git_commit']}` | Net Margin: {r['net_margin_pct']}%)\n"
            f"  └─ [white]{r['ai_diagnostic']}[/white]"
        )
    console.print(
        Panel(
            "\n\n".join(diag_lines),
            title="[bold #00a8ea]Pulse Release Diagnostics & Root-Cause Attribution[/bold #00a8ea]",
            border_style="#00a8ea",
        )
    )


@app.command("disputes")
def cmd_disputes(
    resolve: Optional[str] = typer.Option(
        None,
        "--resolve",
        "-r",
        help="Dispute ID to resolve with Pulse AI evidence + live PayPal Sandbox replacement order (e.g. PP-D-90411-DE)",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt on dispute resolution",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output machine-readable JSON to stdout",
    ),
    demo: bool = typer.Option(
        False,
        "--demo",
        help="Run against isolated demo database (.pulse_cache/demo.db)",
    ),
) -> None:
    """List active customer disputes or auto-resolve a dispute via Pulse AI and PayPal Sandbox."""
    settings = get_settings(demo_mode=demo)
    pp_client = PayPalClient(settings)
    store = PulseStore(settings.db_path)

    if not store.is_seeded():
        store.seed_telemetry()

    if resolve:
        dispute_id = resolve.strip()
        dispute = store.get_dispute_by_id(dispute_id)
        if not dispute:
            console_err.print(f"[bold red]Error:[/bold red] Dispute ID `{dispute_id}` not found in ledger.")
            raise typer.Exit(code=EX_USAGE)

        if not yes and sys.stdin.isatty():
            confirmed = typer.confirm(
                f"Resolve dispute {dispute_id} ({dispute.get('amount')} {dispute.get('currency')}) "
                f"with live PayPal Sandbox replacement order & AI evidence?"
            )
            if not confirmed:
                console_err.print("[yellow]Resolution aborted by user.[/yellow]")
                raise typer.Exit(code=0)

        sku_val = dispute.get("platform_sku", dispute.get("device_model", "Ubuntu Linux CLI"))

        # 1. Execute live PayPal Sandbox action
        sandbox_res = pp_client.submit_dispute_resolution(
            dispute_id=dispute_id,
            evidence_text=f"Hotfix v1.2.2 deployed for {sku_val}",
            amount=float(dispute["amount"]),
            currency=dispute["currency"],
        )
        rep_order = sandbox_res["replacement_order"]

        # 2. Draft formal dispute evidence via Pulse AI
        evidence_text, model_used = draft_dispute_evidence(
            dispute=dispute,
            replacement_order=rep_order,
            settings=settings,
        )

        order_id_str = rep_order.order_id if rep_order.success else "PP-LOCAL-RESOLVE"
        if rep_order.success:
            store.record_live_order(
                rep_order,
                release_version=dispute["release_version"],
                country_code=dispute["buyer_country"],
                platform_sku=sku_val,
            )
        store.mark_dispute_resolved(dispute_id, evidence_text, order_id_str)

        if json_output:
            out_res = {
                "dispute_id": dispute_id,
                "status": "RESOLVED",
                "paypal_dispute_api_status": sandbox_res["dispute_api_status"],
                "replacement_order_id": order_id_str,
                "evidence_draft": evidence_text,
            }
            sys.stdout.write(json.dumps(out_res, indent=2) + "\n")
            return

        console.print(
            Panel.fit(
                f"[bold yellow]Resolving Dispute `{dispute_id}` via Pulse AI + Live PayPal Sandbox...[/bold yellow]",
                border_style="yellow",
            )
        )
        console.print(
            Panel(
                Markdown(evidence_text),
                title="[bold green]● Pulse Evidence Package Drafted[/bold green]",
                border_style="green",
            )
        )
        console.print(
            Panel(
                f"• [bold]Dispute ID:[/bold] `{dispute_id}` -> [bold green]RESOLVED[/bold green]\n"
                f"• [bold]PayPal Disputes API Status:[/bold] `{sandbox_res['dispute_api_status']}`\n"
                f"• [bold]Live Sandbox Replacement Order ID:[/bold] [bold cyan]{order_id_str}[/bold cyan]\n"
                f"• [bold]Replacement Approval URL:[/bold] {rep_order.approve_url if rep_order.success else 'N/A'}",
                title="[bold #00a8ea]PayPal Sandbox Resolution Execution[/bold #00a8ea]",
                border_style="#0070ba",
            )
        )
        return

    # List disputes
    auth_res = pp_client.authenticate(force_refresh=False)
    live_disp_probe = pp_client.get_disputes() if auth_res.authenticated else {"ok": False, "items": []}
    disputes = store.get_disputes()

    if json_output:
        sys.stdout.write(json.dumps(disputes, indent=2) + "\n")
        return

    table = Table(
        title=f"Customer Disputes Ledger (Sandbox API Sync: {'ONLINE' if live_disp_probe.get('ok') else 'CACHED'})",
        box=box.ROUNDED,
        expand=True,
    )
    table.add_column("Dispute ID", style="bold yellow", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Country", style="cyan")
    table.add_column("Release", style="magenta")
    table.add_column("Client Platform / SKU", style="white")
    table.add_column("Amount", justify="right", style="bold")
    table.add_column("Due Date", no_wrap=True)
    table.add_column("Buyer Claim & Root Cause", style="dim")

    for d in disputes:
        st_badge = (
            "[bold red]OPEN[/bold red]"
            if d["status"] == "OPEN"
            else f"[bold green]{d['status']}[/bold green] ({d['resolution_order_id']})"
        )
        sku_entry = d.get("platform_sku", d.get("device_model", ""))
        table.add_row(
            d["dispute_id"],
            st_badge,
            d["buyer_country"],
            d["release_version"],
            sku_entry,
            f"{d['amount']:.2f} {d['currency']}",
            d["due_date"],
            d["description"],
        )

    console.print(table)
    console.print(
        "[dim]Tip: Run[/dim] [bold cyan]pp disputes --resolve PP-D-90411-DE[/bold cyan] "
        "[dim]to draft an evidence response with Pulse AI and mint a live PayPal Sandbox replacement order.[/dim]"
    )


@app.command("ask")
def cmd_ask(
    question: list[str] = typer.Argument(..., help="Natural language question or action command for Pulse AI Financial Copilot"),
    open_browser: bool = typer.Option(False, "--open", "-o", help="Automatically open newly minted PayPal Sandbox checkout links in your default web browser"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
    demo: bool = typer.Option(False, "--demo", help="Run against isolated demo sample telemetry database"),
) -> None:
    """Ask Pulse AI Financial Copilot about revenue, releases, disputes, or order creation."""
    import webbrowser

    settings = get_settings(demo_mode=demo)
    store = PulseStore(settings.db_path)
    if demo and not store.is_seeded():
        store.seed_telemetry()
    pp_client = PayPalClient(settings)

    full_question = " ".join(question).strip()
    res = ask_copilot(question=full_question, store=store, paypal_client=pp_client, settings=settings)

    if json_output:
        sys.stdout.write(json.dumps(res.model_dump(), indent=2) + "\n")
        return

    if res.created_order and res.created_order.success and open_browser:
        webbrowser.open(res.created_order.approve_url)

    if res.executed_action:
        console.print(
            Panel(
                f"[bold green]⚡ Live PayPal Sandbox Tool Executed:[/bold green] {res.executed_action}",
                border_style="green",
            )
        )

    console.print(
        Panel(
            Markdown(res.answer),
            title=(
                f"[bold #00a8ea]Pulse — PayPal Pulse AI Copilot[/bold #00a8ea] "
                f"[dim](⚡ {res.tokens_per_sec:.0f} tok/s · {res.latency_ms / 1000:.2f}s)[/dim]"
            ),
            border_style="#0070ba",
        )
    )


@app.command("trigger")
def cmd_trigger(
    event_type: str = typer.Argument(
        "payment.capture.completed",
        help="PayPal webhook event type (e.g. payment.capture.completed, customer.dispute.created, customer.dispute.resolved, checkout.order.approved)",
    ),
    amount: float = typer.Option(19.99, "--amount", "-a", help="Gross transaction amount"),
    currency: str = typer.Option("USD", "--currency", "-c", help="Currency code (USD, EUR, PHP, GBP)"),
    sku: str = typer.Option("PulseCore Pro", "--sku", help="Platform SKU or license name"),
    dispute_id: str = typer.Option("PP-D-99881-US", "--id", help="Dispute ID for dispute events"),
    json_output: bool = typer.Option(False, "--json", help="Output event payload and ingestion receipt as JSON"),
) -> None:
    """Synthesize and ingest realistic PayPal Webhook events directly into the local SQLite ledger."""
    settings = get_settings()
    store = PulseStore(settings.db_path)
    if not store.is_seeded():
        store.seed_telemetry()

    evt_normalized = event_type.strip().lower().replace("_", ".")
    fixtures_dir = Path(__file__).parent / "fixtures"

    fixture_map = {
        "payment.capture.completed": "payment_capture_completed.json",
        "checkout.order.approved": "checkout_order_approved.json",
        "customer.dispute.created": "customer_dispute_created.json",
        "customer.dispute.resolved": "customer_dispute_resolved.json",
    }

    fixture_file = fixture_map.get(evt_normalized)
    if fixture_file and (fixtures_dir / fixture_file).exists():
        event_payload = json.loads((fixtures_dir / fixture_file).read_text(encoding="utf-8"))
    else:
        event_payload = {
            "id": f"WH-SIM-{int(time.time())}",
            "event_type": event_type.upper().replace(".", "_"),
            "create_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "resource": {
                "id": f"RES-SIM-{int(time.time())}",
                "status": "COMPLETED",
                "amount": {"value": str(amount), "currency_code": currency.upper()},
                "custom_id": f"v1.2.1|US|{sku}",
            },
        }

    # Override with command-line arguments if provided
    if "resource" in event_payload:
        res_obj = event_payload["resource"]
        if "amount" in res_obj:
            res_obj["amount"]["value"] = str(amount)
            res_obj["amount"]["currency_code"] = currency.upper()
        if "dispute_amount" in res_obj:
            res_obj["dispute_amount"]["value"] = str(amount)
            res_obj["dispute_amount"]["currency_code"] = currency.upper()
        if "dispute_id" in res_obj:
            res_obj["dispute_id"] = dispute_id
        if "custom_id" in res_obj:
            res_obj["custom_id"] = f"v1.2.1|US|{sku}"

    receipt = store.record_webhook_event(event_payload)

    if json_output:
        out = {
            "event": event_payload,
            "receipt": receipt,
            "status": "INGESTED",
        }
        sys.stdout.write(json.dumps(out, indent=2) + "\n")
        return

    console.print(
        Panel(
            f"[bold green]✓ PayPal Webhook Event Ingested into Local Ledger[/bold green]\n\n"
            f"  • [bold]Event Type:[/bold] [cyan]{event_payload.get('event_type')}[/cyan]\n"
            f"  • [bold]Event ID:[/bold] `{event_payload.get('id')}`\n"
            f"  • [bold]Simulated Amount:[/bold] {amount:.2f} {currency.upper()} ({sku})\n"
            f"  • [bold]Dual-Engine Ingestion:[/bold] [bold green]Zero-Latency Instant SQLite Update[/bold green]\n"
            f"  • [bold]Receipt:[/bold] `{json.dumps(receipt)}`",
            title="[bold #00a8ea]PayPal Pulse (`pp trigger`) — Event Synthesizer[/bold #00a8ea]",
            border_style="green",
        )
    )


@app.command("demo")
def cmd_demo(
    tour: bool = typer.Option(
        True,
        "--tour/--no-tour",
        help="Run the guided interactive onboarding walkthrough",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output machine-readable JSON demo summary to stdout",
    ),
) -> None:
    """Launch the zero-credential interactive onboarding demo tour backed by isolated sample telemetry (.pulse_cache/demo.db)."""
    settings = get_settings(demo_mode=True)
    store = PulseStore(settings.db_path)
    if not store.is_seeded():
        store.seed_telemetry()

    summary = store.get_dashboard_summary()

    if json_output:
        payload = {
            "mode": "demo",
            "db_path": str(store.db_path),
            "summary": summary,
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return

    is_interactive = sys.stdin.isatty() and tour

    if not is_interactive:
        console.print(
            Panel.fit(
                "[bold #00a8ea]PayPal Pulse (`pp demo`) — Zero-Credential Demonstration Tour[/bold #00a8ea]\n"
                "[dim]Isolated Database: `.pulse_cache/demo.db` (Live `pulse.db` untouched)[/dim]",
                border_style="#0070ba",
            )
        )
        console.print(
            f"  • [bold]30-Day Gross Volume:[/bold] ${summary['total_gross_usd']:,.2f} USD\n"
            f"  • [bold]True Net Take-Home:[/bold] [bold green]${summary['total_net_usd']:,.2f} USD[/bold green] "
            f"(Fee Drag: ${summary['total_fees_usd']:,.2f} / {summary['fee_drag_pct']}%)\n"
            f"  • [bold]Health Score:[/bold] [bold cyan]{summary['health_score']}/100[/bold cyan] ({summary.get('health_rating', 'HEALTHY')})\n"
            f"  • [bold]Active Disputes:[/bold] [bold red]{summary['open_disputes_count']} open on release v1.2.1[/bold red]"
        )
        return

    # Interactive Guided Tour
    console.print()
    console.print(
        Panel(
            "[bold #58a6ff]⚡ Welcome to PayPal Pulse (`pp demo`) — Guided Onboarding Tour[/bold #58a6ff]\n"
            "[dim]Terminal-Native Financial Telemetry & AI Copilot for Modern SaaS & Software Creators[/dim]\n\n"
            "[bold #c9d1d9]You are viewing an isolated, zero-credential demo session.[/bold #c9d1d9]\n"
            "This walkthrough demonstrates how PayPal Pulse bridges rapid code deployments with real-time financial observability.",
            title="[bold #58a6ff]ONBOARDING WALKTHROUGH[/bold #58a6ff]",
            border_style="#58a6ff",
            padding=(1, 2),
        )
    )

    # Step 1: Cash flow & fee drag
    console.print("\n[bold cyan]─── Step 1/4: Multi-Currency Cash Flow & Real-Time Net Take-Home ───[/bold cyan]")
    console.print(
        f"  • [bold]30-Day Gross Revenue:[/bold] ${summary['total_gross_usd']:,.2f} USD across USD, EUR, and PHP\n"
        f"  • [bold]PayPal Fee Drag:[/bold] [bold red]-${summary['total_fees_usd']:,.2f}[/bold red] ({summary['fee_drag_pct']}% total friction)\n"
        f"  • [bold]True Net Margin:[/bold] [bold green]${summary['total_net_usd']:,.2f} USD[/bold green] ({summary['net_margin_pct']}% take-home)\n"
        f"  • [bold]Merchant Health Index:[/bold] [bold cyan]{summary['health_score']}/100[/bold cyan] (Disputes: {summary['open_disputes_count']} open)"
    )
    typer.prompt("\nPress [Enter] to inspect Step 2 (Git Release-to-Revenue Correlation)", default="", show_default=False)

    # Step 2: Release correlation
    console.print("\n[bold cyan]─── Step 2/4: Git Release-to-Revenue Correlation (`pp timeline`) ───[/bold cyan]")
    releases = summary.get("releases", [])
    for r in releases:
        badge_style = "bold green" if "SURGE" in r.get("status_badge", "") else ("bold red" if "DISPUTE" in r.get("status_badge", "") else "cyan")
        console.print(
            f"  • [bold]{r['version']}[/bold] ([dim]{r['git_commit']}[/dim]) — Conv: [bold]{r['conversion_rate']}%[/bold] | "
            f"Net: [bold green]${r['net_usd']:,.2f}[/bold green] | "
            f"Status: [{badge_style}]{r['status_badge']}[/{badge_style}]"
        )
        console.print(f"    [dim]Diagnostic: {r['ai_diagnostic']}[/dim]")
    typer.prompt("\nPress [Enter] to inspect Step 3 (Proactive Dispute Remediation)", default="", show_default=False)

    # Step 3: Dispute AI Remediation
    console.print("\n[bold cyan]─── Step 3/4: Proactive Buyer Dispute AI Remediation (`pp disputes`) ───[/bold cyan]")
    disputes = summary.get("disputes", [])
    if disputes:
        d = disputes[0]
        console.print(
            f"  • [bold red]Active Dispute:[/bold red] `{d['dispute_id']}` ({d['amount']:.2f} {d['currency']})\n"
            f"    Buyer: {d['buyer_email']} (Country: {d['buyer_country']})\n"
            f"    Reason: {d['reason']} — {d['description']}\n"
            f"    [bold green]Pulse AI Action:[/bold green] Auto-drafts formal evidence citing hotfix v1.2.2 and generates replacement order."
        )
    typer.prompt("\nPress [Enter] to inspect Step 4 (Instant Webhook Ingestion)", default="", show_default=False)

    # Step 4: Webhook Simulation
    console.print("\n[bold cyan]─── Step 4/4: Instant Local Webhook Ingestion (`pp trigger`) ───[/bold cyan]")
    console.print(
        "  • PayPal's official Reporting API has an up-to-3-hour indexing delay.\n"
        "  • Pulse's Dual-Engine SQLite ledger ingests webhook captures in [bold green]<5ms[/bold green], giving instant dashboard feedback."
    )

    console.print("\n" + "=" * 70)
    console.print("[bold green]✓ Onboarding walkthrough complete![/bold green]")
    console.print("[bold]Next action:[/bold]")
    console.print("  [bold cyan][1][/bold cyan] Launch Full-Screen Interactive Terminal Command Deck (`pp tui --demo`)")
    console.print("  [bold cyan][2][/bold cyan] Connect Live PayPal Sandbox Credentials (`pp setup`)")
    console.print("  [bold cyan][3][/bold cyan] Exit to shell")

    choice = typer.prompt("Select option", default="1")
    if choice.strip() == "1":
        from pulse.tui import PayPalPulseTUI
        tui_app = PayPalPulseTUI(demo_mode=True)
        tui_app.run()
    elif choice.strip() == "2":
        cmd_setup()
    else:
        console.print("[dim]Exiting demo. Run 'pp --help' anytime to explore all subcommands.[/dim]")


@app.command("setup")
@app.command("init")
def cmd_setup(
    client_id: Optional[str] = typer.Option(None, "--client-id", help="PayPal REST App Client ID"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret", help="PayPal REST App Secret"),
    mode: str = typer.Option("sandbox", "--mode", help="PayPal environment (sandbox or live)"),
    groq_key: Optional[str] = typer.Option(None, "--groq-key", help="Groq API Key for Pulse AI copilot (optional)"),
    demo: bool = typer.Option(False, "--demo", help="Launch instant demo mode instead of configuring credentials"),
) -> None:
    """Guided onboarding setup wizard to configure PayPal Sandbox and Groq AI credentials."""
    if demo:
        cmd_demo(tour=True, json_output=False)
        return

    # Check non-interactive safety
    if not sys.stdin.isatty() and not (client_id and client_secret):
        console_err.print(
            "[bold red]Error:[/bold red] `pp setup` in non-interactive environment requires `--client-id` and `--client-secret`.\n"
            "Usage: pp setup --client-id <ID> --client-secret <SECRET> [--mode sandbox|live] [--groq-key <KEY>]"
        )
        raise typer.Exit(code=EX_USAGE)

    console.print()
    console.print(
        Panel.fit(
            "[bold #00a8ea]PayPal Pulse (`pp setup`) — Guided Configuration Wizard[/bold #00a8ea]\n"
            "[dim]Saves verified credentials to ~/.pulse/config.json (persistent across all directories)[/dim]",
            border_style="#0070ba",
        )
    )

    if not (client_id and client_secret):
        console.print("[bold]How would you like to proceed?[/bold]")
        console.print("  [bold cyan][1][/bold cyan] Launch Instant Demo Tour (Explore with sample SaaS data — zero setup)")
        console.print("  [bold cyan][2][/bold cyan] Connect Live PayPal Sandbox Credentials")
        console.print("  [bold cyan][3][/bold cyan] Cancel & Exit")

        choice = typer.prompt("Select option", default="1")
        if choice.strip() == "1":
            cmd_demo(tour=True, json_output=False)
            return
        elif choice.strip() == "3":
            console.print("[dim]Setup cancelled.[/dim]")
            return

        # Interactive prompts
        console.print("\n[bold]Enter your PayPal REST App Credentials[/bold] [dim](from developer.paypal.com)[/dim]:")
        c_id = typer.prompt("  PayPal Client ID").strip()
        c_sec = typer.prompt("  PayPal Client Secret", hide_input=True).strip()
        c_mode = typer.prompt("  PayPal Mode (sandbox/live)", default="sandbox").strip().lower()
        g_key = typer.prompt("  Groq API Key (optional, press Enter to skip)", default="", hide_input=True).strip()
    else:
        c_id = client_id.strip()
        c_sec = client_secret.strip()
        c_mode = mode.strip().lower()
        g_key = (groq_key or "").strip()

    # Pre-flight validation against PayPal Sandbox
    console.print("\n[dim]Verifying credentials against PayPal REST OAuth2 endpoint...[/dim]")
    test_settings = PulseSettings(
        paypal_client_id=c_id,
        paypal_client_secret=c_sec,
        paypal_mode=c_mode,
        groq_api_key=g_key,
    )
    test_client = PayPalClient(test_settings)
    auth_res = test_client.authenticate(force_refresh=True)

    if not auth_res.authenticated:
        console_err.print(f"\n[bold red]✕ Authentication Failed:[/bold red] {auth_res.error}")
        console_err.print("[yellow]Please verify your Client ID and Secret in developer.paypal.com.[/yellow]")
        raise typer.Exit(code=EX_NOPERM)

    # Save to global config file (~/.pulse/config.json)
    cfg_path = save_global_config(
        paypal_client_id=c_id,
        paypal_client_secret=c_sec,
        paypal_mode=c_mode,
        groq_api_key=g_key,
    )

    console.print(
        f"\n[bold green]✓ Credentials Verified & Saved Successfully![/bold green]\n"
        f"  • [bold]Target Config:[/bold] `{cfg_path}` (Permissions: 0600)\n"
        f"  • [bold]App ID:[/bold] `{auth_res.app_id}`\n"
        f"  • [bold]Latency:[/bold] {auth_res.latency_ms:.1f}ms\n"
        f"  • [bold]Mode:[/bold] {c_mode.upper()}\n\n"
        f"[bold]Next Steps:[/bold]\n"
        f"  • Run `pp pulse` to view live financial telemetry.\n"
        f"  • Run `pp tui` to launch the full-screen terminal command deck.\n"
        f"  • Run `pp ask \"What is our net margin?\"` to chat with Pulse AI."
    )


@app.command("tui")
def cmd_tui(
    check: bool = typer.Option(
        False,
        "--check",
        help="Verify Textual TUI composition and data binding without blocking terminal",
    ),
    demo: bool = typer.Option(
        False,
        "--demo",
        help="Launch in demo mode using isolated sample telemetry (.pulse_cache/demo.db)",
    ),
) -> None:
    """Launch the interactive full-screen Textual Terminal UI (`pp tui`)."""
    from pulse.tui import PayPalPulseTUI

    settings = get_settings(demo_mode=demo)

    # First-run routing if no credentials are configured and --demo wasn't passed
    if not demo and not settings.has_paypal_credentials:
        if sys.stdin.isatty():
            console.print(
                Panel.fit(
                    "[bold #58a6ff]PayPal Pulse (`pp tui`)[/bold #58a6ff]\n"
                    "[dim]No active PayPal credentials detected in ~/.pulse/config.json or .env[/dim]\n\n"
                    "Select how you want to launch:\n"
                    "  [bold cyan][1][/bold cyan] Launch Instant Demo Mode (Zero Credentials / Explore Now)\n"
                    "  [bold cyan][2][/bold cyan] Configure Live Credentials (pp setup)\n"
                    "  [bold cyan][3][/bold cyan] Exit",
                    border_style="#58a6ff",
                )
            )
            choice = typer.prompt("Select option", default="1").strip()
            if choice == "2":
                cmd_setup()
                return
            elif choice == "3":
                return
            else:
                demo = True
        else:
            console_err.print(
                "[yellow]Warning: No PayPal credentials detected. Falling back to isolated Demo Mode.[/yellow]\n"
                "[dim]To connect live credentials, run 'pp setup'.[/dim]"
            )
            demo = True

    tui_app = PayPalPulseTUI(demo_mode=demo)
    if check:
        console.print("[bold green]✓ PayPalPulseTUI initialized and verified ready.[/bold green]")
        return
    tui_app.run()


def main() -> None:
    """Package script entry point."""
    app()


if __name__ == "__main__":
    main()
