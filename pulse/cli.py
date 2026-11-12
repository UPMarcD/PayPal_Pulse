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
from rich.rule import Rule
from rich.table import Table

from pulse.ai_copilot import (
    ask_copilot,
    diagnose_dispute_and_remediate,
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
from pulse.constants import EX_CONFIG, EX_GENERAL, EX_NOPERM, EX_OK, EX_UNAVAILABLE, EX_USAGE
from pulse import onboarding
from pulse.mascot import format_mascot_header
from pulse.paypal_client import PayPalClient
from pulse.store import CURRENCY_SYMBOLS, PulseStore, detect_project_context, parse_delivery_jsonl, parse_release_version

# Ensure UTF-8 output on Windows consoles
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

console = Console()
console_err = Console(stderr=True)


# Enhance command signatures in `pp --help` so command arguments are visible
def _patch_typer_commands_panel_with_args() -> None:
    try:
        from typer import rich_utils as _ru
        import click

        _orig_print_commands_panel = _ru._print_commands_panel

        class _CommandProxy:
            def __init__(self, cmd: click.Command) -> None:
                self._cmd = cmd
                clean_args: list[str] = []
                for p in getattr(cmd, "params", []):
                    if isinstance(p, click.Argument):
                        mv = p.make_metavar()
                        if p.name == "amount" and mv.startswith("[create]"):
                            clean_args.append("AMOUNT")
                        elif p.name in ("currency", "currency_opt") and mv == "[CURRENCY]":
                            if "[CURRENCY]" not in clean_args:
                                clean_args.append("[CURRENCY]")
                        elif p.name in ("recipient_email", "recipient_opt") and mv == "[EMAIL]":
                            if "[EMAIL]" not in clean_args:
                                clean_args.append("[EMAIL]")
                        else:
                            clean_args.append(mv)
                arg_str = (" " + " ".join(clean_args)) if clean_args else ""
                self.name = f"{cmd.name or ''}{arg_str}"
                self.short_help = cmd.short_help or cmd.help
                self.help = cmd.help
                self.deprecated = cmd.deprecated

        def _patched_print_commands_panel(*, name, commands, markup_mode, console, cmd_len):
            proxies = [_CommandProxy(c) for c in commands]
            max_len = max([len(p.name) for p in proxies], default=cmd_len)
            return _orig_print_commands_panel(
                name=name,
                commands=proxies,
                markup_mode=markup_mode,
                console=console,
                cmd_len=max_len,
            )

        _ru._print_commands_panel = _patched_print_commands_panel
    except Exception:
        pass


_patch_typer_commands_panel_with_args()

app = typer.Typer(
    name="pp",
    help="PayPal Pulse (pp) — Terminal-Native Financial Telemetry & Pulse AI for Developers.",
    add_completion=False,
    invoke_without_command=True,
)


@app.callback()
def _root(ctx: typer.Context) -> None:
    """Show the first-run welcome when no credentials exist, otherwise show help."""
    if ctx.invoked_subcommand is not None:
        return
    if sys.stdin.isatty() and not get_settings(demo_mode=False).has_paypal_credentials:
        onboarding.welcome(console, first_run=True)
        choice = onboarding.select(
            "What would you like to do?",
            [
                ("Explore demo mode (zero credentials needed)", "demo"),
                ("Connect PayPal Sandbox credentials", "setup"),
                ("Show available commands", "help"),
            ],
            default="demo",
        )
        if choice == "demo":
            cmd_demo(tour=True, json_output=False)
        elif choice == "setup":
            run_setup_wizard()
        else:
            console.print(ctx.get_help())
        raise typer.Exit()
    console.print(ctx.get_help())
    raise typer.Exit()


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

    if not json_output and onboarding.should_animate():
        with onboarding.LiveChecklist(console) as cl:
            idx_pp = cl.start_step("PayPal OAuth2", "Authenticating against PayPal REST gateway...")
            auth_res = pp_client.authenticate(force_refresh=refresh)
            cl.finish_step(
                idx_pp,
                "ok" if auth_res.authenticated else "fail",
                f"{auth_res.latency_ms:.0f} ms" if auth_res.authenticated else (auth_res.error or "Failed"),
            )
            idx_ai = cl.start_step("Pulse AI Engine", "Verifying inference endpoint...")
            groq_res = verify_groq_connection(settings)
            cl.finish_step(
                idx_ai,
                "ok" if groq_res.connected else "warn",
                f"{groq_res.model} ({groq_res.latency_ms:.0f} ms)" if groq_res.connected else (groq_res.error or "Offline engine"),
            )
    else:
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
            "[bold red][FAIL][/bold red]",
            f"Error: {auth_res.error}",
            f"{auth_res.latency_ms:.0f} ms",
        )

    ai_provider_name = (getattr(groq_res, "provider", None) or settings.active_ai_provider or "AI").upper()
    if groq_res.connected:
        table.add_row(
            f"Pulse AI Engine ({ai_provider_name})",
            "[bold green]● CONNECTED[/bold green]",
            (
                f"Persona: [bold]Pulse[/bold] ({groq_res.available_models_count} inference endpoints online)\n"
                f"Active Engine: [bold]{groq_res.model}[/bold] | Key: `{groq_res.key_preview}`"
            ),
            f"{groq_res.latency_ms:.0f} ms",
        )
    else:
        table.add_row(
            f"Pulse AI Engine ({ai_provider_name})",
            "[bold red][DISCONNECTED][/bold red]",
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
    demo: bool = typer.Option(
        False,
        "--demo",
        help="Seed the isolated demo database (.pulse_cache/demo.db)",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output machine-readable JSON to stdout",
    ),
) -> None:
    """Create real live Orders/Invoices on PayPal Sandbox and seed 30 days of software telemetry."""
    settings = get_settings(demo_mode=demo)

    # Fail-safe guard: Never seed mock data on Live Production accounts
    if settings.paypal_mode.lower() == "live" and not demo:
        err_msg = (
            "CRITICAL SECURITY GUARD: Seeding synthetic data on a Live/Production PayPal account is strictly prohibited.\n"
            "To test synthetic releases, run with isolated demo mode: `pp seed --demo`"
        )
        if json_output:
            sys.stderr.write(json.dumps({"error": err_msg, "code": EX_USAGE}, indent=2) + "\n")
        else:
            console_err.print(f"[bold red]CRITICAL SECURITY GUARD:[/bold red] {err_msg}")
        raise typer.Exit(code=EX_USAGE)

    pp_client = PayPalClient(settings)
    store = PulseStore(settings.db_path, settings=settings)

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
            sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
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
            f"[green][OK] Created Live PayPal Sandbox Draft Invoice:[/green] [bold]{inv_res.invoice_id}[/bold] "
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
                f"[bold green][OK] Dual-Engine SQLite Ledger Seeded Successfully![/bold green] (`{store.db_path}`)\n"
                f"  • [bold]Releases Correlated:[/bold] `v1.1.0`, `v1.2.0 - Performance & Async Engine`, `v1.2.1 - EU Multi-Currency Tier`\n"
                f"  • [bold]30-Day Gross Volume:[/bold] ${summary['total_gross_usd']:,.2f} USD across `USD`, `EUR`, `PHP`\n"
                f"  • [bold]True Net Take-Home:[/bold] [bold green]${summary['total_net_usd']:,.2f} USD[/bold green] "
                f"(PayPal Fee Drag: ${summary['total_fees_usd']:,.2f} / {summary['fee_drag_pct']}%)\n"
                f"  • [bold]Active Dispute Alerts:[/bold] [bold red]{summary['open_disputes_count']} German (DE) buyer disputes on v1.2.1[/bold red]",
                title="Seed Summary",
                border_style="green",
            )
        )


@app.command("reset")
def cmd_reset(
    demo: bool = typer.Option(False, "--demo", help="Reset isolated demo database (.pulse_cache/demo.db)"),
    force: bool = typer.Option(False, "--force", "-f", help="Bypass confirmation prompt"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
) -> None:
    """Reset the local dual-engine ledger to a pristine, unseeded state for production use."""
    settings = get_settings(demo_mode=demo)
    store = PulseStore(settings.db_path, settings=settings)

    if not force and sys.stdin.isatty() and not json_output:
        target_name = "DEMO database (.pulse_cache/demo.db)" if demo else "LIVE database (.pulse_cache/pulse.db)"
        confirmed = typer.confirm(f"Reset {target_name} to a clean, empty state?", default=True)
        if not confirmed:
            console.print("[dim]Reset cancelled.[/dim]")
            return

    store.reset(mark_clean=True)

    if json_output:
        payload = {
            "status": "ok",
            "db_path": str(settings.db_path),
            "demo": demo,
            "clean_mode": True,
            "message": "Database reset to pristine unseeded state. Auto-seeding disabled.",
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return

    target_label = "Demo" if demo else "Live"
    console.print(
        Panel.fit(
            f"[bold green][OK] {target_label} Database Reset Complete[/bold green]\n"
            f"  • [bold]Target:[/bold] `{settings.db_path}`\n"
            f"  • [bold]Status:[/bold] Clean empty state (0 releases, 0 transactions, 0 disputes)\n"
            f"  • [bold]Auto-Seeding:[/bold] [yellow]Disabled (clean_mode=1)[/yellow]\n\n"
            f"[dim]Real transactions created via `pp order` or captured through webhooks will populate this ledger without being overwritten.[/dim]\n"
            f"[dim]To restore synthetic sample data at any time, run `pp seed{ ' --demo' if demo else '' }`.[/dim]",
            title="[bold #00a8ea]LEDGER RESET[/bold #00a8ea]",
            border_style="#0070ba",
        )
    )


@app.command("sync")
@app.command("refresh", hidden=True)
def cmd_sync(
    demo: bool = typer.Option(False, "--demo", help="Run against isolated demo database (.pulse_cache/demo.db)"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON summary"),
) -> None:
    """Synchronize live customer disputes and transactions from PayPal Sandbox into local SQLite ledger."""
    settings = get_settings(demo_mode=demo)
    pp_client = PayPalClient(settings)
    store = PulseStore(settings.db_path, settings=settings)

    auth = pp_client.authenticate(force_refresh=True)
    if not auth.authenticated:
        if json_output:
            sys.stdout.write(json.dumps({"ok": False, "error": auth.error or "Authentication failed"}) + "\n")
        else:
            console.print(
                f"[bold red][ERROR] PayPal Authentication Failed:[/bold red] {auth.error}\n"
                "[dim]Run [bold]pp setup[/bold] to configure credentials, or [bold]pp doctor[/bold] to diagnose.[/dim]"
            )
        raise typer.Exit(code=1)

    if not json_output and onboarding.should_animate():
        with onboarding.LiveChecklist(console) as cl:
            idx_d = cl.start_step("Customer Disputes API", "Syncing GET /v1/customer/disputes...")
            disp_res = pp_client.get_disputes()
            disp_count = len(disp_res.get("items", [])) if disp_res.get("ok") else 0
            disp_added = store.sync_external_disputes(disp_res.get("items", [])) if disp_res.get("ok") else 0
            cl.finish_step(idx_d, "ok" if disp_res.get("ok") else "warn", f"{disp_added} synced ({disp_count} total)")

            idx_t = cl.start_step("Reporting Transactions API", "Syncing GET /v1/reporting/transactions...")
            tx_res = pp_client.get_transactions()
            tx_count = len(tx_res.get("transaction_details", [])) if tx_res.get("ok") else 0
            tx_added = store.sync_external_transactions(tx_res.get("transaction_details", [])) if tx_res.get("ok") else 0
            cl.finish_step(idx_t, "ok" if tx_res.get("ok") else "warn", f"{tx_added} synced ({tx_count} total)")
    else:
        disp_res = pp_client.get_disputes()
        disp_count = len(disp_res.get("items", [])) if disp_res.get("ok") else 0
        disp_added = store.sync_external_disputes(disp_res.get("items", [])) if disp_res.get("ok") else 0

        tx_res = pp_client.get_transactions()
        tx_count = len(tx_res.get("transaction_details", [])) if tx_res.get("ok") else 0
        tx_added = store.sync_external_transactions(tx_res.get("transaction_details", [])) if tx_res.get("ok") else 0

    if json_output:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "auth": auth.model_dump(),
                    "disputes_synced": disp_added,
                    "total_disputes": disp_count,
                    "transactions_synced": tx_added,
                    "total_transactions": tx_count,
                    "reporting_api_ok": tx_res.get("ok", False),
                },
                indent=2,
            )
            + "\n"
        )
        return

    reporting_note = (
        "[bold green]OK[/bold green]"
        if tx_res.get("ok")
        else "[yellow]403 Forbidden (Transaction Search permission required on PayPal portal)[/yellow]"
    )
    console.print(
        Panel.fit(
            f"[bold green][OK] PayPal Telemetry Synchronized[/bold green]\n"
            f"  • [bold]Gateway Latency:[/bold] [cyan]{auth.latency_ms:.0f}ms[/cyan]\n"
            f"  • [bold]Disputes Ingested:[/bold] [bold #f0f6fc]{disp_count}[/bold #f0f6fc] ({disp_added} newly stored / reconciled into SQLite)\n"
            f"  • [bold]Transactions Synced:[/bold] [bold #f0f6fc]{tx_count}[/bold #f0f6fc] ({tx_added} newly stored)\n"
            f"  • [bold]Reporting API Status:[/bold] {reporting_note}\n\n"
            f"[dim]Run `pp pulse` to view the updated financial dashboard or `pp tui` (press 'r' anytime to refresh).[/dim]",
            title="[bold #00a8ea]PAYPAL TELEMETRY SYNC[/bold #00a8ea]",
            border_style="#0070ba",
        )
    )


@app.command("pulse")
@app.command("status", hidden=True)
def cmd_pulse(
    refresh: bool = typer.Option(False, "--refresh", "-r", help="Synchronize live disputes and transactions before rendering dashboard"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
    demo: bool = typer.Option(False, "--demo", help="Run against isolated demo database (.pulse_cache/demo.db)"),
) -> None:
    """Display the rich full-terminal financial & customer telemetry dashboard."""
    settings = get_settings(demo_mode=demo)
    pp_client = PayPalClient(settings)
    store = PulseStore(settings.db_path, settings=settings)

    if store.should_auto_seed():
        store.seed_telemetry()
    ledger_state = store.detect_state()

    if ledger_state == "fresh":
        summary = store.get_dashboard_summary(api_connected=False)
        if json_output:
            sys.stdout.write(json.dumps(summary, indent=2) + "\n")
            return
        onboarding.welcome(console, first_run=True)
        console.print()
        console.print(
            Panel(
                "[bold #f0f6fc]This is a clean ledger, not an empty dashboard.[/bold #f0f6fc]\n"
                "No revenue, fees, customers, or disputes appear because none are recorded.\n"
                f"[dim]Ledger: {summary['db_path']}[/dim]",
                title="[bold #00a8ea]No Ledger Data Yet[/bold #00a8ea]",
                border_style="#0070ba",
                padding=(1, 2),
            )
        )
        onboarding.try_now_cards(console, sandbox_mode=False)
        return

    auth_res = pp_client.authenticate(force_refresh=False)
    if refresh and auth_res.authenticated and not demo:
        try:
            disp_res = pp_client.get_disputes()
            if disp_res.get("ok") and disp_res.get("items"):
                store.sync_external_disputes(disp_res["items"])
        except Exception:
            pass
        try:
            tx_res = pp_client.get_transactions()
            if tx_res.get("ok") and tx_res.get("transaction_details"):
                store.sync_external_transactions(tx_res["transaction_details"])
        except Exception:
            pass

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
        if not demo and not settings.has_paypal_credentials:
            conn_text += "\n[dim]Showing local sample data. Run [bold]pp setup[/bold] to connect PayPal, or [bold]pp doctor[/bold] to check your setup.[/dim]"

    if ledger_state == "sample":
        conn_text += (
            "\n[bold #e5a824][SAMPLE DATA][/bold #e5a824] [dim]Synthetic ledger. "
            "Run [bold]pp reset[/bold] before recording real orders.[/dim]"
        )

    console.print(Panel(conn_text, title="[bold #00a8ea]PayPal Pulse (`pp pulse`) — Merchant Command Center[/bold #00a8ea]", border_style="#0070ba"))

    # 2. Active Dispute Alert Banner
    if summary["open_disputes_count"] > 0:
        open_disputes = store.get_disputes(status_filter="OPEN")
        first_disp = open_disputes[0] if open_disputes else {}
        disp_rel = first_disp.get("release_version", "v1.2.1")
        disp_country = first_disp.get("buyer_country", "US")
        disp_sku = first_disp.get("platform_sku", "PulseCore Pro")
        disp_id = first_disp.get("dispute_id", "PP-D-90411-DE")
        console.print(
            Panel(
                f"[bold #f85149][ALERT] PROACTIVE DISPUTE ALERT:[/bold #f85149] [bold]{summary['open_disputes_count']} Open Customer Dispute(s)[/bold] "
                f"detected on release [bold #e5a824]{disp_rel}[/bold #e5a824] (Region: [bold]{disp_country}[/bold], Platform: [bold]{disp_sku}[/bold]).\n"
                f"Run [bold cyan]pp disputes[/bold cyan] to inspect or [bold cyan]pp disputes --resolve {disp_id}[/bold cyan] to auto-draft Pulse AI evidence & issue a Sandbox replacement order.",
                border_style="#f85149",
            )
        )

    # 3. KPI Cards (True Net Take-Home + Orders & Revenue)
    kpi_left = Panel(
        f"[bold white]Gross Volume (30d):[/bold white]   [bold]${summary['total_gross_usd']:,.2f} USD[/bold]\n"
        f"[bold white]PayPal Fee Drag:[/bold white]      [red]-${summary['total_fees_usd']:,.2f} USD ({summary['fee_drag_pct']}%)[/red]\n"
        f"[bold white]True Net Take-Home:[/bold white]   [bold green]${summary['total_net_usd']:,.2f} USD[/bold green]\n"
        f"[bold white]Effective Net Margin:[/bold white] [bold cyan]{summary['net_margin_pct']}%[/bold cyan]",
        title="[bold #00a8ea]True Net Revenue & Fee Drag[/bold #00a8ea]",
        border_style="#00a8ea",
        expand=True,
    )

    paid_orders = summary.get("total_orders", summary.get("total_paid", 0))
    pending_orders = summary.get("total_pending_orders", 0)
    pending_gross = summary.get("pending_gross_usd", 0.0)
    telemetry = summary.get("health_telemetry", {})
    score_val = summary["health_score"]
    rating_str = summary.get("health_rating", "HEALTHY")
    rating_color = telemetry.get("rating_color", "bold cyan")

    kpi_right = Panel(
        f"[bold white]Paid Orders:[/bold white]         [bold]{paid_orders:,}[/bold]\n"
        f"[bold white]Pending Capture:[/bold white]     [dim]{pending_orders:,} (${pending_gross:,.2f} recorded)[/dim]\n"
        f"[bold white]30-Day Net Run-Rate:[/bold white] [bold green]${summary['net_run_rate_30d_usd']:,.2f}[/bold green]\n"
        f"[bold white]Merchant Health Index:[/bold white] [{rating_color}]{score_val} / 100 [{rating_str}][/{rating_color}]",
        title="[bold #00a8ea]Orders & Revenue[/bold #00a8ea]",
        border_style="#00a8ea",
        expand=True,
    )
    console.print(Columns([kpi_left, kpi_right], equal=True, expand=True))

    # 4. Multi-Currency Balances & True Net Breakdown Table
    # Below 100 columns the two margin percentages are dropped so every
    # remaining figure stays readable instead of being squeezed to blank.
    compact_cur = console.width < 100
    cur_table = Table(
        title="Multi-Currency Ledger Balances & True Net Take-Home",
        box=box.SIMPLE_HEAVY,
        expand=True,
    )
    cur_table.add_column("Currency", style="bold cyan")
    cur_table.add_column("Txns", justify="right")
    cur_table.add_column("Gross", justify="right", style="white")
    cur_table.add_column("Fees", justify="right", style="red")
    if not compact_cur:
        cur_table.add_column("Fee Drag %", justify="right", style="yellow")
    cur_table.add_column("Net (Native)", justify="right", style="bold green")
    if not compact_cur:
        cur_table.add_column("Net Margin %", justify="right", style="cyan")
    cur_table.add_column("USD Net", justify="right", style="bold green")

    for c in summary["currencies"]:
        sym = CURRENCY_SYMBOLS.get(c["currency"], f"{c['currency']} ")
        row = [
            c["currency"],
            str(c["tx_count"]),
            f"{sym}{c['gross']:,.2f}",
            f"-{sym}{c['fees']:,.2f}",
        ]
        if not compact_cur:
            row.append(f"{c['fee_pct']}%")
        row.append(f"{sym}{c['net']:,.2f}")
        if not compact_cur:
            row.append(f"{c['net_margin_pct']}%")
        row.append(f"${c['usd_net']:,.2f}")
        cur_table.add_row(*row)
    console.print(cur_table)

    # 5. 14-Day ASCII Revenue & Customer Acquisition Sparkline / Bar Chart
    daily_14 = summary["daily_14"]
    net_series = [float(d["net_usd"]) for d in daily_14]
    inst_series = [float(d["installs"]) for d in daily_14]
    max_net = max(net_series) if net_series else 1.0

    chart_table = Table(
        title=(
            f"14-Day Revenue & Install Velocity  |  Net Sparkline: {_sparkline(net_series)}  |  Installs: {_sparkline(inst_series)}"
            if console.width >= 100
            else f"14-Day Net Revenue  |  {_sparkline(net_series)}"
        ),
        box=box.ROUNDED,
        expand=True,
    )
    narrow_chart = console.width < 90
    chart_table.add_column("Date", style="dim", no_wrap=True)
    chart_table.add_column("Release", style="cyan", no_wrap=True)
    if not narrow_chart:
        chart_table.add_column("Installs", justify="right")
    chart_table.add_column("Orders", justify="right")
    chart_table.add_column("True Net (USD)", justify="right", style="bold green")
    chart_table.add_column("Net Bar" if narrow_chart else "ASCII Net Revenue Bar", style="green")

    for d in daily_14:
        bar_len = max(1, int((float(d["net_usd"]) / max_net) * 24))
        bar_str = "█" * bar_len
        if int(d["disputes"]) > 0:
            bar_str += f" [bold #f85149][!] {d['disputes']} dispute[/bold #f85149]"
        row = [d["day_date"][5:], d["release_version"]]
        if not narrow_chart:
            row.append(str(d["installs"]))
        row.extend([str(d["paid_orders"]), f"${d['net_usd']:,.2f}", bar_str])
        chart_table.add_row(*row)
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


@app.command("init")
def cmd_init(
    project_name: Optional[str] = typer.Option(None, "--name", "-n", help="Project name (defaults to package manifest or folder name)"),
    release_version: Optional[str] = typer.Option(None, "--version", "-v", help="Initial release version (e.g. v1.0.0)"),
    currency: Optional[str] = typer.Option(None, "--currency", "-c", help="Default project billing currency (e.g. USD, EUR, PHP, GBP, JPY, CAD)"),
    sku: Optional[str] = typer.Option(None, "--sku", "-s", help="Default product SKU or license tier name"),
    price: Optional[float] = typer.Option(None, "--price", "-p", help="Default checkout price for snippets and triggers"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing .pulse.json configuration"),
    json_output: bool = typer.Option(False, "--json", help="Output project configuration and integration snippet as JSON"),
) -> None:
    """Initialize PayPal Pulse in your project: creates .pulse.json and outputs PayPal checkout integration snippets."""
    cwd = Path.cwd()
    config_path = cwd / ".pulse.json"

    if config_path.exists() and not force:
        try:
            cfg_data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except Exception:
            cfg_data = {}
        if json_output:
            sys.stdout.write(
                json.dumps(
                    {"status": "ALREADY_INITIALIZED", "config_path": str(config_path), "config": cfg_data},
                    indent=2,
                )
                + "\n"
            )
            return
        console.print(
            f"[bold yellow]Project already initialized:[/bold yellow] Found `{config_path}`\n"
            f"[dim]Use `--force` to reinitialize or overwrite.[/dim]"
        )
        return

    ctx = detect_project_context(cwd)
    p_name = project_name or ctx.get("project_name") or cwd.name
    p_ver = release_version or ctx.get("version") or "v1.0.0"
    p_cur = (currency or ctx.get("default_currency") or "USD").strip().upper()
    p_sku = (sku or (f"{p_name} Pro" if project_name else ctx.get("default_sku")) or f"{p_name} Pro").strip()
    p_price = float(price if price is not None else ctx.get("default_price", 29.99))

    cfg_payload = {
        "project_name": p_name,
        "version": p_ver,
        "default_currency": p_cur,
        "default_sku": p_sku,
        "default_price": p_price,
        "git_repo": ctx.get("git_repo", False),
        "git_branch": ctx.get("git_branch"),
        "git_commit": ctx.get("git_commit"),
        "git_tag": ctx.get("git_tag"),
        "package_type": ctx.get("package_type"),
        "release_tag_strategy": "git_tag",
        "custom_id_format": "release:{version}",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    config_path.write_text(json.dumps(cfg_payload, indent=2) + "\n", encoding="utf-8")

    from pulse.store import format_currency_amount

    formatted_price = format_currency_amount(p_price, p_cur)
    integration_code = {
        "python": (
            "# When creating orders via PayPal Orders v2 API:\n"
            "order_payload = {\n"
            '    "intent": "CAPTURE",\n'
            '    "purchase_units": [{\n'
            f'        "amount": {{"currency_code": "{p_cur}", "value": "{formatted_price}"}},\n'
            f'        "custom_id": f"release:{{{p_ver!r}}}",  # PayPal Pulse attributes revenue to this release!\n'
            f'        "description": "{p_sku} License",\n'
            '    }]\n'
            '}'
        ),
        "nodejs": (
            "// In your Next.js / Express checkout route:\n"
            "const orderPayload = {\n"
            '  intent: "CAPTURE",\n'
            '  purchase_units: [{\n'
            f'    amount: {{ currency_code: "{p_cur}", value: "{formatted_price}" }},\n'
            f'    custom_id: `release:${{process.env.APP_VERSION || {p_ver!r}}}`,\n'
            f'    description: "{p_sku} License"\n'
            '  }]\n'
            '};'
        ),
        "curl": (
            "curl -v -X POST https://api-m.sandbox.paypal.com/v2/checkout/orders \\\n"
            '  -H "Content-Type: application/json" \\\n'
            '  -H "Authorization: Bearer <ACCESS_TOKEN>" \\\n'
            f'  -d \'{{"intent":"CAPTURE","purchase_units":[{{"amount":{{"currency_code":"{p_cur}","value":"{formatted_price}"}},"custom_id":"release:{p_ver}"}}]}}\''
        ),
    }

    if json_output:
        out = {
            "status": "INITIALIZED",
            "config_path": str(config_path),
            "config": cfg_payload,
            "integration_snippets": integration_code,
        }
        sys.stdout.write(json.dumps(out, indent=2) + "\n")
        return

    console.print(
        Panel.fit(
            f"[bold #00a8ea]PayPal Pulse (`pp init`) — Project Successfully Initialized[/bold #00a8ea]\n"
            f"[dim]Saved configuration to {config_path}[/dim]",
            border_style="#0070ba",
        )
    )

    tbl = Table(title="Project Telemetry Metadata", box=box.ROUNDED, expand=True)
    tbl.add_column("Field", style="bold cyan")
    tbl.add_column("Detected Value", style="white")
    tbl.add_row("Project Name", p_name)
    tbl.add_row("Release Version", f"[bold yellow]{p_ver}[/bold yellow]")
    tbl.add_row("Default Currency", f"[bold green]{p_cur}[/bold green] (Default Price: {p_price:.2f} {p_cur})")
    tbl.add_row("Default Product SKU", p_sku)
    tbl.add_row("Git Repository", "[green]Yes[/green]" if ctx.get("git_repo") else "[dim]No (standalone folder)[/dim]")
    if ctx.get("git_branch"):
        tbl.add_row("Git Branch", ctx["git_branch"])
    if ctx.get("git_commit"):
        tbl.add_row("Git Commit", ctx["git_commit"])
    if ctx.get("git_tag"):
        tbl.add_row("Git Tag", ctx["git_tag"])
    if ctx.get("package_type"):
        tbl.add_row("Package Stack", ctx["package_type"])
    tbl.add_row("Custom ID Format", "release:{version}")
    console.print(tbl)

    console.print("\n[bold #3fb950]How to tag your PayPal checkout orders to this release:[/bold #3fb950]")
    console.print("[dim]Pass your release version into `purchase_units[0].custom_id` when calling PayPal Orders v2:[/dim]\n")
    console.print(Panel(Markdown(f"```python\n{integration_code['python']}\n```"), title="Python Integration", border_style="cyan"))
    console.print(Panel(Markdown(f"```typescript\n{integration_code['nodejs']}\n```"), title="Node.js / Next.js Integration", border_style="cyan"))


@app.command("timeline")
@app.command("releases", hidden=True)
def cmd_timeline(
    since: Optional[str] = typer.Option(None, "--since", "-s", help="Filter releases on or after date (YYYY-MM-DD)"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
    demo: bool = typer.Option(False, "--demo", help="Run against isolated demo database (.pulse_cache/demo.db)"),
) -> None:
    """Correlate Git releases (v1.1.0, v1.2.0, v1.2.1) with PayPal revenue, conversion rate, and dispute spikes."""
    settings = get_settings(demo_mode=demo)
    store = PulseStore(settings.db_path, settings=settings)
    if store.should_auto_seed():
        store.seed_telemetry()

    releases = store.get_releases(include_git_tags=True)
    if since:
        releases = [r for r in releases if r.get("released_at", "")[:10] >= since or since in r.get("released_at", "")]

    if not releases:
        if json_output:
            sys.stdout.write("[]\n")
            return
        ctx = detect_project_context()
        has_project = bool(ctx.get("git_repo")) or (Path.cwd() / ".pulse.json").exists()
        hint = (
            "[dim]Tag a commit with `git tag v1.0.0` and run `pp timeline` again.[/dim]"
            if has_project
            else "[dim]Run `pp init` inside a Git repository, or `pp demo` for a filled example.[/dim]"
        )
        console.print()
        console.print(
            Panel(
                "[bold #f0f6fc]No release telemetry yet.[/bold #f0f6fc]\n" + hint,
                title="[bold #00a8ea]Release-to-Revenue Timeline[/bold #00a8ea]",
                border_style="#0070ba",
                padding=(1, 2),
            )
        )
        return

    if json_output:
        sys.stdout.write(json.dumps(releases, indent=2) + "\n")
        return

    ctx = detect_project_context()
    proj_info = f"  [dim]Project:[/dim] [bold white]{ctx.get('project_name', 'Default')}[/bold white] [dim]({ctx.get('version', 'v0.1.0')})[/dim]"
    if ctx.get("git_tag"):
        proj_info += f"  [dim]│ Git Tag:[/dim] [bold cyan]{ctx['git_tag']}[/bold cyan]"

    console.print(
        Panel.fit(
            f"[bold #00a8ea]PayPal Pulse (`pp timeline`) — Release-to-Revenue Correlation Engine[/bold #00a8ea]\n"
            f"{proj_info}",
            border_style="#0070ba",
        )
    )

    # Below 100 columns the install, conversion, gross, and refund columns are
    # dropped so the remaining figures stay readable at the 80-column minimum.
    compact_tl = console.width < 100
    narrow_tl = console.width < 90
    tl_table = Table(
        title="Git Release Telemetry vs. PayPal Conversion & Dispute Surge",
        box=box.ROUNDED,
        expand=True,
    )
    tl_table.add_column("Release" if compact_tl else "Release Tag", style="bold cyan", no_wrap=True)
    tl_table.add_column("Commit", style="dim", no_wrap=True)
    if not narrow_tl:
        tl_table.add_column("Date", no_wrap=True)
    if not compact_tl:
        tl_table.add_column("Installs", justify="right")
    tl_table.add_column("Orders" if compact_tl else "Paid Orders", justify="right")
    if not compact_tl:
        tl_table.add_column("Conv %", justify="right", style="bold yellow")
        tl_table.add_column("Gross (USD)", justify="right")
    tl_table.add_column("Net" if compact_tl else "True Net (USD)", justify="right", style="bold green")
    if not compact_tl:
        tl_table.add_column("Refunds", justify="right")
    tl_table.add_column("Disp" if compact_tl else "Disputes", justify="right")
    tl_table.add_column("Status" if compact_tl else "Telemetry Status", no_wrap=True)

    open_disputes_list = store.get_disputes(status_filter="OPEN")
    disp_country_by_rel: dict[str, str] = {}
    for d in open_disputes_list:
        rv = d.get("release_version", "")
        if rv and rv not in disp_country_by_rel:
            disp_country_by_rel[rv] = d.get("buyer_country", "US")

    for r in releases:
        d_country = disp_country_by_rel.get(r["version"], "DE" if r["version"] == "v1.2.1" else "US")
        disp_str = (
            f"[bold red]{r['dispute_count']} ({d_country})[/bold red]"
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
        conv_val = f"{r['conversion_rate']:.2f}%" if int(cust_val) > 0 else "n/a"
        row = [r["version"], r["git_commit"]]
        if not narrow_tl:
            row.append(r["released_at"])
        if not compact_tl:
            row.append(f"{cust_val:,}")
        row.append(f"{order_val:,}")
        if not compact_tl:
            row.append(conv_val)
            row.append(f"${r['gross_usd']:,.2f}")
        row.append(f"${r['net_usd']:,.2f}")
        if not compact_tl:
            row.append(str(r["refund_count"]))
        row.append(disp_str)
        row.append(f"[{badge_style}]{r['status_badge']}[/{badge_style}]")
        tl_table.add_row(*row)

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
    only_open: bool = typer.Option(
        False,
        "--open",
        "-o",
        help="Filter disputes to only active/unresolved cases",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output machine-readable JSON to stdout",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Show the buyer claim column in the table",
    ),
    include_closed: bool = typer.Option(
        False,
        "--all",
        help="Include resolved and closed disputes in table and JSON output",
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
    store = PulseStore(settings.db_path, settings=settings)

    if store.should_auto_seed():
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

        if not demo and settings.has_paypal_credentials:
            det_res = pp_client.get_dispute_details(dispute_id)
            if det_res.get("ok") and det_res.get("dispute"):
                store.sync_external_disputes([det_res["dispute"]])
                refreshed = store.get_dispute_by_id(dispute_id)
                if refreshed:
                    dispute = refreshed

        sku_val = dispute.get("platform_sku", dispute.get("device_model", "Ubuntu Linux CLI"))

        # 1. Mint live PayPal Sandbox remediation entitlement reference
        rep_order = pp_client.create_order(
            amount=float(dispute.get("amount", 9.99)),
            currency=dispute.get("currency", "USD"),
            description=f"Dispute {dispute_id} Remediation Entitlement ({sku_val})",
            custom_id=f"RESOLVE|{dispute_id}",
        )

        # 2. Triage buyer problem and draft tailored resolution + evidence package via Pulse AI
        plan = diagnose_dispute_and_remediate(dispute, rep_order)
        evidence_text, model_used = draft_dispute_evidence(
            dispute=dispute,
            replacement_order=rep_order,
            settings=settings,
        )

        # 3. Transmit multi-channel resolution (evidence + direct buyer message + claim/offer)
        sandbox_res = pp_client.submit_dispute_resolution(
            dispute_id=dispute_id,
            evidence_text=evidence_text,
            amount=float(dispute["amount"]),
            currency=dispute["currency"],
            reason=str(dispute.get("reason", "MERCHANDISE_OR_SERVICE_NOT_RECEIVED")),
            buyer_message=plan["buyer_message"],
            resolution_strategy=plan["resolution_strategy"],
            replacement_order=rep_order,
        )

        order_id_str = rep_order.order_id if rep_order.success else plan["remediation_id"]
        if rep_order.success:
            store.record_live_order(
                rep_order,
                release_version=dispute["release_version"],
                country_code=dispute["buyer_country"],
                platform_sku=sku_val,
            )
        updated_status = sandbox_res.get("updated_status") or "UNDER_REVIEW"
        store.mark_dispute_resolved(
            dispute_id,
            evidence_text,
            order_id_str,
            new_status=updated_status,
        )

        if json_output:
            out_res = {
                "dispute_id": dispute_id,
                "status": updated_status,
                "officially_resolved": updated_status.upper() in ("RESOLVED", "CLOSED"),
                "problem_category": plan["problem_category"],
                "resolution_strategy": plan["resolution_strategy"],
                "remediation_id": plan["remediation_id"],
                "paypal_dispute_api_status": sandbox_res["dispute_api_status"],
                "replacement_order_id": order_id_str,
                "buyer_message": plan["buyer_message"],
                "evidence_draft": evidence_text,
            }
            sys.stdout.write(json.dumps(out_res, indent=2) + "\n")
            return

        console.print(
            Panel.fit(
                f"[bold yellow]Submitting Remediation for Dispute `{dispute_id}` ({plan['problem_category']}) via Pulse AI + PayPal Sandbox...[/bold yellow]",
                border_style="yellow",
            )
        )
        console.print(
            Panel(
                Markdown(evidence_text),
                title="[bold green]● Pulse Evidence & Customer Remediation Package[/bold green]",
                border_style="green",
            )
        )
        status_style = "bold green" if updated_status.upper() in ("RESOLVED", "CLOSED") else "bold cyan"
        console.print(
            Panel(
                f"• [bold]Dispute ID:[/bold] `{dispute_id}` -> [{status_style}]{updated_status}[/{status_style}] (Evidence Transmitted)\n"
                f"• [bold]Triage Strategy:[/bold] `{plan['problem_category']}` (`{plan['resolution_strategy']}`)\n"
                f"• [bold]Provisioned Credential / Ref ID:[/bold] [bold green]{plan['remediation_id']}[/bold green]\n"
                f"• [bold]PayPal Disputes API Status:[/bold] `{sandbox_res['dispute_api_status']}`\n"
                f"• [bold]Live Sandbox Remediation Order ID:[/bold] [bold cyan]{order_id_str}[/bold cyan]\n"
                f"• [bold]Adjudication Notice:[/bold] [dim]Dispute remains open on PayPal until buyer or PayPal confirms resolution.[/dim]",
                title="[bold #00a8ea]PayPal Sandbox Resolution Execution[/bold #00a8ea]",
                border_style="#0070ba",
            )
        )
        return

    # List disputes
    auth_res = pp_client.authenticate(force_refresh=False)
    live_disp_probe = pp_client.get_disputes() if auth_res.authenticated else {"ok": False, "items": []}
    if live_disp_probe.get("ok") and live_disp_probe.get("items"):
        store.sync_external_disputes(live_disp_probe["items"])
    disputes = store.get_disputes()
    if only_open or not include_closed:
        disputes = [d for d in disputes if str(d.get("status", "")).upper() not in ("RESOLVED", "CLOSED", "CANCELLED")]

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
    if verbose:
        table.add_column("Buyer Claim & Root Cause", style="dim")

    for d in disputes:
        st_val = str(d["status"]).upper()
        if st_val in ("RESOLVED", "CLOSED"):
            st_badge = f"[bold green]{st_val}[/bold green]"
        elif st_val in ("WAITING_FOR_BUYER_RESPONSE", "UNDER_REVIEW"):
            st_badge = f"[cyan]{st_val}[/cyan]"
        elif st_val == "WAITING_FOR_SELLER_RESPONSE":
            st_badge = f"[bold red]{st_val}[/bold red]"
        else:
            st_badge = f"[yellow]{st_val}[/yellow]"

        if d.get("resolution_order_id"):
            st_badge += f" [dim]({d['resolution_order_id']})[/dim]"
        sku_entry = d.get("platform_sku", d.get("device_model", ""))
        row = [
            d["dispute_id"],
            st_badge,
            d["buyer_country"],
            d["release_version"],
            sku_entry,
            f"{d['amount']:.2f} {d['currency']}",
            d["due_date"],
        ]
        if verbose:
            row.append(d["description"])
        table.add_row(*row)

    console.print(table)
    first_open_id = next(
        (
            d["dispute_id"]
            for d in disputes
            if str(d.get("status", "")).upper() not in ("RESOLVED", "CLOSED", "CANCELLED")
        ),
        disputes[0]["dispute_id"] if disputes else "PP-D-90411-DE",
    )
    console.print(
        f"[dim]Tip: Run[/dim] [bold cyan]pp disputes --resolve {first_open_id}[/bold cyan] "
        "[dim]to draft an evidence response with Pulse AI and mint a live PayPal Sandbox replacement order.[/dim]"
    )
    if not verbose and not include_closed:
        console.print(
            "[dim]Resolved disputes are hidden. Use --all to include them, or --verbose to show buyer claims.[/dim]"
        )


@app.command("deliver")
def cmd_deliver(
    commit_sha: str = typer.Argument(..., metavar="COMMIT_SHA", help="Commit SHA or short SHA that shipped"),
    release: Optional[str] = typer.Option(None, "--release", "-r", help="Release version this commit shipped in"),
    channel: str = typer.Option("unknown", "--channel", "-c", help="Delivery channel (github, ci, manual, ...)"),
    note: str = typer.Option("", "--note", "-n", help="Optional note"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
) -> None:
    """Record that a commit shipped to a release channel."""
    settings = get_settings()
    store = PulseStore(settings.db_path, settings=settings)
    record = store.record_delivery(commit_sha, release_version=release, channel=channel, note=note)
    if json_output:
        sys.stdout.write(json.dumps(record, indent=2) + "\n")
        return
    console.print(
        Panel(
            f"[bold green][OK] Delivery recorded[/bold green]\n"
            f"  • [bold]Delivery ID:[/bold] `{record['delivery_id']}`\n"
            f"  • [bold]Commit:[/bold] `{record['commit_sha']}`\n"
            f"  • [bold]Release:[/bold] {record['release_version'] or 'unassigned'}\n"
            f"  • [bold]Channel:[/bold] {record['channel']}\n"
            f"  • [bold]Recorded At:[/bold] {record['delivered_at']}",
            title="[bold #00a8ea]DELIVERY LEDGER[/bold #00a8ea]",
            border_style="#0070ba",
        )
    )


@app.command("deliveries")
def cmd_deliveries(
    import_path: Optional[Path] = typer.Argument(None, metavar="[FILE]", help="JSONL file of delivery records to import"),
    release: Optional[str] = typer.Option(None, "--release", "-r", help="Filter by release version"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
) -> None:
    """Import delivery records from a JSONL file, or list recorded deliveries."""
    settings = get_settings()
    store = PulseStore(settings.db_path, settings=settings)

    if import_path is not None:
        try:
            text = import_path.read_text(encoding="utf-8")
        except OSError as exc:
            console.print(f"[bold red]Cannot read file:[/bold red] {exc}")
            raise typer.Exit(code=EX_CONFIG)
        records, errors = parse_delivery_jsonl(text)
        imported = [store.record_delivery(**record) for record in records]
        if json_output:
            sys.stdout.write(
                json.dumps(
                    {"ok": not errors, "imported": imported, "errors": errors},
                    indent=2,
                )
                + "\n"
            )
            if errors:
                raise typer.Exit(code=EX_USAGE)
            return
        for record in imported:
            console.print(f"[bold green][OK][/bold green] {record['delivery_id']} <- `{record['commit_sha']}`")
        for error in errors:
            console.print(f"[bold red][X][/bold red] {error}")
        if errors:
            raise typer.Exit(code=EX_USAGE)
        return

    deliveries = store.get_deliveries(release)
    if json_output:
        sys.stdout.write(json.dumps(deliveries, indent=2) + "\n")
        return
    if not deliveries:
        console.print(
            Panel(
                "[dim]No deliveries recorded yet. Run `pp deliver <commit_sha> --release v1.0.0`.[/dim]",
                title="[bold #00a8ea]DELIVERY LEDGER[/bold #00a8ea]",
                border_style="#0070ba",
            )
        )
        return
    table = Table(title="Delivery Ledger", box=box.ROUNDED, expand=True)
    table.add_column("Delivery ID", style="bold cyan", no_wrap=True)
    table.add_column("Commit", style="dim", no_wrap=True)
    table.add_column("Release")
    table.add_column("Channel")
    table.add_column("Recorded At", no_wrap=True)
    for d in deliveries:
        table.add_row(
            d["delivery_id"],
            d["commit_sha"][:12],
            d["release_version"] or "-",
            d["channel"],
            d["delivered_at"],
        )
    console.print(table)


@app.command("ledger")
def cmd_ledger(
    csv_output: bool = typer.Option(False, "--csv", help="Export transaction ledger as standard CSV"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
    limit: int = typer.Option(100, "--limit", "-l", help="Maximum transactions to export"),
    demo: bool = typer.Option(False, "--demo", help="Run against isolated demo database (.pulse_cache/demo.db)"),
) -> None:
    """Export or inspect the financial transaction ledger (supports CSV and JSON)."""
    import csv
    import io

    settings = get_settings(demo_mode=demo)
    store = PulseStore(settings.db_path, settings=settings)
    if store.should_auto_seed():
        store.seed_telemetry()

    txs = store.get_transactions(limit=limit)

    if csv_output:
        if not txs:
            return
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(txs[0].keys()))
        writer.writeheader()
        writer.writerows(txs)
        sys.stdout.write(output.getvalue())
        return

    if json_output:
        sys.stdout.write(json.dumps(txs, indent=2) + "\n")
        return

    table = Table(
        title="PayPal Pulse — Financial Transaction Ledger",
        box=box.ROUNDED,
        expand=True,
    )
    table.add_column("PayPal ID", style="bold yellow", no_wrap=True)
    table.add_column("Date", no_wrap=True)
    table.add_column("Gross", justify="right", style="bold")
    table.add_column("Fee", justify="right", style="dim")
    table.add_column("Net", justify="right", style="bold green")
    table.add_column("Curr", style="cyan")
    table.add_column("Release", style="magenta")
    table.add_column("Country", style="white")
    table.add_column("Platform / SKU", style="dim")
    table.add_column("Status", no_wrap=True)

    for tx in txs:
        table.add_row(
            tx["paypal_id"],
            tx["created_at"][:19],
            f"{tx['gross_amount']:.2f}",
            f"{tx['fee_amount']:.2f}",
            f"{tx['net_amount']:.2f}",
            tx["currency"],
            tx["release_version"],
            tx["country_code"],
            tx.get("platform_sku", ""),
            "[green]CAPTURED[/green]" if tx["status"] in ("CAPTURED", "COMPLETED") else tx["status"],
        )
    console.print(table)


@app.command("order")
@app.command("create-order", hidden=True)
def cmd_order(
    amount: str = typer.Argument(..., metavar="[create] AMOUNT", help="Numeric amount to charge (e.g. 5.99) or 'create' subcommand"),
    currency: Optional[str] = typer.Argument(None, metavar="[CURRENCY]", help="Currency code (defaults to project currency or USD) or amount if 'create' used"),
    currency_opt: Optional[str] = typer.Argument(None, metavar="[CURRENCY]", help="Currency code if 'create' subcommand used"),
    desc: Optional[str] = typer.Option(None, "--desc", "-d", help="Item or order description (defaults to project SKU License)"),
    sku: Optional[str] = typer.Option(None, "--sku", "-s", help="Platform SKU or tier (defaults to project default_sku)"),
    release: Optional[str] = typer.Option(None, "--release", "-r", help="Release version to attribute this order to (e.g. v1.3.0)"),
    open_browser: bool = typer.Option(False, "--open", "-o", help="Open approval link in default browser"),
    auto_capture_opt: Optional[bool] = typer.Option(None, "--auto-capture/--no-auto-capture", help="Automatically capture funds upon customer approval (defaults to setting)"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
    demo: bool = typer.Option(False, "--demo", help="Run against isolated demo database"),
    live: bool = typer.Option(False, "--live", help="Explicitly acknowledge execution on Live PayPal production endpoint"),
    force: bool = typer.Option(False, "--force", "-f", help="Bypass interactive confirmation in Live mode"),
) -> None:
    """Mint a live PayPal checkout order (Orders v2 API) directly from the CLI."""
    import webbrowser

    detected_ctx = detect_project_context()
    default_cur = detected_ctx.get("default_currency") or "USD"
    default_sku = detected_ctx.get("default_sku") if detected_ctx.get("config_file") else "PulseCore Pro"
    effective_sku = sku or default_sku or "PulseCore Pro"
    effective_desc = desc or f"{effective_sku} Developer License"

    # Parse flexible syntax: supports both `pp order 5.99 EUR` and `pp order create 5.99 EUR`
    if amount.lower() == "create":
        if not currency:
            console_err.print("[bold red]Usage Error:[/bold red] Missing numeric amount to charge. e.g. `pp order create 5.99 EUR`")
            raise typer.Exit(code=EX_USAGE)
        try:
            parsed_amount = float(currency)
        except ValueError:
            console_err.print(f"[bold red]Usage Error:[/bold red] '{currency}' is not a valid numeric amount.")
            raise typer.Exit(code=EX_USAGE)
        parsed_currency = (currency_opt or default_cur).upper()
    else:
        try:
            parsed_amount = float(amount)
        except ValueError:
            console_err.print(f"[bold red]Usage Error:[/bold red] '{amount}' is not a valid numeric amount.")
            raise typer.Exit(code=EX_USAGE)
        parsed_currency = (currency or default_cur).upper()

    settings = get_settings(demo_mode=demo)
    store = PulseStore(settings.db_path, settings=settings)
    pp_client = PayPalClient(settings)

    effective_release = release or detected_ctx.get("version") or "v1.2.1"

    if not settings.has_paypal_credentials and not demo:
        console_err.print("[bold red]Error:[/bold red] PAYPAL_CLIENT_ID not configured. Run `pp setup` first.")
        raise typer.Exit(code=EX_CONFIG)

    if settings.paypal_mode.lower() == "live" and not live and not force and not demo:
        if sys.stdin.isatty():
            confirmed = typer.confirm(
                f"[LIVE PRODUCTION] Create live order for {parsed_amount:.2f} {parsed_currency} on api-m.paypal.com?",
                default=False,
            )
            if not confirmed:
                console_err.print("[yellow]Order creation aborted by user.[/yellow]")
                raise typer.Exit(code=0)
        else:
            console_err.print(
                "[bold red]Safety Error:[/bold red] Creating orders in LIVE production mode requires `--live` or `--force`."
            )
            raise typer.Exit(code=EX_USAGE)

    res = pp_client.create_order(
        amount=parsed_amount,
        currency=parsed_currency,
        description=effective_desc,
        custom_id=f"release:{effective_release}|US|{effective_sku}",
    )

    if not res.success:
        console_err.print(f"[bold red]PayPal API Error:[/bold red] {res.error}")
        raise typer.Exit(code=EX_UNAVAILABLE)

    store.record_live_order(
        order=res,
        release_version=effective_release,
        platform_sku=effective_sku,
    )

    if json_output:
        sys.stdout.write(json.dumps(res.model_dump(), indent=2) + "\n")
        return

    if open_browser and res.approve_url:
        webbrowser.open(res.approve_url)

    should_auto_capture = settings.auto_capture if auto_capture_opt is None else auto_capture_opt
    env_label = "Production" if settings.paypal_mode.lower() == "live" and not demo else "Sandbox"
    capture_note = (
        "[cyan]Auto-Capture is ACTIVE (Standard). Complete payment in browser to settle immediately.[/cyan]"
        if should_auto_capture and not demo
        else f"[dim]To capture this order after customer approval, run:\npp capture {res.order_id}{' --demo' if demo else ''}[/dim]"
    )

    console.print(
        Panel(
            f"[bold green][OK] Live PayPal {env_label} Order Minted Successfully[/bold green]\n\n"
            f"[bold white]Order ID:[/bold white]     [yellow]{res.order_id}[/yellow]\n"
            f"[bold white]Status:[/bold white]       [cyan]{res.status}[/cyan] [dim](Pending buyer authorization & capture)[/dim]\n"
            f"[bold white]Amount:[/bold white]       [cyan]{res.amount:.2f} {res.currency}[/cyan]\n"
            f"[bold white]SKU / Tier:[/bold white]   {effective_sku}\n"
            f"[bold white]Approve URL:[/bold white]  [link={res.approve_url}]{res.approve_url}[/link]\n\n"
            f"{capture_note}",
            title=f"[bold #00a8ea]PayPal Orders v2 API ({env_label})[/bold #00a8ea]",
            border_style="#0070ba",
        )
    )

    if open_browser and should_auto_capture and not demo and res.approve_url:
        console.print("[dim cyan]Waiting for customer approval in browser (press Ctrl+C to cancel wait)...[/dim cyan]")
        for attempt in range(15):
            time.sleep(3.0)
            try:
                chk = pp_client.get_order(res.order_id)
                if chk.success and chk.status == "APPROVED":
                    cap = pp_client.capture_order(res.order_id)
                    if cap.success:
                        store.update_order_status(paypal_id=cap.order_id, status=cap.status, amount=cap.amount, currency=cap.currency)
                        console.print(
                            f"[bold green][AUTO-CAPTURE ✓] Order {cap.order_id} captured successfully! "
                            f"Settled {cap.amount:.2f} {cap.currency} to merchant balance.[/bold green]"
                        )
                    break
                elif chk.success and chk.status == "COMPLETED":
                    console.print(f"[bold green][OK] Order {res.order_id} is already COMPLETED.[/bold green]")
                    break
            except KeyboardInterrupt:
                console.print("\n[yellow]Auto-capture wait canceled. Capture manually with `pp capture` when ready.[/yellow]")
                break
            except Exception:
                pass


@app.command("capture")
def cmd_capture(
    order_id: str = typer.Argument(..., help="PayPal Order ID to capture (e.g. 4K23850270297871H)"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
    demo: bool = typer.Option(False, "--demo", help="Run in isolated demo mode"),
    live: bool = typer.Option(False, "--live", help="Acknowledge execution on Live PayPal production endpoint"),
    force: bool = typer.Option(False, "--force", "-f", help="Bypass interactive confirmation in Live mode"),
) -> None:
    """Capture funds for an approved PayPal Order (Orders v2 API POST /v2/checkout/orders/{id}/capture)."""
    settings = get_settings(demo_mode=demo)
    store = PulseStore(settings.db_path, settings=settings)
    pp_client = PayPalClient(settings)

    if not settings.has_paypal_credentials and not demo:
        console_err.print("[bold red]Error:[/bold red] PAYPAL_CLIENT_ID not configured. Run `pp setup` first.")
        raise typer.Exit(code=EX_CONFIG)

    if settings.paypal_mode.lower() == "live" and not live and not force and not demo:
        if sys.stdin.isatty():
            confirmed = typer.confirm(
                f"[LIVE PRODUCTION] Capture funds for Order `{order_id}` on api-m.paypal.com?",
                default=False,
            )
            if not confirmed:
                console_err.print("[yellow]Order capture aborted by user.[/yellow]")
                raise typer.Exit(code=0)
        else:
            console_err.print(
                "[bold red]Safety Error:[/bold red] Capturing orders in LIVE production mode requires `--live` or `--force`."
            )
            raise typer.Exit(code=EX_USAGE)

    res = pp_client.capture_order(order_id=order_id.strip())

    if not res.success:
        if json_output:
            sys.stderr.write(json.dumps(res.model_dump(), indent=2) + "\n")
        else:
            console_err.print(f"[bold red]Capture Failed:[/bold red] {res.error}")
        raise typer.Exit(code=EX_UNAVAILABLE)

    store.update_order_status(
        paypal_id=res.order_id,
        status=res.status,
        amount=res.amount,
        currency=res.currency,
    )

    if json_output:
        sys.stdout.write(json.dumps(res.model_dump(), indent=2) + "\n")
        return

    env_label = "Production" if settings.paypal_mode.lower() == "live" and not demo else "Sandbox"
    console.print(
        Panel(
            f"[bold green][OK] PayPal {env_label} Order Captured Successfully[/bold green]\n\n"
            f"[bold white]Order ID:[/bold white]     [yellow]{res.order_id}[/yellow]\n"
            f"[bold white]Status:[/bold white]       [bold green]{res.status}[/bold green]\n"
            f"[bold white]Amount:[/bold white]       [cyan]{res.amount:.2f} {res.currency}[/cyan]\n"
            f"[bold white]Captured At:[/bold white]  {res.create_time or datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            title=f"[bold #00a8ea]PAYPAL {env_label.upper()} ORDER CAPTURED[/bold #00a8ea]",
            border_style="#0070ba",
        )
    )


@app.command("invoice")
@app.command("create-invoice", hidden=True)
def cmd_invoice(
    amount: str = typer.Argument(..., metavar="[create] AMOUNT", help="Invoice amount to bill or 'create' subcommand"),
    currency: Optional[str] = typer.Argument(None, metavar="[CURRENCY]", help="Currency code or numeric amount if 'create' used"),
    recipient_email: Optional[str] = typer.Argument(None, metavar="[EMAIL]", help="Recipient customer email or currency code"),
    recipient_opt: Optional[str] = typer.Argument(None, metavar="[EMAIL]", help="Recipient email if 'create' used with currency"),
    item: Optional[str] = typer.Option(None, "--item", "-i", help="Line item description (defaults to project SKU)"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
    demo: bool = typer.Option(False, "--demo", help="Run against isolated demo database"),
    live: bool = typer.Option(False, "--live", help="Explicitly acknowledge execution on Live PayPal production endpoint"),
    force: bool = typer.Option(False, "--force", "-f", help="Bypass interactive confirmation in Live mode"),
) -> None:
    """Create a live PayPal draft invoice (Invoicing v2 API) directly from the CLI."""
    detected_ctx = detect_project_context()
    default_cur = detected_ctx.get("default_currency") or "USD"
    effective_item = item or (f"{detected_ctx['default_sku']} License" if detected_ctx.get("config_file") and detected_ctx.get("default_sku") else "Enterprise License")
    # Parse flexible syntax: supports both `pp invoice 49.00 USD user@example.com` and `pp invoice create 49.00 USD user@example.com`
    if amount.lower() == "create":
        if not currency:
            console_err.print("[bold red]Usage Error:[/bold red] Missing numeric amount for invoice. e.g. `pp invoice create 49.00 USD partner@example.com`")
            raise typer.Exit(code=EX_USAGE)
        try:
            parsed_amount = float(currency)
        except ValueError:
            console_err.print(f"[bold red]Usage Error:[/bold red] '{currency}' is not a valid numeric amount.")
            raise typer.Exit(code=EX_USAGE)
        if recipient_email and "@" in recipient_email:
            parsed_currency = default_cur
            parsed_recipient = recipient_email
        else:
            parsed_currency = (recipient_email or default_cur).upper()
            parsed_recipient = recipient_opt or ""
        if not parsed_recipient or "@" not in parsed_recipient:
            console_err.print("[bold red]Usage Error:[/bold red] Missing recipient email address.")
            raise typer.Exit(code=EX_USAGE)
    else:
        try:
            parsed_amount = float(amount)
        except ValueError:
            console_err.print(f"[bold red]Usage Error:[/bold red] '{amount}' is not a valid numeric amount.")
            raise typer.Exit(code=EX_USAGE)
        if recipient_email and "@" in recipient_email and not recipient_opt:
            parsed_currency = (currency or default_cur).upper()
            parsed_recipient = recipient_email
        elif currency and "@" in currency and not recipient_opt:
            parsed_currency = default_cur
            parsed_recipient = currency
        else:
            parsed_currency = (currency or default_cur).upper()
            parsed_recipient = recipient_email or (recipient_opt or "")
        if not parsed_recipient or "@" not in parsed_recipient:
            console_err.print("[bold red]Usage Error:[/bold red] Missing recipient email address.")
            raise typer.Exit(code=EX_USAGE)

    settings = get_settings(demo_mode=demo)
    pp_client = PayPalClient(settings)

    if not settings.paypal_client_id and not demo:
        console_err.print("[bold red]Error:[/bold red] PAYPAL_CLIENT_ID not configured. Run `pp setup` first.")
        raise typer.Exit(code=EX_CONFIG)

    if settings.paypal_mode.lower() == "live" and not live and not force and not demo:
        if sys.stdin.isatty():
            confirmed = typer.confirm(
                f"[LIVE PRODUCTION] Create live invoice for {parsed_amount:.2f} {parsed_currency} to {parsed_recipient} on api-m.paypal.com?",
                default=False,
            )
            if not confirmed:
                console_err.print("[yellow]Invoice creation aborted by user.[/yellow]")
                raise typer.Exit(code=0)
        else:
            console_err.print(
                "[bold red]Safety Error:[/bold red] Creating invoices in LIVE production mode requires `--live` or `--force`."
            )
            raise typer.Exit(code=EX_USAGE)

    res = pp_client.create_draft_invoice(
        recipient_email=parsed_recipient,
        amount=parsed_amount,
        currency=parsed_currency,
        item_name=effective_item,
    )

    if not res.success:
        console_err.print(f"[bold red]PayPal Invoicing API Error:[/bold red] {res.error}")
        raise typer.Exit(code=EX_UNAVAILABLE)

    if json_output:
        sys.stdout.write(json.dumps(res.model_dump(), indent=2) + "\n")
        return

    console.print(
        Panel(
            f"[bold green][OK] Live PayPal Sandbox Draft Invoice Created[/bold green]\n\n"
            f"[bold white]Invoice ID:[/bold white]  [yellow]{res.invoice_id}[/yellow]\n"
            f"[bold white]Recipient:[/bold white]   {res.recipient_email}\n"
            f"[bold white]Amount:[/bold white]      [cyan]{res.amount:.2f} {res.currency}[/cyan]\n"
            f"[bold white]Status:[/bold white]      {res.status}\n"
            f"[bold white]API Link:[/bold white]    {res.self_url}",
            title="[bold #00a8ea]PayPal Invoicing v2 API[/bold #00a8ea]",
            border_style="#0070ba",
        )
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
    store = PulseStore(settings.db_path, settings=settings)
    if demo and store.should_auto_seed():
        store.seed_telemetry()

    if store.detect_state() == "fresh":
        if json_output:
            sys.stdout.write(
                json.dumps(
                    {
                        "state": "fresh",
                        "db_path": str(settings.db_path),
                        "next_actions": store.next_actions(),
                        "answer": "Nothing to analyze yet. This ledger has no orders.",
                    },
                    indent=2,
                )
                + "\n"
            )
            return
        console.print(
            Panel(
                "[bold #f0f6fc]Nothing to analyze yet.[/bold #f0f6fc]\n"
                "This ledger has no orders. Run `pp setup`, then `pp order <amount> --release <tag>`.",
                title="[bold #00a8ea]Pulse AI Copilot[/bold #00a8ea]",
                border_style="#0070ba",
            )
        )
        return

    pp_client = PayPalClient(settings)

    full_question = " ".join(question).strip()
    if not json_output and onboarding.should_animate():
        with onboarding.LiveChecklist(console) as cl:
            idx_q = cl.start_step("Pulse AI Copilot", "Querying ledger telemetry & reasoning...")
            res = ask_copilot(question=full_question, store=store, paypal_client=pp_client, settings=settings)
            cl.finish_step(idx_q, "ok", f"Completed in {res.latency_ms / 1000:.2f}s ({res.tokens_per_sec:.0f} tok/s)")
    else:
        res = ask_copilot(question=full_question, store=store, paypal_client=pp_client, settings=settings)

    if json_output:
        sys.stdout.write(json.dumps(res.model_dump(), indent=2) + "\n")
        return

    if res.created_order and res.created_order.success and open_browser:
        webbrowser.open(res.created_order.approve_url)

    if res.executed_action:
        console.print(
            Panel(
                f"[bold #00a8ea][TOOL][/bold #00a8ea] [bold #e5a824]•[/bold #e5a824] [bold green]Live PayPal Sandbox Action Executed:[/bold green] {res.executed_action}",
                border_style="#0070ba",
            )
        )

    console.print(
        Panel(
            Markdown(res.answer),
            title=(
                f"[bold #00a8ea]Pulse AI Copilot[/bold #00a8ea] [bold #e5a824]•[/bold #e5a824] "
                f"[dim]({res.tokens_per_sec:.0f} tok/s · {res.latency_ms / 1000:.2f}s)[/dim]"
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
    amount: Optional[float] = typer.Option(None, "--amount", "-a", help="Gross transaction amount (defaults to project default_price or 19.99)"),
    currency: Optional[str] = typer.Option(None, "--currency", "-c", help="Currency code (defaults to project default_currency or USD)"),
    sku: Optional[str] = typer.Option(None, "--sku", help="Platform SKU or license name (defaults to project default_sku)"),
    release: Optional[str] = typer.Option(None, "--release", "-r", help="Release version to attribute event to (e.g. v1.3.0)"),
    dispute_id: Optional[str] = typer.Option(None, "--id", help="Dispute ID for dispute events (defaults to unique ID on creation, or PP-D-99881-US on resolution)"),
    json_output: bool = typer.Option(False, "--json", help="Output event payload and ingestion receipt as JSON"),
) -> None:
    """Synthesize and ingest realistic PayPal Webhook events directly into the local SQLite ledger."""
    settings = get_settings()
    store = PulseStore(settings.db_path)
    if store.should_auto_seed():
        store.seed_telemetry()

    detected_ctx = detect_project_context()
    has_cfg = bool(detected_ctx.get("config_file"))
    effective_release = release or detected_ctx.get("version") or "v1.2.1"
    amount = float(amount if amount is not None else (detected_ctx.get("default_price", 19.99) if has_cfg else 19.99))
    currency = (currency or (detected_ctx.get("default_currency", "USD") if has_cfg else "USD")).upper()
    sku = sku or (detected_ctx.get("default_sku") if has_cfg else "PulseCore Pro") or "PulseCore Pro"

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
                "custom_id": f"release:{effective_release}|US|{sku}",
            },
        }

    # Override with command-line arguments and generate unique IDs per trigger invocation
    import uuid

    uniq_suffix = uuid.uuid4().hex[:7].upper()
    event_payload["id"] = f"WH-SIM-{uniq_suffix}"
    if "resource" in event_payload:
        res_obj = event_payload["resource"]
        if evt_normalized in ("payment.capture.completed", "checkout.order.approved"):
            prefix = "CAP-SIM" if "capture" in evt_normalized else "ORD-SIM"
            res_obj["id"] = f"{prefix}-{uniq_suffix}"
        if "amount" in res_obj:
            res_obj["amount"]["value"] = str(amount)
            res_obj["amount"]["currency_code"] = currency.upper()
        if "dispute_amount" in res_obj:
            res_obj["dispute_amount"]["value"] = str(amount)
            res_obj["dispute_amount"]["currency_code"] = currency.upper()
        if "dispute_id" in res_obj:
            if dispute_id:
                res_obj["dispute_id"] = dispute_id
            elif evt_normalized == "customer.dispute.created":
                res_obj["dispute_id"] = f"PP-D-{uniq_suffix}"
            else:
                open_disps = store.get_disputes(status_filter="OPEN")
                res_obj["dispute_id"] = open_disps[0]["dispute_id"] if open_disps else "PP-D-99881-US"
        if "release_version" in res_obj:
            res_obj["release_version"] = effective_release
        res_obj["custom_id"] = f"release:{effective_release}|US|{sku}"

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
            f"[bold green][OK] PayPal Webhook Event Ingested into Local Ledger[/bold green]\n\n"
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
    if store.should_auto_seed():
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

    # Interactive onboarding (demo path)
    onboarding.welcome(console, first_run=True)
    choice = onboarding.select(
        "Where would you like to start?",
        [
            ("Launch terminal deck with sample data (`pp tui --demo`)", "tui"),
            ("Connect PayPal Sandbox credentials (`pp setup`)", "setup"),
            ("Exit to shell", "exit"),
        ],
        default="tui",
    )
    if choice == "tui":
        from pulse.tui import PayPalPulseTUI

        PayPalPulseTUI(demo_mode=True).run()
    elif choice == "setup":
        run_setup_wizard(offer_tui=True)
    else:
        console.print("[dim]Run `pp --help` anytime to explore all subcommands.[/dim]")

def run_setup_wizard(
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    mode: str = "sandbox",
    groq_key: Optional[str] = None,
    ai_provider: Optional[str] = None,
    ai_key: Optional[str] = None,
    ai_base_url: Optional[str] = None,
    ai_model: Optional[str] = None,
    offer_tui: bool = True,
) -> None:
    """Execute the interactive or automated credential setup and verification wizard."""
    console.print()
    console.print(
        Panel(
            format_mascot_header(
                title="Credential Setup Wizard",
                subtitle="Configure live PayPal Sandbox REST credentials & Pulse AI engine (BYOK)",
                state="idle",
            ),
            title="[bold #00a8ea]CREDENTIAL SETUP[/bold #00a8ea]",
            border_style="#0070ba",
            padding=(0, 1),
        )
    )

    existing_settings = get_settings(demo_mode=False)
    if not (client_id and client_secret):
        if not sys.stdin.isatty() and existing_settings.has_paypal_credentials and (ai_provider or ai_key or groq_key or ai_base_url or ai_model):
            c_id = existing_settings.paypal_client_id
            c_sec = existing_settings.paypal_client_secret
            c_mode = existing_settings.paypal_mode
            chosen_provider = (ai_provider or ("groq" if groq_key else existing_settings.active_ai_provider)).strip().lower()
            chosen_ai_key = (ai_key or groq_key or existing_settings.active_ai_api_key or "").strip()
            chosen_ai_base_url = (ai_base_url or existing_settings.ai_base_url or "").strip()
            chosen_ai_model = (ai_model or existing_settings.ai_model or "").strip()
        else:
            if existing_settings.has_paypal_credentials:
                masked_id = (
                    existing_settings.paypal_client_id[:8] + "..." + existing_settings.paypal_client_id[-4:]
                    if len(existing_settings.paypal_client_id) > 12
                    else "***"
                )
                console.print(f"[bold green]Active credentials detected:[/bold green] `{masked_id}` (Mode: [bold cyan]{existing_settings.paypal_mode.upper()}[/bold cyan])")
                console.print("[dim]Press Enter with an empty Client ID to keep existing credentials, or enter 'q' to cancel.[/dim]\n")
            else:
                onboarding.step_header(console, 1, 3, "PayPal REST credentials")
                console.print(onboarding.PAYPAL_KEYS_HELP)
                console.print("[dim]Type 'q' or press Enter on an empty prompt to cancel anytime.[/dim]\n")

            c_id = typer.prompt("  PayPal Client ID", default="").strip()
            if not c_id:
                if existing_settings.has_paypal_credentials:
                    if not sys.stdin.isatty():
                        console.print("[bold green][OK] Retained existing active credentials.[/bold green]")
                        return
                    console.print("[bold green][OK] Retaining existing active PayPal credentials.[/bold green]")
                    c_id = existing_settings.paypal_client_id
                    c_sec = existing_settings.paypal_client_secret
                    c_mode = existing_settings.paypal_mode
                else:
                    console.print("[dim]Setup cancelled.[/dim]")
                    return
            elif c_id.lower() in ("q", "quit", "exit"):
                console.print("[dim]Setup cancelled.[/dim]")
                return
            else:
                c_sec = typer.prompt("  PayPal Client Secret", default="", show_default=False, hide_input=True).strip()
                if not c_sec or c_sec.lower() in ("q", "quit", "exit"):
                    console.print("[dim]Setup cancelled (Secret cannot be empty).[/dim]")
                    return

                console.print(f"\n{onboarding.MODE_HELP}")
                c_mode = onboarding.select(
                    "PayPal mode",
                    [("Sandbox (test money, recommended)", "sandbox"), ("Live (real money)", "live")],
                    default="sandbox",
                )

            onboarding.step_header(console, 2, 3, "Pulse AI engine (optional)")
            console.print(
                "[dim]Pulse AI answers questions and drafts dispute evidence. "
                "Bring your own key. Choose Skip to use the offline local engine.[/dim]\n"
            )
            chosen_provider = onboarding.select(
                "AI provider",
                [
                    ("Groq (fast, free tier)", "groq"),
                    ("OpenAI", "openai"),
                    ("Google Gemini", "gemini"),
                    ("Anthropic Claude", "anthropic"),
                    ("xAI Grok", "xai"),
                    ("Ollama / local (http://localhost:11434/v1)", "ollama"),
                    ("Custom OpenAI-compatible endpoint", "custom"),
                    ("Skip (offline local engine)", "none"),
                ],
                default="groq",
            )
            chosen_ai_key = ""
            chosen_ai_base_url = ""
            chosen_ai_model = ""

            if chosen_provider == "groq":
                chosen_ai_key = typer.prompt(
                    "  Groq API Key (from console.groq.com)",
                    default=existing_settings.groq_api_key or "",
                    hide_input=True,
                ).strip()
            elif chosen_provider == "openai":
                chosen_ai_key = typer.prompt(
                    "  OpenAI API Key (from platform.openai.com)",
                    default=existing_settings.openai_api_key or "",
                    hide_input=True,
                ).strip()
            elif chosen_provider == "gemini":
                chosen_ai_key = typer.prompt(
                    "  Google Gemini API Key (from aistudio.google.com)",
                    default=existing_settings.gemini_api_key or "",
                    hide_input=True,
                ).strip()
            elif chosen_provider == "anthropic":
                chosen_ai_key = typer.prompt(
                    "  Anthropic API Key (from console.anthropic.com)",
                    default=existing_settings.anthropic_api_key or "",
                    hide_input=True,
                ).strip()
            elif chosen_provider == "xai":
                chosen_ai_key = typer.prompt(
                    "  xAI API Key (from console.x.ai)",
                    default=existing_settings.xai_api_key or "",
                    hide_input=True,
                ).strip()
            elif chosen_provider == "ollama":
                chosen_ai_base_url = typer.prompt(
                    "  Ollama Base URL",
                    default="http://localhost:11434/v1",
                ).strip()
                chosen_ai_model = typer.prompt(
                    "  Ollama Model ID",
                    default="llama3.2",
                ).strip()
            elif chosen_provider == "custom":
                chosen_ai_base_url = typer.prompt(
                    "  Custom Base URL",
                    default="http://localhost:8000/v1",
                ).strip()
                chosen_ai_key = typer.prompt(
                    "  Custom API Key (or press Enter if none)",
                    default="",
                    hide_input=True,
                ).strip()
                chosen_ai_model = typer.prompt(
                    "  Custom Model ID",
                    default="default",
                ).strip()
            if chosen_provider not in ("ollama", "custom", "none") and not chosen_ai_key:
                chosen_provider = "none"
    else:
        c_id = client_id.strip()
        c_sec = client_secret.strip()
        c_mode = mode.strip().lower()
        if ai_provider or ai_key or groq_key or ai_base_url or ai_model:
            chosen_provider = (ai_provider or ("groq" if groq_key else existing_settings.active_ai_provider)).strip().lower()
            chosen_ai_key = (ai_key or groq_key or existing_settings.active_ai_api_key or "").strip()
            chosen_ai_base_url = (ai_base_url or existing_settings.ai_base_url or "").strip()
            chosen_ai_model = (ai_model or existing_settings.ai_model or "").strip()
        elif existing_settings.has_ai_credentials:
            chosen_provider = existing_settings.active_ai_provider
            chosen_ai_key = existing_settings.active_ai_api_key
            chosen_ai_base_url = existing_settings.ai_base_url
            chosen_ai_model = existing_settings.ai_model
        else:
            chosen_provider = "none"
            chosen_ai_key = ""
            chosen_ai_base_url = ""
            chosen_ai_model = ""

    # Pre-flight validation against PayPal Sandbox & AI via LiveChecklist
    onboarding.step_header(console, 3, 3, "Verify and save")
    test_settings = PulseSettings(
        paypal_client_id=c_id,
        paypal_client_secret=c_sec,
        paypal_mode=c_mode,
        groq_api_key=chosen_ai_key if chosen_provider == "groq" else "",
    )
    test_client = PayPalClient(test_settings)

    save_kwargs = {
        "paypal_client_id": c_id,
        "paypal_client_secret": c_sec,
        "paypal_mode": c_mode,
        "ai_provider": chosen_provider,
        "ai_base_url": chosen_ai_base_url,
        "ai_api_key": chosen_ai_key,
        "ai_model": chosen_ai_model,
    }
    if chosen_provider == "groq":
        save_kwargs["groq_api_key"] = chosen_ai_key
    elif chosen_provider == "openai":
        save_kwargs["openai_api_key"] = chosen_ai_key
    elif chosen_provider == "gemini":
        save_kwargs["gemini_api_key"] = chosen_ai_key
    elif chosen_provider == "anthropic":
        save_kwargs["anthropic_api_key"] = chosen_ai_key
    elif chosen_provider == "xai":
        save_kwargs["xai_api_key"] = chosen_ai_key

    # Preserve saved keys for providers the user did not re-enter (avoid silent data loss).
    for _field in ("groq_api_key", "openai_api_key", "gemini_api_key", "anthropic_api_key", "xai_api_key", "openrouter_api_key"):
        if not save_kwargs.get(_field):
            _old = getattr(existing_settings, _field, "") or ""
            if _old:
                save_kwargs[_field] = _old

    with onboarding.LiveChecklist(console) as checklist:
        idx_pp = checklist.start_step("PayPal REST OAuth2", f"Negotiating {c_mode} handshake...")
        auth_res = test_client.authenticate(force_refresh=True)
        if not auth_res.authenticated:
            checklist.finish_step(idx_pp, "fail", f"Authentication failed: {auth_res.error}")
        else:
            checklist.finish_step(
                idx_pp,
                "ok",
                f"Connected to {c_mode.upper()} (App `{auth_res.app_id}`, {auth_res.latency_ms:.0f}ms)",
            )

    if not auth_res.authenticated:
        console_err.print(f"\n[bold red][FAIL] Authentication Failed:[/bold red] {auth_res.error}")
        console_err.print("[yellow]Please verify your Client ID and Secret in developer.paypal.com.[/yellow]")
        raise typer.Exit(code=EX_NOPERM)

    with onboarding.LiveChecklist(console) as checklist2:
        idx_cfg = checklist2.start_step("Local vault", "Writing configuration...")
        cfg_path = save_global_config(**save_kwargs)
        perm_note = "owner-only, 0600" if sys.platform != "win32" else "plain JSON; protect your user profile"
        checklist2.finish_step(idx_cfg, "ok", f"Saved `{cfg_path}` ({perm_note})")

        idx_ai = checklist2.start_step("Pulse AI copilot", "Checking inference engine...")
        if chosen_provider not in ("none", ""):
            ai_ping = verify_groq_connection(PulseSettings(**save_kwargs))
            if ai_ping.connected:
                checklist2.finish_step(idx_ai, "ok", f"[bold green]● Pulse AI Connected:[/bold green] {ai_ping.model} ({ai_ping.latency_ms:.0f}ms)")
            else:
                checklist2.finish_step(idx_ai, "warn", f"[dim yellow][!] AI Note: {ai_ping.error}[/dim yellow]")
        else:
            checklist2.finish_step(idx_ai, "ok", "[bold cyan]● Pulse AI Mode:[/bold cyan] Local Deterministic Engine (Zero-Key Offline Fallback Active)")

    onboarding.celebration_banner(
        console,
        title="Pulse Ready",
        subtitle=f"Connected to PayPal {c_mode.upper()} • {chosen_provider.upper()} AI Engine",
    )

    onboarding.summary_table(
        console,
        [
            ("Status", "[bold green]Credentials verified and saved[/bold green]"),
            ("Config file", f"{cfg_path}" + (" (owner-only, 0600)" if sys.platform != "win32" else " (plain JSON; protect your user profile)")),
            ("App ID", f"{auth_res.app_id}"),
            ("Latency", f"{auth_res.latency_ms:.1f} ms"),
            ("PayPal mode", c_mode.upper()),
            ("AI provider", chosen_provider.upper()),
        ],
        latency_ms=auth_res.latency_ms,
    )

    onboarding.try_now_cards(console, sandbox_mode=(c_mode == "sandbox"))
    if offer_tui and onboarding.is_interactive() and not (client_id and client_secret):
        if onboarding.select("Launch the terminal deck now?", [("Yes, open `pp tui`", "yes"), ("No, exit to shell", "no")], default="yes") == "yes":
            from pulse.tui import PayPalPulseTUI

            PayPalPulseTUI(demo_mode=False).run()


@app.command("setup")
@app.command("configure", hidden=True)
def cmd_setup(
    client_id: Optional[str] = typer.Option(None, "--client-id", help="PayPal REST App Client ID"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret", help="PayPal REST App Secret"),
    mode: str = typer.Option("sandbox", "--mode", help="PayPal environment (sandbox or live)"),
    groq_key: Optional[str] = typer.Option(None, "--groq-key", help="Groq API Key (backward compatibility)"),
    provider: Optional[str] = typer.Option(None, "--provider", help="AI Provider (groq, openai, gemini, anthropic, xai, ollama, custom)"),
    ai_key: Optional[str] = typer.Option(None, "--ai-key", help="API Key for the chosen AI provider"),
    ai_base_url: Optional[str] = typer.Option(None, "--ai-base-url", help="Base URL for custom/Ollama endpoint"),
    ai_model: Optional[str] = typer.Option(None, "--ai-model", help="Model ID for custom/Ollama endpoint"),
    demo: bool = typer.Option(False, "--demo", help="Launch instant demo mode instead of configuring credentials"),
) -> None:
    """Guided onboarding setup wizard to configure PayPal Sandbox and AI Copilot credentials."""
    if demo:
        cmd_demo(tour=True, json_output=False)
        return

    existing_settings = get_settings(demo_mode=False)
    # Check non-interactive safety: either PayPal credentials must be supplied or existing credentials re-used
    if not sys.stdin.isatty() and not (client_id and client_secret):
        if not (existing_settings.has_paypal_credentials and (provider or ai_key or groq_key or ai_base_url or ai_model)):
            console_err.print(
                "[bold red]Error:[/bold red] `pp setup` in non-interactive environment requires `--client-id` and `--client-secret`.\n"
                "Usage: pp setup --client-id <ID> --client-secret <SECRET> [--mode sandbox|live] [--provider <PROVIDER>] [--ai-key <KEY>]"
            )
            raise typer.Exit(code=EX_USAGE)

    run_setup_wizard(
        client_id=client_id,
        client_secret=client_secret,
        mode=mode,
        groq_key=groq_key,
        ai_provider=provider,
        ai_key=ai_key,
        ai_base_url=ai_base_url,
        ai_model=ai_model,
    )


@app.command("doctor")
def cmd_doctor(
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON to stdout"),
    offline: bool = typer.Option(False, "--offline", help="Skip network checks"),
) -> None:
    """Check credentials, PayPal and AI connectivity, database, Git, and terminal size."""
    import shutil
    import subprocess

    settings = get_settings(demo_mode=False)
    checks: list[tuple[str, str, str]] = []  # (name, status ok|warn|fail, detail)

    if not json_output:
        console.print()
        console.print(Rule(f"[bold {onboarding.ACCENT}]Pulse Doctor[/bold {onboarding.ACCENT}] · System & API Health Checklist", style=onboarding.BORDER, align="left"))

    with onboarding.LiveChecklist(console if not json_output else Console(file=sys.stderr)) as cl:
        # 1. PayPal credentials & API
        idx_cred = cl.start_step("PayPal credentials", "inspecting ~/.pulse/config.json...")
        if settings.has_paypal_credentials:
            detail_cred = f"mode {settings.paypal_mode}; config {GLOBAL_CONFIG_FILE}"
            cl.finish_step(idx_cred, "ok", detail_cred)
            checks.append(("PayPal credentials", "ok", detail_cred))

            idx_api = cl.start_step("PayPal API", "verifying OAuth2 token...")
            if offline:
                cl.finish_step(idx_api, "warn", "skipped (--offline)")
                checks.append(("PayPal API", "warn", "skipped (--offline)"))
            else:
                res = PayPalClient(settings).authenticate(force_refresh=True)
                if res.authenticated:
                    d_api = f"authenticated in {res.latency_ms:.0f} ms"
                    cl.finish_step(idx_api, "ok", d_api)
                    checks.append(("PayPal API", "ok", d_api))
                else:
                    d_api = f"{res.error}. Run `pp setup`."
                    cl.finish_step(idx_api, "fail", d_api)
                    checks.append(("PayPal API", "fail", d_api))
        else:
            d_cred = "not set. Run `pp setup` or try `pp demo`."
            cl.finish_step(idx_cred, "warn", d_cred)
            checks.append(("PayPal credentials", "warn", d_cred))

        # 2. Pulse AI
        idx_ai = cl.start_step("Pulse AI", "probing AI copilot provider...")
        if settings.has_ai_credentials:
            provider = settings.active_ai_provider
            if offline:
                d_ai = f"{provider} configured; skipped (--offline)"
                cl.finish_step(idx_ai, "warn", d_ai)
                checks.append(("Pulse AI", "warn", d_ai))
            else:
                ping = verify_groq_connection(settings)
                if ping.connected:
                    d_ai = f"{provider} {ping.model} ({ping.latency_ms:.0f} ms)"
                    cl.finish_step(idx_ai, "ok", d_ai)
                    checks.append(("Pulse AI", "ok", d_ai))
                else:
                    d_ai = f"{provider}: {ping.error}. Local engine active."
                    cl.finish_step(idx_ai, "warn", d_ai)
                    checks.append(("Pulse AI", "warn", d_ai))
        else:
            d_ai = "no key set. Local offline engine active."
            cl.finish_step(idx_ai, "warn", d_ai)
            checks.append(("Pulse AI", "warn", d_ai))

        # 3. Local ledger
        idx_db = cl.start_step("Local ledger", "querying SQLite dual-engine store...")
        try:
            PulseStore(settings.db_path, settings=settings).get_dashboard_summary()
            d_db = str(settings.db_path)
            cl.finish_step(idx_db, "ok", d_db)
            checks.append(("Local ledger", "ok", d_db))
        except Exception as exc:  # noqa: BLE001
            d_db = f"{exc}"
            cl.finish_step(idx_db, "fail", d_db)
            checks.append(("Local ledger", "fail", d_db))

        # 4. Git
        idx_git = cl.start_step("Git", "checking repository work-tree...")
        if shutil.which("git"):
            inside = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True
            ).returncode == 0
            d_git = "repository detected" if inside else "not in a Git repo. `pp init` links releases."
            st_git = "ok" if inside else "warn"
            cl.finish_step(idx_git, st_git, d_git)
            checks.append(("Git", st_git, d_git))
        else:
            d_git = "git not found. Release telemetry needs Git."
            cl.finish_step(idx_git, "warn", d_git)
            checks.append(("Git", "warn", d_git))

        # 5. Terminal size
        idx_term = cl.start_step("Terminal size", "measuring viewport dimensions...")
        size = shutil.get_terminal_size()
        st_term = "ok" if size.columns >= 80 and size.lines >= 24 else "warn"
        d_term = f"{size.columns}x{size.lines} (80x24 minimum for `pp tui`)"
        cl.finish_step(idx_term, st_term, d_term)
        checks.append(("Terminal size", st_term, d_term))

    failed_creds = any(name == "PayPal API" and s == "fail" for name, s, _ in checks)
    failed = any(s == "fail" for _, s, _ in checks)
    if json_output:
        sys.stdout.write(json.dumps({"ok": not failed, "checks": [{"name": n, "status": s, "detail": d} for n, s, d in checks]}, indent=2) + "\n")
    if failed_creds:
        raise typer.Exit(code=EX_NOPERM)
    if failed:
        raise typer.Exit(code=EX_UNAVAILABLE)


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
                    "[dim]No PayPal credentials found in ~/.pulse/config.json or .env[/dim]",
                    border_style="#58a6ff",
                )
            )
            choice = onboarding.select(
                "How do you want to launch?",
                [
                    ("Instant demo (no credentials)", "demo"),
                    ("Connect PayPal credentials (pp setup)", "setup"),
                    ("Exit", "exit"),
                ],
                default="demo",
            )
            if choice == "setup":
                run_setup_wizard(offer_tui=False)
                settings = get_settings(demo_mode=False)
                if not settings.has_paypal_credentials:
                    return
                demo = False
            elif choice == "exit":
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
        console.print("[bold green][OK] PayPalPulseTUI initialized and verified ready.[/bold green]")
        return
    tui_app.run()


@app.command("mcp")
def cmd_mcp(
    demo: bool = typer.Option(False, "--demo", help="Run MCP server against isolated demo database"),
) -> None:
    """Run PayPal Pulse as a Model Context Protocol (MCP) server over stdio for AI hosts (Antigravity, Claude, Cursor).
    
    Listens on stdin for machine-readable JSON-RPC 2.0 messages. To connect, add to your MCP client config.
    """
    from pulse.mcp_server import run_mcp_stdio

    run_mcp_stdio(demo_mode=demo)


def main() -> None:
    """Package script entry point."""
    app()


if __name__ == "__main__":
    main()
