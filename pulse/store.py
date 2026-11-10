"""Dual-Engine SQLite Storage (.pulse_cache/pulse.db) for PayPal Pulse."""

from __future__ import annotations

import random
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pulse.config import PACKAGE_ROOT, get_settings
from pulse.paypal_client import PayPalInvoiceResult, PayPalOrderResult

FX_TO_USD = {
    "USD": 1.0,
    "EUR": 1.09,
    "PHP": 0.0175,
    "GBP": 1.28,
}


def compute_paypal_fee(amount: float, currency: str, cross_border: bool = True) -> float:
    """Calculate realistic PayPal micro-merchant fee (percentage + fixed fee + cross-border FX drag)."""
    cur = currency.upper()
    rate = 0.0349 + (0.015 if cross_border else 0.0)
    fixed_map = {
        "USD": 0.49,
        "EUR": 0.39,
        "PHP": 25.00,
        "GBP": 0.39,
    }
    fixed = fixed_map.get(cur, 0.49)
    fee = round(amount * rate + fixed, 2)
    return min(fee, round(amount * 0.45, 2))


class PulseStore:
    """SQLite telemetry & dual-engine ledger store."""

    def __init__(self, db_path: Path | None = None) -> None:
        settings = get_settings()
        self.db_path = db_path or settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=15000;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS releases (
                    version TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    released_at TEXT NOT NULL,
                    git_commit TEXT NOT NULL,
                    installs INTEGER NOT NULL,
                    active_users INTEGER NOT NULL,
                    trial_starts INTEGER NOT NULL,
                    paid_conversions INTEGER NOT NULL,
                    conversion_rate REAL NOT NULL,
                    gross_usd REAL NOT NULL,
                    fees_usd REAL NOT NULL,
                    net_usd REAL NOT NULL,
                    refund_count INTEGER NOT NULL,
                    dispute_count INTEGER NOT NULL,
                    status_badge TEXT NOT NULL,
                    ai_diagnostic TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paypal_id TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL,
                    gross_amount REAL NOT NULL,
                    fee_amount REAL NOT NULL,
                    net_amount REAL NOT NULL,
                    currency TEXT NOT NULL,
                    usd_gross REAL NOT NULL,
                    usd_fee REAL NOT NULL,
                    usd_net REAL NOT NULL,
                    release_version TEXT NOT NULL,
                    country_code TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    platform_sku TEXT NOT NULL,
                    status TEXT NOT NULL,
                    is_live_sandbox INTEGER NOT NULL DEFAULT 0,
                    approve_url TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS disputes (
                    dispute_id TEXT PRIMARY KEY,
                    paypal_transaction_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    buyer_country TEXT NOT NULL,
                    buyer_email TEXT NOT NULL,
                    platform_sku TEXT NOT NULL,
                    release_version TEXT NOT NULL,
                    amount REAL NOT NULL,
                    currency TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    ai_evidence_draft TEXT NOT NULL DEFAULT '',
                    resolution_order_id TEXT NOT NULL DEFAULT '',
                    resolved_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS daily_metrics (
                    day_date TEXT PRIMARY KEY,
                    installs INTEGER NOT NULL,
                    paid_orders INTEGER NOT NULL,
                    gross_usd REAL NOT NULL,
                    fees_usd REAL NOT NULL,
                    net_usd REAL NOT NULL,
                    disputes INTEGER NOT NULL,
                    release_version TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # Automatic schema migration for existing legacy databases
            try:
                txn_cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
                if "device_model" in txn_cols:
                    if "platform_sku" not in txn_cols:
                        conn.execute("ALTER TABLE transactions RENAME COLUMN device_model TO platform_sku")
                    else:
                        conn.execute("ALTER TABLE transactions DROP COLUMN device_model")

                disp_cols = {row["name"] for row in conn.execute("PRAGMA table_info(disputes)").fetchall()}
                if "device_model" in disp_cols:
                    if "platform_sku" not in disp_cols:
                        conn.execute("ALTER TABLE disputes RENAME COLUMN device_model TO platform_sku")
                    else:
                        conn.execute("ALTER TABLE disputes DROP COLUMN device_model")
            except Exception:
                pass

            conn.commit()

    def is_seeded(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM releases").fetchone()
            return bool(row and row["cnt"] > 0)

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def record_live_order(
        self,
        order: PayPalOrderResult,
        release_version: str = "v1.2.1",
        country_code: str = "US",
        platform_sku: str = "Ubuntu Linux 24.04 (CLI)",
        device_model: str | None = None,
    ) -> dict[str, Any]:
        """Insert a freshly created live PayPal Sandbox order into the instant local ledger."""
        cur = order.currency.upper()
        effective_sku = device_model or platform_sku
        fx = FX_TO_USD.get(cur, 1.0)
        cross_border = country_code.upper() != "US" or cur != "USD"
        fee = compute_paypal_fee(order.amount, cur, cross_border=cross_border)
        net = round(order.amount - fee, 2)
        usd_gross = round(order.amount * fx, 2)
        usd_fee = round(fee * fx, 2)
        usd_net = round(net * fx, 2)
        created_at = order.create_time or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM transactions WHERE paypal_id = ?",
                (order.order_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO transactions (
                    paypal_id, created_at, gross_amount, fee_amount, net_amount,
                    currency, usd_gross, usd_fee, usd_net, release_version,
                    country_code, product_name, platform_sku, status,
                    is_live_sandbox, approve_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(paypal_id) DO UPDATE SET
                    status = excluded.status,
                    approve_url = excluded.approve_url
                """,
                (
                    order.order_id,
                    created_at,
                    order.amount,
                    fee,
                    net,
                    cur,
                    usd_gross,
                    usd_fee,
                    usd_net,
                    release_version,
                    country_code.upper(),
                    order.description or f"PulseCore Pro ({release_version})",
                    effective_sku,
                    order.status or "COMPLETED",
                    order.approve_url,
                ),
            )
            if not existing:
                # Update release rollup and recalculate conversion_rate
                conn.execute(
                    """
                    UPDATE releases
                    SET paid_conversions = paid_conversions + 1,
                        conversion_rate = ROUND(
                            ((paid_conversions + 1) * 100.0) / CASE WHEN installs > 0 THEN installs ELSE 1 END,
                            2
                        ),
                        gross_usd = ROUND(gross_usd + ?, 2),
                        fees_usd = ROUND(fees_usd + ?, 2),
                        net_usd = ROUND(net_usd + ?, 2)
                    WHERE version = ?
                    """,
                    (usd_gross, usd_fee, usd_net, release_version),
                )
                # Update today's row in daily_metrics so 14-day sparkline & bar chart update instantaneously
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                conn.execute(
                    """
                    UPDATE daily_metrics
                    SET paid_orders = paid_orders + 1,
                        gross_usd = ROUND(gross_usd + ?, 2),
                        fees_usd = ROUND(fees_usd + ?, 2),
                        net_usd = ROUND(net_usd + ?, 2)
                    WHERE day_date = (SELECT MAX(day_date) FROM daily_metrics WHERE day_date <= ?)
                    """,
                    (usd_gross, usd_fee, usd_net, today_str),
                )
            conn.commit()

        return {
            "paypal_id": order.order_id,
            "gross": order.amount,
            "fee": fee,
            "net": net,
            "currency": cur,
            "usd_net": usd_net,
            "release_version": release_version,
            "approve_url": order.approve_url,
        }

    def record_webhook_event(self, event_data: dict[str, Any]) -> dict[str, Any]:
        """Ingest a PayPal Webhook event payload (live or triggered) into the SQLite ledger."""
        event_type = event_data.get("event_type", "").upper()
        resource = event_data.get("resource", {})

        if event_type in ("PAYMENT.CAPTURE.COMPLETED", "CHECKOUT.ORDER.APPROVED"):
            order_id = resource.get("id", f"ORD-WH-{int(time.time())}")
            amount_dict = resource.get("amount") or resource.get("dispute_amount") or {}
            if not amount_dict and "seller_receivable_breakdown" in resource:
                amount_dict = resource["seller_receivable_breakdown"].get("gross_amount", {})
            amt = float(amount_dict.get("value", 19.99))
            cur = str(amount_dict.get("currency_code", "USD")).upper()
            custom_id = resource.get("custom_id", "v1.2.1|US|PulseCore Pro")
            parts = custom_id.split("|")
            release_ver = parts[0] if len(parts) > 0 and parts[0] else "v1.2.1"
            country = parts[1] if len(parts) > 1 and parts[1] else "US"
            sku = parts[2] if len(parts) > 2 and parts[2] else "PulseCore Pro"

            order_res = PayPalOrderResult(
                success=True,
                order_id=order_id,
                status=resource.get("status", "COMPLETED"),
                amount=amt,
                currency=cur,
                description=f"Webhook Ingestion: {sku} ({release_ver})",
                approve_url=f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}",
            )
            return self.record_live_order(
                order_res,
                release_version=release_ver,
                country_code=country,
                platform_sku=sku,
            )
        elif event_type == "CUSTOMER.DISPUTE.CREATED":
            dispute_id = resource.get("dispute_id", f"PP-D-{int(time.time())}-SIM")
            amount_dict = resource.get("dispute_amount", {})
            amt = float(amount_dict.get("value", 29.99))
            cur = str(amount_dict.get("currency_code", "USD")).upper()
            buyer_country = resource.get("buyer_country", "US")
            buyer_email = resource.get("buyer_email", f"buyer-{dispute_id.lower()}@example.com")
            release_ver = resource.get("release_version", "v1.2.1")
            desc = resource.get("description", "Dispute triggered via webhook event.")
            sku = resource.get("custom_id", "PulseCore Pro")
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            due_str = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")

            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO disputes (
                        dispute_id, paypal_transaction_id, status, buyer_country,
                        buyer_email, release_version, platform_sku, amount, currency,
                        due_date, reason, description, created_at
                    ) VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED', ?, ?)
                    ON CONFLICT(dispute_id) DO UPDATE SET status = 'OPEN'
                    """,
                    (dispute_id, f"TX-{dispute_id}", buyer_country, buyer_email, release_ver, sku, amt, cur, due_str, desc, now_str),
                )
                conn.execute(
                    "UPDATE releases SET dispute_count = dispute_count + 1 WHERE version = ?",
                    (release_ver,),
                )
                conn.commit()
            return {"dispute_id": dispute_id, "status": "OPEN", "amount": amt, "currency": cur}
        elif event_type == "CUSTOMER.DISPUTE.RESOLVED":
            dispute_id = resource.get("dispute_id", "")
            if dispute_id:
                self.mark_dispute_resolved(dispute_id, "Resolved via webhook event", "PP-RESOLVE-WEBHOOK")
            return {"dispute_id": dispute_id, "status": "RESOLVED"}

        return {"event_type": event_type, "status": "IGNORED"}

    def seed_telemetry(
        self,
        live_orders: list[PayPalOrderResult] | None = None,
        live_invoice: PayPalInvoiceResult | None = None,
    ) -> dict[str, Any]:
        """Seed 30 days of universal indie software & micro-business history across 3 Git releases + real Sandbox orders."""
        now = datetime.now(timezone.utc)
        live_orders = live_orders or []

        with self._connect() as conn:
            conn.execute("DELETE FROM releases")
            conn.execute("DELETE FROM transactions")
            conn.execute("DELETE FROM disputes")
            conn.execute("DELETE FROM daily_metrics")

            # 1. Seed 3 Releases: v1.1.0 (Core Engine), v1.2.0 (Performance & Memory Optimization), v1.2.1 (EU Multi-Currency Tier)
            releases_data = [
                (
                    "v1.1.0",
                    "v1.1.0 - Core Engine & PayPal Checkout",
                    (now - timedelta(days=30)).strftime("%Y-%m-%d"),
                    "a4f81c2",
                    4120,
                    2890,
                    1180,
                    255,
                    6.19,
                    1272.45,
                    168.20,
                    1104.25,
                    2,
                    0,
                    "STABLE",
                    "Baseline production software release. Steady 6.2% install-to-paid conversion; low refund drag (0.8%).",
                ),
                (
                    "v1.2.0",
                    "v1.2.0 - Performance & Async Engine",
                    (now - timedelta(days=18)).strftime("%Y-%m-%d"),
                    "c91e7b4",
                    6840,
                    5410,
                    2390,
                    643,
                    9.40,
                    3530.07,
                    438.92,
                    3091.15,
                    3,
                    0,
                    "SURGE (+52% CONV)",
                    "Reduced CPU/memory overhead by 45%. Conversion jumped from 6.2% -> 9.4%; highest net margin and customer retention.",
                ),
                (
                    "v1.2.1",
                    "v1.2.1 - EU Multi-Currency Tier",
                    (now - timedelta(days=5)).strftime("%Y-%m-%d"),
                    "f309d8e",
                    3490,
                    2980,
                    1120,
                    248,
                    7.11,
                    1564.80,
                    229.40,
                    1335.40,
                    9,
                    3,
                    "DISPUTE ALERT (3 DE)",
                    "Introduced EUR 5.99 pricing tier. Triggered 3 German (DE) buyer disputes due to license key webhook timeout on EUR checkout.",
                ),
            ]
            conn.executemany(
                """
                INSERT INTO releases (
                    version, title, released_at, git_commit, installs, active_users,
                    trial_starts, paid_conversions, conversion_rate, gross_usd,
                    fees_usd, net_usd, refund_count, dispute_count, status_badge, ai_diagnostic
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                releases_data,
            )

            # 2. Seed 30-day daily metrics (including 14-day detailed window for sparklines)
            daily_rows = []
            for days_ago in range(29, -1, -1):
                day_dt = now - timedelta(days=days_ago)
                day_str = day_dt.strftime("%Y-%m-%d")
                if days_ago >= 18:
                    ver = "v1.1.0"
                    inst = 320 + ((days_ago * 17) % 90)
                    orders = 18 + ((days_ago * 5) % 8)
                    gross = round(orders * 4.99, 2)
                    disp = 0
                elif days_ago >= 5:
                    ver = "v1.2.0"
                    inst = 510 + ((days_ago * 29) % 140)
                    orders = 46 + ((days_ago * 7) % 15)
                    gross = round(orders * 5.49, 2)
                    disp = 0
                else:
                    ver = "v1.2.1"
                    inst = 640 + ((days_ago * 31) % 120)
                    orders = 48 + ((days_ago * 11) % 12)
                    gross = round(orders * 6.29, 2)
                    disp = 1 if days_ago in (3, 2, 1) else 0

                fees = round(gross * 0.132, 2)
                net = round(gross - fees, 2)
                daily_rows.append((day_str, inst, orders, gross, fees, net, disp, ver))

            conn.executemany(
                """
                INSERT INTO daily_metrics (
                    day_date, installs, paid_orders, gross_usd, fees_usd, net_usd, disputes, release_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                daily_rows,
            )

            # 3. Seed multi-currency historical + live transactions (USD, EUR, PHP) across realistic platforms & SKUs
            sample_txs = [
                ("PP-TXN-110-US01", (now - timedelta(days=25)).strftime("%Y-%m-%dT10:15:00Z"), 1272.45, 168.20, 1104.25, "USD", 1272.45, 168.20, 1104.25, "v1.1.0", "US", "PulseCore Pro License (v1.1.0)", "Linux (Docker Server)", "COMPLETED", 0, ""),
                ("PP-TXN-120-US01", (now - timedelta(days=14)).strftime("%Y-%m-%dT14:22:00Z"), 2180.00, 245.50, 1934.50, "USD", 2180.00, 245.50, 1934.50, "v1.2.0", "US", "PulseCore Pro Team (v1.2.0)", "macOS (Apple Silicon)", "COMPLETED", 0, ""),
                ("PP-TXN-120-EU01", (now - timedelta(days=11)).strftime("%Y-%m-%dT18:05:00Z"), 940.00, 134.80, 805.20, "EUR", 1024.60, 146.93, 877.67, "v1.2.0", "NL", "PulseCore Pro Dev Seat (v1.2.0)", "Windows 11 x64", "COMPLETED", 0, ""),
                ("PP-TXN-120-PH01", (now - timedelta(days=8)).strftime("%Y-%m-%dT06:40:00Z"), 18598.00, 2656.00, 15942.00, "PHP", 325.47, 46.49, 278.98, "v1.2.0", "PH", "PulseCore Pro APAC Tier (v1.2.0)", "Ubuntu Server 24.04", "COMPLETED", 0, ""),
                ("PP-TXN-121-US01", (now - timedelta(days=4)).strftime("%Y-%m-%dT12:10:00Z"), 680.00, 88.40, 591.60, "USD", 680.00, 88.40, 591.60, "v1.2.1", "US", "PulseCore Pro Developer (v1.2.1)", "Linux (Docker Engine)", "COMPLETED", 0, ""),
                ("PP-TXN-121-DE01", (now - timedelta(days=3)).strftime("%Y-%m-%dT09:30:00Z"), 590.00, 99.10, 490.90, "EUR", 643.10, 108.02, 535.08, "v1.2.1", "DE", "PulseCore Pro EU Seat (v1.2.1)", "Linux (Ubuntu Desktop)", "DISPUTED", 0, ""),
                ("PP-TXN-121-PH01", (now - timedelta(days=2)).strftime("%Y-%m-%dT04:12:00Z"), 13811.00, 1884.00, 11927.00, "PHP", 241.70, 32.98, 208.72, "v1.2.1", "PH", "PulseCore Pro APAC (v1.2.1)", "macOS (Apple Silicon)", "COMPLETED", 0, ""),
            ]
            conn.executemany(
                """
                INSERT INTO transactions (
                    paypal_id, created_at, gross_amount, fee_amount, net_amount,
                    currency, usd_gross, usd_fee, usd_net, release_version,
                    country_code, product_name, platform_sku, status,
                    is_live_sandbox, approve_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                sample_txs,
            )

            # Insert the real live PayPal Sandbox Orders created during `pp seed`
            live_meta_tags = [
                ("v1.1.0", "US", "Linux (Docker Engine)"),
                ("v1.2.0", "PH", "macOS (Apple Silicon)"),
                ("v1.2.1", "DE", "Windows 11 x64"),
            ]
            for idx, order in enumerate(live_orders):
                if not order.success or not order.order_id:
                    continue
                ver, country, platform_sku = live_meta_tags[idx % len(live_meta_tags)]
                cur = order.currency.upper()
                fx = FX_TO_USD.get(cur, 1.0)
                fee = compute_paypal_fee(order.amount, cur, cross_border=(country != "US"))
                net = round(order.amount - fee, 2)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO transactions (
                        paypal_id, created_at, gross_amount, fee_amount, net_amount,
                        currency, usd_gross, usd_fee, usd_net, release_version,
                        country_code, product_name, platform_sku, status,
                        is_live_sandbox, approve_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        order.order_id,
                        order.create_time or now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        order.amount,
                        fee,
                        net,
                        cur,
                        round(order.amount * fx, 2),
                        round(fee * fx, 2),
                        round(net * fx, 2),
                        ver,
                        country,
                        order.description or f"PulseCore Pro ({ver})",
                        platform_sku,
                        order.status or "CREATED",
                        order.approve_url,
                    ),
                )

            # 4. Seed the 3 German buyer disputes on v1.2.1
            dispute_tx_id = (
                live_orders[-1].order_id
                if live_orders and live_orders[-1].success
                else "PP-TXN-121-DE01"
            )
            disputes_data = [
                (
                    "PP-D-90411-DE",
                    dispute_tx_id,
                    (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M UTC"),
                    "DE",
                    "lukas.weber.muc@example.de",
                    "Ubuntu Linux 24.04 (CLI)",
                    "v1.2.1",
                    5.99,
                    "EUR",
                    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
                    "License key activation webhook timed out after €5.99 PayPal checkout unlock on v1.2.1.",
                    "OPEN",
                    (now + timedelta(days=2)).strftime("%Y-%m-%d"),
                ),
                (
                    "PP-D-90418-DE",
                    "PP-TXN-121-DE02",
                    (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M UTC"),
                    "DE",
                    "hanna.schmidt.berlin@example.de",
                    "macOS (Apple Silicon)",
                    "v1.2.1",
                    5.99,
                    "EUR",
                    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
                    "Payment captured via PayPal checkout, but v1.2.1 automated token dispatch failed with 504 Gateway Timeout.",
                    "OPEN",
                    (now + timedelta(days=3)).strftime("%Y-%m-%d"),
                ),
                (
                    "PP-D-90425-DE",
                    "PP-TXN-121-DE03",
                    (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M UTC"),
                    "DE",
                    "markus.bauer.hh@example.de",
                    "Windows 11 x64",
                    "v1.2.1",
                    5.99,
                    "EUR",
                    "BILLING_DISPUTE_VAT_DISPLAY",
                    "Charged €5.99 on v1.2.1 EU Pricing Tier; automated European VAT receipt missing from digital fulfillment email.",
                    "OPEN",
                    (now + timedelta(days=4)).strftime("%Y-%m-%d"),
                ),
            ]
            conn.executemany(
                """
                INSERT INTO disputes (
                    dispute_id, paypal_transaction_id, created_at, buyer_country,
                    buyer_email, platform_sku, release_version, amount, currency,
                    reason, description, status, due_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                disputes_data,
            )

            if live_invoice and live_invoice.success:
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_live_invoice_id', ?)",
                    (live_invoice.invoice_id,),
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_seeded_at', ?)",
                (now.strftime("%Y-%m-%d %H:%M:%S UTC"),),
            )
            conn.commit()

        return self.get_dashboard_summary()

    def seed_smartwatch_telemetry(
        self,
        live_orders: list[PayPalOrderResult] | None = None,
        live_invoice: PayPalInvoiceResult | None = None,
    ) -> dict[str, Any]:
        """Backward-compatible alias for seed_telemetry."""
        return self.seed_telemetry(live_orders=live_orders, live_invoice=live_invoice)

    def get_currency_breakdown(self) -> list[dict[str, Any]]:
        """Return multi-currency gross, fee drag, and net take-home breakdown."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    currency,
                    COUNT(*) AS tx_count,
                    ROUND(SUM(gross_amount), 2) AS gross,
                    ROUND(SUM(fee_amount), 2) AS fees,
                    ROUND(SUM(net_amount), 2) AS net,
                    ROUND(SUM(usd_gross), 2) AS usd_gross,
                    ROUND(SUM(usd_fee), 2) AS usd_fee,
                    ROUND(SUM(usd_net), 2) AS usd_net
                FROM transactions
                GROUP BY currency
                ORDER BY usd_gross DESC
                """
            ).fetchall()

        results = []
        for r in rows:
            gross = float(r["gross"] or 0.0)
            fees = float(r["fees"] or 0.0)
            net = float(r["net"] or 0.0)
            fee_pct = round((fees / gross) * 100, 1) if gross > 0 else 0.0
            net_margin_pct = round((net / gross) * 100, 1) if gross > 0 else 0.0
            results.append(
                {
                    "currency": r["currency"],
                    "tx_count": r["tx_count"],
                    "gross": gross,
                    "fees": fees,
                    "net": net,
                    "fee_pct": fee_pct,
                    "net_margin_pct": net_margin_pct,
                    "usd_gross": float(r["usd_gross"] or 0.0),
                    "usd_fee": float(r["usd_fee"] or 0.0),
                    "usd_net": float(r["usd_net"] or 0.0),
                }
            )
        return results

    def get_releases(self, include_git_tags: bool = True) -> list[dict[str, Any]]:
        """Return release-to-revenue timeline rows plus any local git repository tags."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM releases ORDER BY released_at ASC"
            ).fetchall()
        releases = [dict(r) for r in rows]
        for rel in releases:
            gross = float(rel["gross_usd"] or 0.0)
            net = float(rel["net_usd"] or 0.0)
            fees = float(rel["fees_usd"] or 0.0)
            rel["net_margin_pct"] = round((net / gross) * 100, 1) if gross > 0 else 0.0
            rel["fee_drag_pct"] = round((fees / gross) * 100, 1) if gross > 0 else 0.0

        if include_git_tags:
            known_versions = {r["version"] for r in releases}
            for tag in self._discover_git_tags():
                if tag not in known_versions:
                    releases.append(
                        {
                            "version": tag,
                            "title": f"{tag} (Local Git Tag)",
                            "released_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                            "git_commit": "HEAD",
                            "installs": 0,
                            "active_users": 0,
                            "trial_starts": 0,
                            "paid_conversions": 0,
                            "conversion_rate": 0.0,
                            "gross_usd": 0.0,
                            "fees_usd": 0.0,
                            "net_usd": 0.0,
                            "net_margin_pct": 0.0,
                            "fee_drag_pct": 0.0,
                            "refund_count": 0,
                            "dispute_count": 0,
                            "status_badge": "GIT TAG",
                            "ai_diagnostic": "Detected local Git tag in repository awaiting telemetry sync.",
                        }
                    )
        return releases

    def _discover_git_tags(self) -> list[str]:
        if hasattr(self, "_git_tags_cache") and self._git_tags_cache is not None:
            return self._git_tags_cache
        try:
            proc = subprocess.run(
                ["git", "tag", "-l"],
                cwd=str(PACKAGE_ROOT),
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                self._git_tags_cache = [
                    line.strip() for line in proc.stdout.splitlines() if line.strip()
                ]
                return self._git_tags_cache
        except Exception:
            pass
        self._git_tags_cache = []
        return self._git_tags_cache

    def get_disputes(self, status_filter: str | None = None) -> list[dict[str, Any]]:
        """Return disputes from local dual-engine ledger."""
        with self._connect() as conn:
            if status_filter:
                rows = conn.execute(
                    "SELECT * FROM disputes WHERE UPPER(status) = UPPER(?) ORDER BY created_at DESC",
                    (status_filter,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM disputes ORDER BY created_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_dispute_by_id(self, dispute_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM disputes WHERE UPPER(dispute_id) = UPPER(?)",
                (dispute_id.strip(),),
            ).fetchone()
        return dict(row) if row else None

    def mark_dispute_resolved(
        self,
        dispute_id: str,
        ai_evidence: str,
        resolution_order_id: str,
    ) -> bool:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT release_version FROM disputes WHERE UPPER(dispute_id) = UPPER(?)",
                (dispute_id.strip(),),
            ).fetchone()
            if not row:
                return False
            rel_ver = row["release_version"]
            cur = conn.execute(
                """
                UPDATE disputes
                SET status = 'RESOLVED',
                    ai_evidence_draft = ?,
                    resolution_order_id = ?,
                    resolved_at = ?
                WHERE UPPER(dispute_id) = UPPER(?)
                """,
                (ai_evidence, resolution_order_id, now_str, dispute_id.strip()),
            )
            open_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM disputes WHERE release_version = ? AND UPPER(status) = 'OPEN'",
                (rel_ver,),
            ).fetchone()
            open_cnt = int(open_row["cnt"]) if open_row else 0
            new_badge = (
                f"DISPUTE ALERT ({open_cnt} DE)"
                if open_cnt > 0
                else "RESOLVED (PATCH v1.2.2)"
            )
            conn.execute(
                """
                UPDATE releases
                SET dispute_count = ?,
                    status_badge = ?
                WHERE version = ?
                """,
                (open_cnt, new_badge, rel_ver),
            )
            conn.commit()
            return cur.rowcount > 0

    def resolve_dispute(
        self,
        dispute_id: str,
        ai_evidence: str = "",
        replacement_order_id: str = "",
    ) -> bool:
        """Alias for mark_dispute_resolved with flexible keyword arguments."""
        return self.mark_dispute_resolved(
            dispute_id=dispute_id,
            ai_evidence=ai_evidence,
            resolution_order_id=replacement_order_id,
        )

    def get_recent_daily_metrics(self, days: int = 14) -> list[dict[str, Any]]:
        """Return last N days of revenue and install metrics sorted chronologically."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM daily_metrics
                ORDER BY day_date DESC
                LIMIT ?
                """,
                (days,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_transactions(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return all transactions ledger entries ordered by most recent first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, paypal_id, created_at, gross_amount, fee_amount, net_amount,
                       currency, release_version, country_code, platform_sku, status, is_live_sandbox
                FROM transactions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_live_sandbox_transactions(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return transactions backed by real live PayPal Sandbox Order IDs."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM transactions
                WHERE is_live_sandbox = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_dashboard_summary(
        self,
        api_latency_ms: float = 180.0,
        api_connected: bool = True,
    ) -> dict[str, Any]:
        """Compute complete KPI summary for `pp pulse`, `pp ask`, and `pp tui`."""
        currencies = self.get_currency_breakdown()
        releases = self.get_releases(include_git_tags=False)
        disputes = self.get_disputes()
        open_disputes = [d for d in disputes if d["status"] == "OPEN"]
        daily_14 = self.get_recent_daily_metrics(days=14)
        live_txs = self.get_live_sandbox_transactions(limit=10)

        total_gross_usd = round(sum(c["usd_gross"] for c in currencies), 2)
        total_fees_usd = round(sum(c["usd_fee"] for c in currencies), 2)
        total_net_usd = round(sum(c["usd_net"] for c in currencies), 2)
        fee_drag_pct = (
            round((total_fees_usd / total_gross_usd) * 100, 1)
            if total_gross_usd > 0
            else 0.0
        )
        net_margin_pct = (
            round((total_net_usd / total_gross_usd) * 100, 1)
            if total_gross_usd > 0
            else 0.0
        )

        total_installs = sum(int(r["installs"]) for r in releases)
        active_users = sum(int(r["active_users"]) for r in releases)
        total_paid = sum(int(r["paid_conversions"]) for r in releases)
        overall_conv = (
            round((total_paid / total_installs) * 100, 2)
            if total_installs > 0
            else 0.0
        )

        # MRR (Monthly Recurring Revenue from recurring software subscriptions + license seats)
        mrr_usd = round(total_net_usd * 0.68, 2)

        health_telemetry = self.compute_merchant_health(
            open_disputes_count=len(open_disputes),
            total_orders=total_paid,
            net_margin_pct=net_margin_pct,
            latest_release_disputes=(releases[-1]["dispute_count"] if releases else 0),
            api_latency_ms=api_latency_ms,
            api_connected=api_connected,
        )

        return {
            "seeded": len(releases) > 0,
            "last_seeded_at": self.get_meta("last_seeded_at", "Never"),
            "last_live_invoice_id": self.get_meta("last_live_invoice_id", ""),
            "total_gross_usd": total_gross_usd,
            "total_fees_usd": total_fees_usd,
            "total_net_usd": total_net_usd,
            "fee_drag_pct": fee_drag_pct,
            "net_margin_pct": net_margin_pct,
            "total_installs": total_installs,
            "active_users": active_users,
            "total_customers": total_installs,
            "active_customers": active_users,
            "total_orders": total_paid,
            "total_paid": total_paid,
            "overall_conversion_pct": overall_conv,
            "mrr_usd": mrr_usd,
            "health_score": health_telemetry["composite_score"],
            "health_rating": health_telemetry["rating"],
            "health_telemetry": health_telemetry,
            "open_disputes_count": len(open_disputes),
            "total_disputes_count": len(disputes),
            "currencies": currencies,
            "releases": releases,
            "disputes": disputes,
            "daily_14": daily_14,
            "live_sandbox_orders": live_txs,
            "recent_transactions": self.get_transactions(limit=15),
        }

    def compute_merchant_health(
        self,
        open_disputes_count: int,
        total_orders: int,
        net_margin_pct: float,
        latest_release_disputes: int,
        api_latency_ms: float = 180.0,
        api_connected: bool = True,
    ) -> dict[str, Any]:
        """Compute live CFO-grade Merchant Health Index across 4 dimensional sub-scores."""
        # 1. Risk & Dispute Health (0-100)
        dispute_ratio_pct = round((open_disputes_count / max(1, total_orders)) * 100, 2)
        if open_disputes_count == 0:
            risk_score = 100
        elif dispute_ratio_pct <= 0.35:
            risk_score = 84
        elif dispute_ratio_pct <= 0.80:
            risk_score = 70
        elif dispute_ratio_pct <= 1.50:
            risk_score = 52
        else:
            risk_score = 30

        # 2. Take-Home Margin Efficiency (0-100)
        if net_margin_pct >= 90.0:
            margin_score = 98
        elif net_margin_pct >= 85.0:
            margin_score = 91
        elif net_margin_pct >= 80.0:
            margin_score = 82
        elif net_margin_pct >= 70.0:
            margin_score = 68
        else:
            margin_score = 45

        # 3. Release Financial Stability (0-100)
        if latest_release_disputes == 0:
            release_score = 96
        elif latest_release_disputes <= 2:
            release_score = 75
        elif latest_release_disputes <= 4:
            release_score = 62
        else:
            release_score = 40

        # 4. Gateway & API Telemetry (0-100)
        if api_connected:
            if api_latency_ms < 300:
                gateway_score = 98
            elif api_latency_ms < 650:
                gateway_score = 86
            elif api_latency_ms < 1200:
                gateway_score = 72
            else:
                gateway_score = 55
        else:
            gateway_score = 40

        composite = int(
            round(
                0.35 * risk_score
                + 0.25 * margin_score
                + 0.20 * release_score
                + 0.20 * gateway_score
            )
        )
        composite = max(20, min(100, composite))

        if composite >= 90:
            rating = "EXCELLENT"
            rating_color = "bold green"
        elif composite >= 75:
            rating = "HEALTHY"
            rating_color = "bold cyan"
        elif composite >= 60:
            rating = "NEEDS ATTENTION"
            rating_color = "bold yellow"
        else:
            rating = "CRITICAL RISK"
            rating_color = "bold red"

        return {
            "composite_score": composite,
            "rating": rating,
            "rating_color": rating_color,
            "risk_score": risk_score,
            "margin_score": margin_score,
            "release_score": release_score,
            "gateway_score": gateway_score,
            "dispute_ratio_pct": dispute_ratio_pct,
            "net_margin_pct": net_margin_pct,
            "api_latency_ms": api_latency_ms,
            "api_connected": api_connected,
        }

    def simulate_incoming_dispute(
        self,
        amount: float = 5.99,
        currency: str = "EUR",
        platform_sku: str = "Ubuntu Linux 24.04 (CLI)",
        release_version: str = "v1.2.1",
        reason: str | None = None,
        buyer_country: str = "DE",
    ) -> dict[str, Any]:
        """Simulate an incoming real-world chargeback event to test live dispute and CFO score workflows."""
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        due_date = (now + timedelta(days=7)).strftime("%Y-%m-%d")
        rand_suffix = uuid.uuid4().hex[:5].upper()
        dispute_id = f"PP-D-SIM-{rand_suffix}"

        reasons = [
            "License activation token failed with 504 Gateway Timeout during checkout unlock",
            "Customer claims recurring seat billed without localized EU VAT receipt attachment",
            "CLI authentication token expired immediately after PayPal transaction capture",
        ]
        desc = reason or random.choice(reasons)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO disputes (
                    dispute_id, paypal_transaction_id, created_at, buyer_country,
                    buyer_email, platform_sku, release_version, amount, currency,
                    reason, description, status, due_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """,
                (
                    dispute_id,
                    f"PP-TXN-SIM-{rand_suffix}",
                    now_str,
                    buyer_country,
                    f"simulated-buyer-{rand_suffix.lower()}@example.de",
                    platform_sku,
                    release_version,
                    amount,
                    currency,
                    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    desc,
                    due_date,
                ),
            )
            # Increment dispute count on current release
            conn.execute(
                """
                UPDATE releases
                SET dispute_count = dispute_count + 1,
                    status_badge = 'DISPUTE ALERT (ACTIVE)'
                WHERE version = ?
                """,
                (release_version,),
            )
            # Increment today's daily_metrics dispute count
            today_str = now.strftime("%Y-%m-%d")
            conn.execute(
                """
                UPDATE daily_metrics
                SET disputes = disputes + 1
                WHERE day_date = (SELECT MAX(day_date) FROM daily_metrics WHERE day_date <= ?)
                """,
                (today_str,),
            )
            conn.commit()

        return self.get_dispute_by_id(dispute_id) or {}

    def sync_external_disputes(self, external_items: list[dict[str, Any]]) -> int:
        """Upsert real PayPal Sandbox disputes fetched from GET /v1/customer/disputes."""
        if not external_items:
            return 0
        added = 0
        with self._connect() as conn:
            for item in external_items:
                disp_id = item.get("dispute_id", "")
                if not disp_id:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM disputes WHERE dispute_id = ?", (disp_id,)
                ).fetchone()
                if not exists:
                    dispute_amount = float(
                        item.get("dispute_amount", {}).get("value", 5.99)
                    )
                    curr = item.get("dispute_amount", {}).get("currency_code", "USD")
                    reason = item.get("reason", "MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
                    status = item.get("status", "OPEN")
                    create_time = item.get("create_time", datetime.now(timezone.utc).isoformat())
                    conn.execute(
                        """
                        INSERT INTO disputes (
                            dispute_id, paypal_transaction_id, created_at, buyer_country,
                            buyer_email, platform_sku, release_version, amount, currency,
                            reason, description, status, due_date
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            disp_id,
                            item.get("disputed_transactions", [{}])[0].get("seller_transaction_id", "PP-SANDBOX-TX"),
                            create_time,
                            "US",
                            "sandbox-buyer@example.com",
                            "Universal Platform",
                            "v1.2.1",
                            dispute_amount,
                            curr,
                            reason,
                            f"Live Sandbox Dispute: {reason}",
                            status,
                            (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d"),
                        ),
                    )
                    added += 1
            if added > 0:
                conn.commit()
        return added
