"""Dual-Engine SQLite Storage (.pulse_cache/pulse.db) for PayPal Pulse."""

from __future__ import annotations

import json
import os
import random
import re
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
    "CAD": 0.72,
    "AUD": 0.66,
    "JPY": 0.0065,
    "SGD": 0.75,
    "HKD": 0.128,
    "NZD": 0.60,
    "CHF": 1.13,
    "SEK": 0.093,
    "NOK": 0.091,
    "DKK": 0.146,
    "PLN": 0.25,
    "CZK": 0.043,
    "HUF": 0.0027,
    "ILS": 0.27,
    "MXN": 0.050,
    "BRL": 0.175,
    "MYR": 0.225,
    "TWD": 0.031,
    "THB": 0.029,
    "INR": 0.0119,
    "CNY": 0.138,
}

CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "PHP": "₱",
    "GBP": "£",
    "CAD": "CA$",
    "AUD": "A$",
    "JPY": "¥",
    "SGD": "S$",
    "HKD": "HK$",
    "NZD": "NZ$",
    "CHF": "CHF ",
    "SEK": "kr ",
    "NOK": "kr ",
    "DKK": "kr ",
    "PLN": "zł",
    "CZK": "Kč ",
    "HUF": "Ft ",
    "ILS": "₪",
    "MXN": "MX$",
    "BRL": "R$",
    "MYR": "RM",
    "TWD": "NT$",
    "THB": "฿",
    "INR": "₹",
    "CNY": "¥",
}


ZERO_DECIMAL_CURRENCIES = {"JPY", "HUF", "TWD"}

# PayPal Orders v2 statuses that mean money has actually moved. An order that is
# only CREATED or APPROVED has not been captured, so it must not be counted as
# revenue or as a paid conversion.
PAID_TRANSACTION_STATUSES = ("COMPLETED", "CAPTURED", "DISPUTED")
PENDING_TRANSACTION_STATUSES = ("CREATED", "APPROVED", "PENDING")
# Refunds and reversals carry negative amounts. Revenue sums include them so
# net revenue is not inflated. Paid-order counts still use PAID only.
REFUND_TRANSACTION_STATUSES = ("REFUNDED", "REVERSED")
REVENUE_TRANSACTION_STATUSES = PAID_TRANSACTION_STATUSES + REFUND_TRANSACTION_STATUSES

# PayPal dispute states that still need seller action. Exact matches on "OPEN"
# miss every dispute that has moved to WAITING_FOR_SELLER_RESPONSE or
# REQUIRED_ACTION.
OPEN_DISPUTE_STATUSES = (
    "OPEN",
    "WAITING_FOR_SELLER_RESPONSE",
    "WAITING_FOR_BUYER_RESPONSE",
    "REQUIRED_ACTION",
    "UNDER_REVIEW",
)


def format_currency_amount(amount: float, currency: str) -> str:
    """Format numeric amount according to PayPal REST API decimal rules.
    
    Zero-decimal currencies (JPY, HUF, TWD) must NOT include decimal fractions.
    All other currencies require exactly two decimal places (e.g. 14.99).
    """
    cur = currency.upper().strip()
    if cur in ZERO_DECIMAL_CURRENCIES:
        return str(int(round(amount)))
    return f"{amount:.2f}"


def compute_paypal_fee(amount: float, currency: str, cross_border: bool = True) -> float:
    """Estimate PayPal's commercial fee for a received payment.

    Percentage: 3.49% domestic plus 1.50% international (US commercial rate).
    Fixed fee: PayPal's published commercial fixed fee for the received
    currency. INR and CNY have no published commercial fixed fee; the nearest
    published QR-code fee is used and is an estimate.
    """
    cur = currency.upper()
    rate = 0.0349 + (0.015 if cross_border else 0.0)
    fixed_map = {
        "USD": 0.49,
        "EUR": 0.39,
        "PHP": 25.00,
        "GBP": 0.39,
        "CAD": 0.59,
        "AUD": 0.59,
        "JPY": 49.00,
        "SGD": 0.69,
        "HKD": 3.79,
        "NZD": 0.69,
        "CHF": 0.49,
        "SEK": 4.09,
        "NOK": 3.90,
        "DKK": 2.90,
        "PLN": 1.89,
        "CZK": 9.00,
        "HUF": 149.00,
        "ILS": 1.60,
        "MXN": 9.00,
        "BRL": 2.90,
        "MYR": 2.00,
        "TWD": 14.00,
        "THB": 15.00,
        # Not in the published commercial fixed-fee table; QR-code estimate.
        "INR": 30.00,
        "CNY": 2.10,
    }
    fixed = fixed_map.get(cur, 0.49)
    if cur in ZERO_DECIMAL_CURRENCIES:
        return float(round(amount * rate + fixed))
    return round(amount * rate + fixed, 2)


def parse_release_version(custom_id: str | None, default: str = "v0.1.0") -> str:
    """Extract release version from custom_id metadata (e.g. 'release:v1.3.0', JSON, or pipe format)."""
    if not custom_id:
        return default
    text = str(custom_id).strip()
    if not text:
        return default

    # 1. Prefix format: "release:v1.3.0" or "release:v1.3.0|US|SKU"
    if text.lower().startswith("release:"):
        remainder = text[8:].strip()
        first = remainder.split("|")[0].strip()
        tokens = first.split()
        if tokens:
            return tokens[0]
        return default

    # 2. JSON format: {"v": "v1.3.0"} or {"version": "v1.3.0"}
    if text.startswith("{") and text.endswith("}"):
        try:
            import json

            data = json.loads(text)
            for k in ("v", "version", "release", "release_version", "tag"):
                if k in data and data[k]:
                    return str(data[k]).strip()
        except Exception:
            pass

    # 3. Delimited pipe format: "v1.2.1|US|PulseCore Pro"
    if "|" in text:
        ver = text.split("|")[0].strip()
        if ver:
            return ver

    # 4. Direct semver/git tag pattern (e.g. "v1.3.0" or "1.3.0")
    parts = text.split()
    first_token = parts[0] if parts else text
    if first_token.startswith("v") and len(first_token) > 1 and any(c.isdigit() for c in first_token):
        return first_token

    return first_token or default


def detect_project_context(cwd: Path | None = None) -> dict[str, Any]:
    """Inspect directory for .pulse.json, git metadata, and language package descriptors."""
    target_dir = cwd or Path.cwd()
    info: dict[str, Any] = {
        "project_name": target_dir.name,
        "version": "v0.1.0",
        "default_currency": "USD",
        "default_sku": f"{target_dir.name} Pro",
        "default_price": 19.99,
        "git_repo": False,
        "git_branch": None,
        "git_commit": None,
        "git_tag": None,
        "config_file": None,
        "package_type": None,
    }

    # 1. Inspect .pulse.json in target_dir or parents
    pulse_cfg = target_dir / ".pulse.json"
    if not pulse_cfg.exists():
        for p in target_dir.parents:
            cand = p / ".pulse.json"
            if cand.exists():
                pulse_cfg = cand
                break
    has_custom_sku = False
    if pulse_cfg.exists():
        try:
            import json

            data = json.loads(pulse_cfg.read_text(encoding="utf-8-sig"))
            info["config_file"] = str(pulse_cfg)
            if "project_name" in data and data["project_name"]:
                info["project_name"] = str(data["project_name"])
            if "version" in data and data["version"]:
                info["version"] = str(data["version"])
            if "default_currency" in data and data["default_currency"]:
                info["default_currency"] = str(data["default_currency"]).upper()
            if "default_sku" in data and data["default_sku"]:
                info["default_sku"] = str(data["default_sku"])
                has_custom_sku = True
            if "default_price" in data and data["default_price"] is not None:
                try:
                    info["default_price"] = float(data["default_price"])
                except Exception:
                    pass
        except Exception:
            pass

    # 2. Inspect Git repository
    try:
        proc_rev = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc_rev.returncode == 0 and proc_rev.stdout.strip() == "true":
            info["git_repo"] = True
            proc_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if proc_branch.returncode == 0 and proc_branch.stdout.strip():
                info["git_branch"] = proc_branch.stdout.strip()

            proc_sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if proc_sha.returncode == 0 and proc_sha.stdout.strip():
                info["git_commit"] = proc_sha.stdout.strip()

            proc_tag = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if proc_tag.returncode == 0 and proc_tag.stdout.strip():
                tag_out = proc_tag.stdout.strip()
                info["git_tag"] = tag_out
                if info["version"] == "v0.1.0" or not info.get("config_file"):
                    info["version"] = tag_out
    except Exception:
        pass

    # 3. Detect package ecosystem
    pkg_json = target_dir / "package.json"
    if pkg_json.exists():
        info["package_type"] = "npm"
        try:
            import json

            pdata = json.loads(pkg_json.read_text(encoding="utf-8-sig"))
            if "name" in pdata and (info["project_name"] == target_dir.name or not info.get("config_file")):
                info["project_name"] = str(pdata["name"])
            if "version" in pdata and not info.get("config_file"):
                ver = str(pdata["version"])
                info["version"] = ver if ver.startswith("v") else f"v{ver}"
        except Exception:
            pass

    pyproj = target_dir / "pyproject.toml"
    if pyproj.exists():
        info["package_type"] = "python"
        try:
            for line in pyproj.read_text(encoding="utf-8-sig").splitlines():
                if line.strip().startswith("version") and "=" in line:
                    ver = line.split("=")[1].strip().strip('"').strip("'")
                    if ver and not info.get("config_file"):
                        info["version"] = ver if ver.startswith("v") else f"v{ver}"
                        break
        except Exception:
            pass

    cargo = target_dir / "Cargo.toml"
    if cargo.exists():
        info["package_type"] = "rust"

    gomod = target_dir / "go.mod"
    if gomod.exists():
        info["package_type"] = "go"

    if not has_custom_sku:
        info["default_sku"] = f"{info['project_name']} Pro"

    return info


_TAG_ALLOWLIST = re.compile(r"^v?[0-9][0-9A-Za-z._-]*$")

_DELIVERY_KEYS = {"commit_sha", "release_version", "channel", "note"}


def recent_commits(tag: str, limit: int = 8) -> list[str]:
    """Return up to `limit` commit subjects reachable from a release tag.

    The tag is checked against an allowlist before it reaches git, so an
    untrusted value cannot start another program. Every failure returns an
    empty list, because missing history must never break evidence drafting.
    """
    tag = (tag or "").strip()
    if not tag or not _TAG_ALLOWLIST.match(tag) or limit <= 0:
        return []
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CEILING_DIRECTORIES": str(Path.cwd().resolve().parent),
    }
    try:
        proc = subprocess.run(
            ["git", "log", "--format=%s", "-n", str(limit), tag, "--"],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    subjects = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return subjects[:limit]


def parse_delivery_jsonl(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse JSONL delivery records into valid records and per-line errors.

    One record per line. Each record needs a commit_sha. Unknown keys are
    rejected so a typo cannot silently drop a delivery.
    """
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {lineno}: invalid JSON ({exc.msg})")
            continue
        if not isinstance(obj, dict):
            errors.append(f"line {lineno}: expected a JSON object")
            continue
        unknown = set(obj) - _DELIVERY_KEYS
        if unknown:
            errors.append(f"line {lineno}: unknown keys {sorted(unknown)}")
            continue
        sha = str(obj.get("commit_sha", "")).strip()
        if not sha:
            errors.append(f"line {lineno}: commit_sha is required")
            continue
        records.append(
            {
                "commit_sha": sha,
                "release_version": str(obj.get("release_version", "") or ""),
                "channel": str(obj.get("channel", "") or "unknown"),
                "note": str(obj.get("note", "") or ""),
            }
        )
    return records, errors


class PulseStore:
    """SQLite telemetry & dual-engine ledger store."""

    def __init__(
        self,
        db_path: Path | None = None,
        settings: PulseSettings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db_path = db_path or self.settings.db_path
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

                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT UNIQUE NOT NULL,
                    commit_sha TEXT NOT NULL,
                    release_version TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT 'unknown',
                    note TEXT NOT NULL DEFAULT '',
                    delivered_at TEXT NOT NULL
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

    def has_live_data(self) -> bool:
        """Check if transactions table has any recorded orders or webhook captures."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM transactions").fetchone()
            return bool(row and row["cnt"] > 0)

    def is_clean_mode(self) -> bool:
        """Check if auto-seeding is suppressed to preserve a pristine, unseeded slate."""
        return self.get_meta("clean_mode") == "1"

    def set_clean_mode(self, enabled: bool = True) -> None:
        """Toggle clean mode to suppress or permit synthetic auto-seeding."""
        self.set_meta("clean_mode", "1" if enabled else "0")

    def should_auto_seed(self) -> bool:
        """Return True only for an isolated demo database.

        Real project ledgers stay empty until real orders or an explicit user
        command record rows. Seeding them would present fabricated revenue to a
        developer who has not connected PayPal yet.
        """
        if self.db_path.name != "demo.db":
            return False
        return not (self.is_seeded() or self.is_clean_mode())

    def detect_state(self) -> str:
        """Classify this ledger as 'fresh', 'sample', or 'connected'.

        'fresh' means no releases and no transactions. 'sample' means the rows
        came from seed_telemetry, the only writer of the last_seeded_at marker.
        'connected' means the rows came from PayPal or an explicit command.
        """
        if not self.is_seeded() and not self.has_live_data():
            return "fresh"
        if self.db_path.name == "demo.db" or self.get_meta("last_seeded_at", "").strip():
            return "sample"
        return "connected"

    def next_actions(self) -> list[str]:
        """Return the short list of commands that move this ledger forward."""
        state = self.detect_state()
        if state == "fresh":
            return [
                "Run `pp setup` to connect PayPal Sandbox credentials.",
                "Run `pp init` in your project so releases carry attribution.",
                "Run `pp order <amount> --release <tag>` to record a real order.",
            ]
        if state == "sample":
            return ["Run `pp reset` to clear sample data before recording real orders."]
        return []

    def reset(self, mark_clean: bool = True) -> None:
        """Wipe all telemetry tables cleanly without deleting the database file."""
        with self._connect() as conn:
            conn.execute("DELETE FROM releases")
            conn.execute("DELETE FROM transactions")
            conn.execute("DELETE FROM disputes")
            conn.execute("DELETE FROM daily_metrics")
            conn.execute("DELETE FROM deliveries")
            conn.execute("DELETE FROM meta WHERE key IN ('last_seeded_at', 'last_live_invoice_id')")
            conn.commit()
        if mark_clean:
            self.set_clean_mode(True)

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

    def record_delivery(
        self,
        commit_sha: str,
        release_version: str | None = None,
        channel: str = "unknown",
        note: str = "",
    ) -> dict[str, Any]:
        """Record that a commit shipped to a release channel."""
        sha = (commit_sha or "").strip()
        if not sha:
            raise ValueError("commit_sha is required")
        delivery_id = f"DLV-{sha[:12].upper()}"
        delivered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        release = release_version or ""
        chan = channel or "unknown"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deliveries (delivery_id, commit_sha, release_version, channel, note, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(delivery_id) DO UPDATE SET
                    release_version = excluded.release_version,
                    channel = excluded.channel,
                    note = excluded.note,
                    delivered_at = excluded.delivered_at
                """,
                (delivery_id, sha, release, chan, note or "", delivered_at),
            )
            conn.commit()
        return {
            "delivery_id": delivery_id,
            "commit_sha": sha,
            "release_version": release,
            "channel": chan,
            "note": note or "",
            "delivered_at": delivered_at,
        }

    def get_deliveries(self, release_version: str | None = None) -> list[dict[str, Any]]:
        """Return recorded deliveries, newest first."""
        query = "SELECT * FROM deliveries"
        params: tuple[Any, ...] = ()
        if release_version:
            query += " WHERE release_version = ?"
            params = (release_version,)
        query += " ORDER BY id DESC"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def _ensure_release(
        self, conn: sqlite3.Connection, version: str, title: str | None = None
    ) -> None:
        """Create a minimal release row so a rollup never targets a missing row."""
        if not version:
            return
        exists = conn.execute(
            "SELECT version FROM releases WHERE version = ?", (version,)
        ).fetchone()
        if exists:
            return
        now_ymd = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn.execute(
            """
            INSERT INTO releases (
                version, title, released_at, git_commit, installs, active_users,
                trial_starts, paid_conversions, conversion_rate, gross_usd,
                fees_usd, net_usd, refund_count, dispute_count, status_badge, ai_diagnostic
            ) VALUES (?, ?, ?, 'HEAD', 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0, 'ACTIVE', ?)
            """,
            (
                version,
                title or f"{version} (Release)",
                now_ymd,
                "Recorded from a captured PayPal transaction.",
            ),
        )

    def _apply_paid_rollup(
        self,
        conn: sqlite3.Connection,
        release_version: str,
        usd_gross: float,
        usd_fee: float,
        usd_net: float,
        day_date: str,
        count_order: bool = True,
    ) -> None:
        """Add one captured payment to the release and daily rollups.

        ``count_order=False`` applies the revenue delta without incrementing
        the paid-order count. Refunds and reversals use it so they reduce net
        revenue without looking like new sales.
        """
        self._ensure_release(conn, release_version)
        inc = 1 if count_order else 0
        conn.execute(
            f"""
            UPDATE releases
            SET paid_conversions = paid_conversions + {inc},
                conversion_rate = CASE
                    WHEN installs > 0 THEN ROUND(((paid_conversions + {inc}) * 100.0) / installs, 2)
                    ELSE 0.0
                END,
                gross_usd = ROUND(gross_usd + ?, 2),
                fees_usd = ROUND(fees_usd + ?, 2),
                net_usd = ROUND(net_usd + ?, 2)
            WHERE version = ?
            """,
            (usd_gross, usd_fee, usd_net, release_version),
        )
        day = str(day_date or "")[:10]
        if not day:
            return
        row = conn.execute(
            "SELECT day_date FROM daily_metrics WHERE day_date = ?", (day,)
        ).fetchone()
        if row:
            conn.execute(
                f"""
                UPDATE daily_metrics
                SET paid_orders = paid_orders + {inc},
                    gross_usd = ROUND(gross_usd + ?, 2),
                    fees_usd = ROUND(fees_usd + ?, 2),
                    net_usd = ROUND(net_usd + ?, 2)
                WHERE day_date = ?
                """,
                (usd_gross, usd_fee, usd_net, day),
            )
        else:
            conn.execute(
                f"""
                INSERT INTO daily_metrics (
                    day_date, installs, paid_orders, gross_usd, fees_usd, net_usd, disputes, release_version
                ) VALUES (?, 0, {inc}, ?, ?, ?, 0, ?)
                """,
                (day, usd_gross, usd_fee, usd_net, release_version),
            )

    def record_live_order(
        self,
        order: PayPalOrderResult,
        release_version: str | None = None,
        country_code: str = "US",
        platform_sku: str | None = None,
        device_model: str | None = None,
        fee_override: float | None = None,
        net_override: float | None = None,
    ) -> dict[str, Any]:
        """Insert a freshly created live PayPal Sandbox order into the instant local ledger."""
        cur = order.currency.upper()
        ctx = detect_project_context()
        default_sku = ctx.get("default_sku") or "PulseCore Pro"
        effective_release = release_version or ctx.get("version") or "v1.2.1"
        legacy_default = "Ubuntu Linux 24.04 (CLI)"
        clean_model = device_model if device_model and device_model != legacy_default else None
        clean_platform = platform_sku if platform_sku and platform_sku != legacy_default else None
        effective_sku = clean_model or clean_platform or default_sku
        fx = FX_TO_USD.get(cur, 1.0)
        cross_border = country_code.upper() != "US" or cur != "USD"
        # A captured payment carries PayPal's real fee and net. Use them when
        # the caller has them; only fall back to the local fee model otherwise.
        fee = (
            round(float(fee_override), 2)
            if fee_override is not None
            else compute_paypal_fee(order.amount, cur, cross_border=cross_border)
        )
        net = (
            round(float(net_override), 2)
            if net_override is not None
            else round(order.amount - fee, 2)
        )
        usd_gross = round(order.amount * fx, 2)
        usd_fee = round(fee * fx, 2)
        usd_net = round(net * fx, 2)
        created_at = order.create_time or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # An order is only revenue once PayPal has captured it. CREATED and
        # APPROVED orders are recorded for instant feedback but stay pending.
        is_paid = str(order.status or "").upper() in PAID_TRANSACTION_STATUSES

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, status FROM transactions WHERE paypal_id = ?",
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
                    approve_url = excluded.approve_url,
                    gross_amount = excluded.gross_amount,
                    fee_amount = excluded.fee_amount,
                    net_amount = excluded.net_amount,
                    usd_gross = excluded.usd_gross,
                    usd_fee = excluded.usd_fee,
                    usd_net = excluded.usd_net
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
                    effective_release,
                    country_code.upper(),
                    order.description or f"{effective_sku} ({effective_release})",
                    effective_sku,
                    order.status or "COMPLETED",
                    order.approve_url,
                ),
            )
            if not existing:
                # Dynamic release upsert: ensure release exists in releases table with real Git metadata if available
                cur_rel = conn.execute("SELECT version FROM releases WHERE version = ?", (effective_release,)).fetchone()
                if not cur_rel:
                    now_ymd = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    tag_meta = self._discover_git_tag_metadata().get(effective_release, {})
                    commit_sha = tag_meta.get("commit", "HEAD")
                    rel_date = tag_meta.get("date", now_ymd)
                    rel_title = (
                        f"{effective_release} — {tag_meta['subject']}"
                        if tag_meta.get("subject")
                        else f"{effective_release} (Release)"
                    )
                    conn.execute(
                        """
                        INSERT INTO releases (
                            version, title, released_at, git_commit, installs, active_users,
                            trial_starts, paid_conversions, conversion_rate, gross_usd,
                            fees_usd, net_usd, refund_count, dispute_count, status_badge, ai_diagnostic
                        ) VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0, 'ACTIVE', 'Dynamic release created via live PayPal transaction.')
                        """,
                        (effective_release, rel_title, rel_date, commit_sha),
                    )

                # Roll up revenue only for captured orders. A CREATED or APPROVED
                # order stays pending until PayPal reports it as captured.
                if is_paid:
                    conn.execute(
                        """
                        UPDATE releases
                        SET paid_conversions = paid_conversions + 1,
                            conversion_rate = CASE
                                WHEN installs > 0 THEN ROUND(((paid_conversions + 1) * 100.0) / installs, 2)
                                ELSE 0.0
                            END,
                            gross_usd = ROUND(gross_usd + ?, 2),
                            fees_usd = ROUND(fees_usd + ?, 2),
                            net_usd = ROUND(net_usd + ?, 2)
                        WHERE version = ?
                        """,
                        (usd_gross, usd_fee, usd_net, effective_release),
                    )
                    # Update today's row in daily_metrics (or insert on clean board) so 14-day sparkline & bar chart update instantaneously
                    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    existing_day = conn.execute(
                        "SELECT day_date FROM daily_metrics WHERE day_date = ?",
                        (today_str,),
                    ).fetchone()
                    if existing_day:
                        conn.execute(
                            """
                            UPDATE daily_metrics
                            SET paid_orders = paid_orders + 1,
                                gross_usd = ROUND(gross_usd + ?, 2),
                                fees_usd = ROUND(fees_usd + ?, 2),
                                net_usd = ROUND(net_usd + ?, 2),
                                release_version = ?
                            WHERE day_date = ?
                            """,
                            (usd_gross, usd_fee, usd_net, effective_release, today_str),
                        )
                    else:
                        max_day_row = conn.execute(
                            "SELECT MAX(day_date) AS max_d FROM daily_metrics WHERE day_date <= ?",
                            (today_str,),
                        ).fetchone()
                        seeded_flag = conn.execute(
                            "SELECT value FROM meta WHERE key = 'last_seeded_at'"
                        ).fetchone()
                        if max_day_row and max_day_row["max_d"] and seeded_flag is not None and not self.is_clean_mode():
                            conn.execute(
                                """
                                UPDATE daily_metrics
                                SET paid_orders = paid_orders + 1,
                                    gross_usd = ROUND(gross_usd + ?, 2),
                                    fees_usd = ROUND(fees_usd + ?, 2),
                                    net_usd = ROUND(net_usd + ?, 2)
                                WHERE day_date = ?
                                """,
                                (usd_gross, usd_fee, usd_net, max_day_row["max_d"]),
                            )
                        else:
                            conn.execute(
                                """
                                INSERT INTO daily_metrics (
                                    day_date, installs, paid_orders, gross_usd, fees_usd, net_usd, disputes, release_version
                                ) VALUES (?, 1, 1, ?, ?, ?, 0, ?)
                                """,
                                (today_str, usd_gross, usd_fee, usd_net, effective_release),
                            )
            elif is_paid and str(existing["status"] or "").upper() not in PAID_TRANSACTION_STATUSES:
                # A pending order became captured. Apply the revenue now.
                self._apply_paid_rollup(
                    conn,
                    effective_release,
                    usd_gross,
                    usd_fee,
                    usd_net,
                    created_at[:10],
                )
            conn.commit()

        return {
            "paypal_id": order.order_id,
            "gross": order.amount,
            "fee": fee,
            "net": net,
            "currency": cur,
            "usd_net": usd_net,
            "release_version": effective_release,
            "approve_url": order.approve_url,
        }

    def update_order_status(
        self,
        paypal_id: str,
        status: str,
        amount: float = 0.0,
        currency: str = "USD",
        release_version: str = "v1.2.1",
        platform_sku: str = "Captured Order",
    ) -> bool:
        """Update transaction status or upsert captured order into ledger."""
        new_status = str(status or "").upper()
        is_paid = new_status in PAID_TRANSACTION_STATUSES
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT status, release_version, usd_gross, usd_fee, usd_net, created_at "
                "FROM transactions WHERE paypal_id = ?",
                (paypal_id,),
            ).fetchone()
            cursor = conn.execute(
                "UPDATE transactions SET status = ? WHERE paypal_id = ?",
                (status, paypal_id),
            )
            if cursor.rowcount > 0:
                was_paid = bool(existing) and str(
                    existing["status"] or ""
                ).upper() in PAID_TRANSACTION_STATUSES
                if existing and is_paid and not was_paid:
                    self._apply_paid_rollup(
                        conn,
                        existing["release_version"] or release_version,
                        float(existing["usd_gross"] or 0.0),
                        float(existing["usd_fee"] or 0.0),
                        float(existing["usd_net"] or 0.0),
                        str(existing["created_at"] or "")[:10],
                    )
                conn.commit()
                return True

            # If not previously recorded, upsert captured order into transactions
            if amount > 0:
                cur = currency.upper()
                fx = FX_TO_USD.get(cur, 1.0)
                fee = compute_paypal_fee(amount, cur, cross_border=True)
                net = round(amount - fee, 2)
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    """
                    INSERT INTO transactions (
                        paypal_id, created_at, gross_amount, fee_amount, net_amount,
                        currency, usd_gross, usd_fee, usd_net, release_version,
                        country_code, product_name, platform_sku, status,
                        is_live_sandbox, approve_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '')
                    ON CONFLICT(paypal_id) DO UPDATE SET status = excluded.status
                    """,
                    (
                        paypal_id,
                        now_str,
                        amount,
                        fee,
                        net,
                        cur,
                        round(amount * fx, 2),
                        round(fee * fx, 2),
                        round(net * fx, 2),
                        release_version,
                        "US",
                        "PayPal Checkout Order",
                        platform_sku,
                        status,
                    ),
                )
                if is_paid:
                    self._apply_paid_rollup(
                        conn,
                        release_version,
                        round(amount * fx, 2),
                        round(fee * fx, 2),
                        round(net * fx, 2),
                        now_str[:10],
                    )
                conn.commit()
                return True
            conn.commit()
            return False

    def record_webhook_event(self, event_data: dict[str, Any]) -> dict[str, Any]:
        """Ingest a PayPal Webhook event payload (live or triggered) into the SQLite ledger."""
        event_type = event_data.get("event_type", "").upper().replace("_", ".")
        resource = event_data.get("resource", {})

        ctx = detect_project_context()
        default_rel = ctx.get("version") or "v1.2.1"
        default_sku = ctx.get("default_sku") or "PulseCore Pro"
        default_cur = ctx.get("default_currency") or "USD"

        if event_type in ("PAYMENT.CAPTURE.COMPLETED", "CHECKOUT.ORDER.APPROVED"):
            purchase_units = resource.get("purchase_units") or []
            purchase_unit = purchase_units[0] if purchase_units else {}
            related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
            order_id = (
                related.get("order_id")
                or resource.get("id")
                or f"ORD-WH-{int(time.time())}"
            )
            breakdown = resource.get("seller_receivable_breakdown") or {}
            amount_dict = (
                resource.get("amount")
                or (purchase_unit.get("amount") or {})
                or resource.get("dispute_amount")
                or {}
            )
            if not amount_dict and breakdown:
                amount_dict = breakdown.get("gross_amount", {})
            amt = float(amount_dict.get("value", ctx.get("default_price", 19.99)))
            cur = str(amount_dict.get("currency_code", default_cur)).upper()

            def _breakdown_value(key: str) -> float | None:
                raw = (breakdown.get(key) or {}).get("value")
                if raw is None:
                    return None
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return None

            fee_override = _breakdown_value("paypal_fee")
            net_override = _breakdown_value("net_amount")
            raw_custom = resource.get("custom_id") or purchase_unit.get("custom_id")
            release_ver = parse_release_version(raw_custom, default=default_rel)
            parts = str(raw_custom).split("|") if raw_custom else []
            country = parts[1] if len(parts) > 1 and parts[1] else "US"
            sku = parts[2] if len(parts) > 2 and parts[2] else default_sku

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
                fee_override=fee_override,
                net_override=net_override,
            )
        elif event_type == "CUSTOMER.DISPUTE.CREATED":
            dispute_id = resource.get("dispute_id", f"PP-D-{int(time.time())}-SIM")
            amount_dict = resource.get("dispute_amount", {})
            amt = float(amount_dict.get("value", 29.99))
            cur = str(amount_dict.get("currency_code", default_cur)).upper()
            buyer_country = resource.get("buyer_country", "US")
            buyer_email = resource.get("buyer_email", f"buyer-{dispute_id.lower()}@example.com")
            raw_custom = resource.get("custom_id", default_sku)
            release_ver = resource.get("release_version") or parse_release_version(raw_custom, default=default_rel)
            parts = str(raw_custom).split("|") if raw_custom else []
            sku = parts[2] if len(parts) > 2 and parts[2] else str(raw_custom)
            desc = resource.get("description", "Dispute triggered via webhook event.")
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            due_str = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")

            with self._connect() as conn:
                existing_disp = conn.execute(
                    "SELECT status FROM disputes WHERE dispute_id = ?", (dispute_id,)
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO disputes (
                        dispute_id, paypal_transaction_id, status, buyer_country,
                        buyer_email, release_version, platform_sku, amount, currency,
                        due_date, reason, description, created_at
                    ) VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED', ?, ?)
                    ON CONFLICT(dispute_id) DO UPDATE SET
                        status = 'OPEN',
                        release_version = excluded.release_version,
                        amount = excluded.amount
                    """,
                    (dispute_id, f"TX-{dispute_id}", buyer_country, buyer_email, release_ver, sku, amt, cur, due_str, desc, now_str),
                )
                if not existing_disp or existing_disp["status"] != "OPEN":
                    cur_rel = conn.execute("SELECT version FROM releases WHERE version = ?", (release_ver,)).fetchone()
                    if not cur_rel:
                        now_ymd = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        tag_meta = self._discover_git_tag_metadata().get(release_ver, {})
                        commit_sha = tag_meta.get("commit", "HEAD")
                        rel_date = tag_meta.get("date", now_ymd)
                        rel_title = (
                            f"{release_ver} — {tag_meta['subject']}"
                            if tag_meta.get("subject")
                            else f"{release_ver} (Release)"
                        )
                        conn.execute(
                            """
                            INSERT INTO releases (
                                version, title, released_at, git_commit, installs, active_users,
                                trial_starts, paid_conversions, conversion_rate, gross_usd,
                                fees_usd, net_usd, refund_count, dispute_count, status_badge, ai_diagnostic
                            ) VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, 0, 'DISPUTE RISK', ?)
                            """,
                            (
                                release_ver,
                                rel_title,
                                rel_date,
                                commit_sha,
                                f"Alert: Open buyer dispute ({dispute_id}) recorded on {release_ver}.",
                            ),
                        )
                    conn.execute(
                        """
                        UPDATE releases
                        SET dispute_count = dispute_count + 1,
                            status_badge = 'DISPUTE RISK',
                            ai_diagnostic = ?
                        WHERE version = ?
                        """,
                        (f"Alert: Active buyer dispute ({dispute_id}) detected on release {release_ver}.", release_ver),
                    )
                    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    existing_day = conn.execute(
                        "SELECT day_date FROM daily_metrics WHERE day_date = ?",
                        (today_str,),
                    ).fetchone()
                    if existing_day:
                        conn.execute(
                            "UPDATE daily_metrics SET disputes = disputes + 1 WHERE day_date = ?",
                            (today_str,),
                        )
                    else:
                        seeded_flag = conn.execute("SELECT value FROM meta WHERE key = 'last_seeded_at'").fetchone()
                        max_day_row = conn.execute(
                            "SELECT MAX(day_date) AS max_d FROM daily_metrics WHERE day_date <= ?",
                            (today_str,),
                        ).fetchone()
                        if max_day_row and max_day_row["max_d"] and seeded_flag is not None and not self.is_clean_mode():
                            conn.execute(
                                "UPDATE daily_metrics SET disputes = disputes + 1 WHERE day_date = ?",
                                (max_day_row["max_d"],),
                            )
                        else:
                            conn.execute(
                                """
                                INSERT INTO daily_metrics (
                                    day_date, installs, paid_orders, gross_usd, fees_usd, net_usd, disputes, release_version
                                ) VALUES (?, 0, 0, 0.0, 0.0, 0.0, 1, ?)
                                """,
                                (today_str, release_ver),
                            )
                conn.commit()
            return {
                "dispute_id": dispute_id,
                "status": "OPEN",
                "amount": amt,
                "currency": cur,
                "release_version": release_ver,
            }
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
        """Seed 30 days of universal software & business history across 3 Git releases + Sandbox orders."""
        self.set_clean_mode(False)
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

    def get_currency_breakdown(
        self,
        statuses: tuple[str, ...] | None = None,
        since_days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return multi-currency gross, fee drag, and net take-home breakdown.

        When ``statuses`` is given, only transactions in those statuses are
        summed. Pass PAID_TRANSACTION_STATUSES for revenue, and
        PENDING_TRANSACTION_STATUSES for orders that are not yet captured.
        When ``since_days`` is given, only transactions created inside that
        window are summed. The dashboard labels its totals as 30-day, so the
        caller must pass 30 to make that label true.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"UPPER(status) IN ({placeholders})")
            params.extend(s.upper() for s in statuses)
        if since_days is not None:
            clauses.append("date(created_at) >= date('now', ?)")
            params.append(f"-{int(since_days)} days")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
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
                {where}
                GROUP BY currency
                ORDER BY usd_gross DESC
                """,
                tuple(params),
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
            # Aggregate actual live transactions by release_version to enrich metrics
            txn_stats = {
                r["release_version"]: dict(r)
                for r in conn.execute(
                    """
                    SELECT release_version,
                           SUM(CASE WHEN UPPER(status) IN ('COMPLETED', 'CAPTURED', 'DISPUTED') THEN 1 ELSE 0 END) as paid_count,
                           ROUND(SUM(usd_gross), 2) as sum_gross,
                           ROUND(SUM(usd_fee), 2) as sum_fee,
                           ROUND(SUM(usd_net), 2) as sum_net
                    FROM transactions
                    WHERE UPPER(status) IN ('COMPLETED', 'CAPTURED', 'DISPUTED', 'REFUNDED', 'REVERSED')
                    GROUP BY release_version
                    """
                ).fetchall()
            }
            # Aggregate open disputes by release_version
            dispute_stats = {
                r["release_version"]: int(r["open_cnt"] or 0)
                for r in conn.execute(
                    """
                    SELECT release_version, COUNT(*) as open_cnt
                    FROM disputes
                    WHERE UPPER(status) = 'OPEN'
                    GROUP BY release_version
                    """
                ).fetchall()
            }

        tag_meta_map = self._discover_git_tag_metadata() if include_git_tags else {}
        seeded_ledger = bool(self.get_meta("last_seeded_at", ""))
        always_reconcile = not seeded_ledger
        releases = [dict(r) for r in rows]
        for rel in releases:
            ver = rel["version"]
            # Enrich git commit and release date from local Git tag metadata if currently generic
            if ver in tag_meta_map:
                t_meta = tag_meta_map[ver]
                if rel.get("git_commit") in (None, "", "HEAD") and t_meta.get("commit"):
                    rel["git_commit"] = t_meta["commit"]
                if t_meta.get("date"):
                    rel["released_at"] = t_meta["date"]
                if t_meta.get("subject") and rel.get("title", "").endswith("(Release)"):
                    rel["title"] = f"{ver} — {t_meta['subject']}"

            # Reconcile with the transaction ledger. A real (non-sample) ledger is
            # always reconciled so a status change from CREATED to COMPLETED is
            # reflected. A seeded sample ledger keeps its synthetic rollups.
            stats = txn_stats.get(ver)
            if always_reconcile or (
                stats is not None and float(rel["gross_usd"] or 0.0) == 0.0
            ):
                rel["gross_usd"] = float((stats or {}).get("sum_gross") or 0.0)
                rel["fees_usd"] = float((stats or {}).get("sum_fee") or 0.0)
                rel["net_usd"] = float((stats or {}).get("sum_net") or 0.0)
                rel["paid_conversions"] = int((stats or {}).get("paid_count") or 0)
                # Never invent an install base. Without install telemetry the
                # conversion rate is unknown, not 100%.
                rel["conversion_rate"] = (
                    round((rel["paid_conversions"] * 100.0) / int(rel["installs"]), 2)
                    if int(rel["installs"] or 0) > 0
                    else 0.0
                )
                if stats is not None and rel["status_badge"] == "GIT TAG":
                    rel["status_badge"] = "ACTIVE"
                    rel["ai_diagnostic"] = "Active release receiving live PayPal transactions."

            # Reconcile open dispute counts from disputes table
            if ver in dispute_stats:
                rel["dispute_count"] = max(int(rel.get("dispute_count") or 0), dispute_stats[ver])
                if rel["dispute_count"] > 0 and rel.get("status_badge") in ("ACTIVE", "GIT TAG"):
                    rel["status_badge"] = "DISPUTE RISK"
                    rel["ai_diagnostic"] = (
                        f"Alert: {rel['dispute_count']} open buyer dispute(s) detected on release {ver}."
                    )

            gross = float(rel["gross_usd"] or 0.0)
            net = float(rel["net_usd"] or 0.0)
            fees = float(rel["fees_usd"] or 0.0)
            rel["net_margin_pct"] = round((net / gross) * 100, 1) if gross > 0 else 0.0
            rel["fee_drag_pct"] = round((fees / gross) * 100, 1) if gross > 0 else 0.0

        if include_git_tags:
            known_versions = {r["version"] for r in releases}
            for tag in self._discover_git_tags():
                if tag not in known_versions:
                    tag_stats = txn_stats.get(tag)
                    disp_cnt = dispute_stats.get(tag, 0)
                    t_meta = tag_meta_map.get(tag, {})
                    t_commit = t_meta.get("commit", "HEAD")
                    t_date = t_meta.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                    t_subj = t_meta.get("subject", "")
                    if tag_stats or disp_cnt > 0:
                        t_gross = float(tag_stats["sum_gross"] or 0.0) if tag_stats else 0.0
                        t_fees = float(tag_stats["sum_fee"] or 0.0) if tag_stats else 0.0
                        t_net = float(tag_stats["sum_net"] or 0.0) if tag_stats else 0.0
                        t_paid = int(tag_stats["paid_count"] or 0) if tag_stats else 0
                        badge = "DISPUTE RISK" if disp_cnt > 0 else ("ACTIVE" if t_gross > 0 else "GIT TAG")
                        diag = (
                            f"Alert: {disp_cnt} open buyer dispute(s) detected on release {tag}."
                            if disp_cnt > 0
                            else (
                                "Local Git release with live PayPal transaction attribution."
                                if t_gross > 0
                                else "Detected local Git tag in repository awaiting telemetry sync."
                            )
                        )
                        releases.append(
                            {
                                "version": tag,
                                "title": f"{tag} — {t_subj}" if t_subj else f"{tag} (Local Git Release)",
                                "released_at": t_date,
                                "git_commit": t_commit,
                                "installs": 0,
                                "active_users": 0,
                                "trial_starts": 0,
                                "paid_conversions": t_paid,
                                "conversion_rate": 0.0,
                                "gross_usd": t_gross,
                                "fees_usd": t_fees,
                                "net_usd": t_net,
                                "net_margin_pct": round((t_net / t_gross) * 100, 1) if t_gross > 0 else 0.0,
                                "fee_drag_pct": round((t_fees / t_gross) * 100, 1) if t_gross > 0 else 0.0,
                                "refund_count": 0,
                                "dispute_count": disp_cnt,
                                "status_badge": badge,
                                "ai_diagnostic": diag,
                            }
                        )
                    else:
                        releases.append(
                            {
                                "version": tag,
                                "title": f"{tag} — {t_subj}" if t_subj else f"{tag} (Local Git Tag)",
                                "released_at": t_date,
                                "git_commit": t_commit,
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

    def _discover_git_tag_metadata(self) -> dict[str, dict[str, str]]:
        """Discover local Git tags along with their commit SHA, creation date, and commit subject."""
        if hasattr(self, "_git_tag_meta_cache") and self._git_tag_meta_cache is not None:
            return self._git_tag_meta_cache

        meta: dict[str, dict[str, str]] = {}
        search_dirs = [Path.cwd(), PACKAGE_ROOT]

        for d in search_dirs:
            try:
                proc_check = subprocess.run(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    cwd=str(d),
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                if proc_check.returncode != 0 or proc_check.stdout.strip() != "true":
                    continue

                proc = subprocess.run(
                    [
                        "git",
                        "for-each-ref",
                        "--sort=creatordate",
                        "--format=%(refname:short)|%(if)%(*objectname:short)%(then)%(*objectname:short)%(else)%(objectname:short)%(end)|%(creatordate:short)|%(contents:subject)",
                        "refs/tags",
                    ],
                    cwd=str(d),
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    for line in proc.stdout.splitlines():
                        line_str = line.strip()
                        if not line_str:
                            continue
                        parts = line_str.split("|", 3)
                        tag_name = parts[0].strip()
                        commit_sha = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "HEAD"
                        tag_date = parts[2].strip() if len(parts) > 2 and parts[2].strip() else ""
                        subject = parts[3].strip() if len(parts) > 3 and parts[3].strip() else ""
                        if tag_name and tag_name not in meta:
                            meta[tag_name] = {
                                "commit": commit_sha,
                                "date": tag_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                                "subject": subject,
                            }
                # If we are inside an active Git repo in Path.cwd(), do not mix in PACKAGE_ROOT tags
                break
            except Exception:
                pass

        self._git_tag_meta_cache = meta
        return self._git_tag_meta_cache

    def _discover_git_tags(self) -> list[str]:
        if hasattr(self, "_git_tags_cache") and self._git_tags_cache is not None:
            return self._git_tags_cache

        meta = self._discover_git_tag_metadata()
        # Only real tags count. `git describe --always` returns a bare commit
        # hash when the repository has no tags, which would render a SHA as a
        # release in the timeline.
        tags: list[str] = list(meta.keys())

        self._git_tags_cache = tags
        return self._git_tags_cache

    def get_disputes(self, status_filter: str | None = None) -> list[dict[str, Any]]:
        """Return disputes from local dual-engine ledger."""
        with self._connect() as conn:
            if status_filter:
                if status_filter.upper() == "OPEN":
                    placeholders = ", ".join("?" for _ in OPEN_DISPUTE_STATUSES)
                    rows = conn.execute(
                        f"SELECT * FROM disputes WHERE UPPER(status) IN ({placeholders}) ORDER BY created_at DESC",
                        OPEN_DISPUTE_STATUSES,
                    ).fetchall()
                else:
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
        new_status: str | None = None,
    ) -> bool:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT release_version, status FROM disputes WHERE UPPER(dispute_id) = UPPER(?)",
                (dispute_id.strip(),),
            ).fetchone()
            if not row:
                return False
            rel_ver = row["release_version"]
            current_status = row["status"]

            # Gateway fidelity: submitting evidence does not equate to dispute adjudication.
            # Mock / demonstration fixture disputes (PP-D-) resolve immediately on remediation.
            # Real disputes (PP-R-) reflect gateway reality: submitting evidence sets status to
            # WAITING_FOR_BUYER_RESPONSE or UNDER_REVIEW, and only becomes RESOLVED when PayPal confirms.
            if dispute_id.strip().upper().startswith("PP-D-"):
                target_status = "RESOLVED"
            elif new_status:
                target_status = new_status
            elif current_status.upper() in ("RESOLVED", "CLOSED", "CANCELLED"):
                target_status = current_status
            else:
                target_status = "UNDER_REVIEW"

            cur = conn.execute(
                """
                UPDATE disputes
                SET status = ?,
                    ai_evidence_draft = ?,
                    resolution_order_id = ?,
                    resolved_at = ?
                WHERE UPPER(dispute_id) = UPPER(?)
                """,
                (target_status, ai_evidence, resolution_order_id, now_str, dispute_id.strip()),
            )
            open_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM disputes WHERE release_version = ? AND UPPER(status) NOT IN ('RESOLVED', 'CLOSED', 'CANCELLED')",
                (rel_ver,),
            ).fetchone()
            open_cnt = int(open_row["cnt"]) if open_row else 0

            # Query if there are open country-specific disputes (e.g. DE)
            de_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM disputes WHERE release_version = ? AND UPPER(status) NOT IN ('RESOLVED', 'CLOSED', 'CANCELLED') AND UPPER(buyer_country) = 'DE'",
                (rel_ver,),
            ).fetchone()
            de_cnt = int(de_row["cnt"]) if de_row else 0
            badge_details = f"{de_cnt} DE" if de_cnt > 0 else f"{open_cnt} OPEN"

            resolved_label = "RESOLVED (PATCH v1.2.2)" if rel_ver == "v1.2.1" else f"RESOLVED ({rel_ver})"
            new_badge = (
                f"DISPUTE ALERT ({badge_details})"
                if open_cnt > 0
                else resolved_label
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
        new_status: str | None = None,
    ) -> bool:
        """Alias for mark_dispute_resolved with flexible keyword arguments."""
        return self.mark_dispute_resolved(
            dispute_id=dispute_id,
            ai_evidence=ai_evidence,
            resolution_order_id=replacement_order_id,
            new_status=new_status,
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
        api_latency_ms: float = 0.0,
        api_connected: bool = False,
    ) -> dict[str, Any]:
        """Compute complete KPI summary for `pp pulse`, `pp ask`, and `pp tui`.

        Gateway telemetry defaults to disconnected. Callers that verified the
        API must pass the measured latency and connection state.
        """
        currencies = self.get_currency_breakdown(
            statuses=REVENUE_TRANSACTION_STATUSES, since_days=30
        )
        pending_currencies = self.get_currency_breakdown(
            statuses=PENDING_TRANSACTION_STATUSES, since_days=30
        )
        releases = self.get_releases(include_git_tags=False)
        disputes = self.get_disputes()
        open_disputes = [
            d for d in disputes
            if str(d.get("status", "")).upper() not in ("RESOLVED", "CLOSED", "CANCELLED")
        ]
        daily_14 = self.get_recent_daily_metrics(days=14)
        live_txs = self.get_live_sandbox_transactions(limit=10)

        total_gross_usd = round(sum(c["usd_gross"] for c in currencies), 2)
        total_fees_usd = round(sum(c["usd_fee"] for c in currencies), 2)
        total_net_usd = round(sum(c["usd_net"] for c in currencies), 2)
        total_pending_orders = sum(int(c["tx_count"]) for c in pending_currencies)
        pending_gross_usd = round(
            sum(c["usd_gross"] for c in pending_currencies), 2
        )
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

        # 30-day net run-rate. Pulse stores no subscription or interval data, so
        # this is the trailing net sum, not a recurring-revenue figure.
        net_run_rate_30d_usd = round(total_net_usd, 2)

        state = self.detect_state()
        health_telemetry = self.compute_merchant_health(
            open_disputes_count=len(open_disputes),
            total_orders=total_paid,
            net_margin_pct=net_margin_pct,
            latest_release_disputes=(releases[-1]["dispute_count"] if releases else 0),
            api_latency_ms=api_latency_ms,
            api_connected=api_connected,
        )
        health_score = health_telemetry["composite_score"]
        health_rating = health_telemetry["rating"]
        if state == "fresh":
            # A ledger with no rows has no measurable health. Any score or
            # sub-score here would imply a baseline that does not exist.
            health_score = 0.0
            health_rating = "NO DATA"
            health_telemetry = {}

        return {
            "state": state,
            "db_path": str(self.db_path),
            "next_actions": self.next_actions(),
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
            "total_orders": total_paid,
            "total_paid": total_paid,
            "total_pending_orders": total_pending_orders,
            "pending_gross_usd": pending_gross_usd,
            "pending_currencies": pending_currencies,
            "overall_conversion_pct": overall_conv,
            "net_run_rate_30d_usd": net_run_rate_30d_usd,
            "health_score": health_score,
            "health_rating": health_rating,
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
        api_latency_ms: float = 0.0,
        api_connected: bool = False,
    ) -> dict[str, Any]:
        """Compute the local Merchant Health Index across four weighted sub-scores."""
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
        amount: float | None = None,
        currency: str | None = None,
        platform_sku: str | None = None,
        release_version: str | None = None,
        reason: str | None = None,
        buyer_country: str = "DE",
    ) -> dict[str, Any]:
        """Simulate an incoming real-world chargeback event to test live dispute and Merchant Health workflows."""
        ctx = detect_project_context()
        has_cfg = bool(ctx.get("config_file"))
        eff_amount = amount if amount is not None else (float(ctx.get("default_price", 19.99)) if has_cfg else 5.99)
        eff_currency = (currency or (ctx.get("default_currency", "USD") if has_cfg else "EUR")).upper()
        eff_sku = platform_sku or (ctx.get("default_sku") if has_cfg else "Ubuntu Linux 24.04 (CLI)")
        eff_rel = release_version or (ctx.get("version") if has_cfg else "v1.2.1")

        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        due_date = (now + timedelta(days=7)).strftime("%Y-%m-%d")
        rand_suffix = uuid.uuid4().hex[:5].upper()
        dispute_id = f"PP-D-SIM-{rand_suffix}"

        reasons = [
            "License activation token failed with 504 Gateway Timeout during checkout unlock",
            "Customer claims recurring seat billed without localized VAT receipt attachment",
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
                    eff_sku,
                    eff_rel,
                    eff_amount,
                    eff_currency,
                    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    desc,
                    due_date,
                ),
            )
            cur_rel = conn.execute("SELECT version FROM releases WHERE version = ?", (eff_rel,)).fetchone()
            if cur_rel:
                conn.execute(
                    """
                    UPDATE releases
                    SET dispute_count = dispute_count + 1,
                        status_badge = 'DISPUTE ALERT (ACTIVE)'
                    WHERE version = ?
                    """,
                    (eff_rel,),
                )
            else:
                tag_meta = self._discover_git_tag_metadata().get(eff_rel, {})
                commit_sha = tag_meta.get("commit", "HEAD")
                rel_date = tag_meta.get("date", now.strftime("%Y-%m-%d"))
                rel_title = f"{eff_rel} — {tag_meta['subject']}" if tag_meta.get("subject") else f"{eff_sku} ({eff_rel})"
                conn.execute(
                    """
                    INSERT INTO releases (
                        version, title, released_at, git_commit, installs, active_users,
                        trial_starts, paid_conversions, conversion_rate, gross_usd,
                        fees_usd, net_usd, refund_count, dispute_count, status_badge, ai_diagnostic
                    ) VALUES (?, ?, ?, ?, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, 1, 'DISPUTE ALERT (ACTIVE)', ?)
                    """,
                    (
                        eff_rel,
                        rel_title,
                        rel_date,
                        commit_sha,
                        f"Alert: Active buyer dispute ({dispute_id}) detected on release {eff_rel}.",
                    ),
                )
            # Increment today's daily_metrics dispute count
            today_str = now.strftime("%Y-%m-%d")
            day_row = conn.execute("SELECT day_date FROM daily_metrics WHERE day_date = ?", (today_str,)).fetchone()
            if day_row:
                conn.execute("UPDATE daily_metrics SET disputes = disputes + 1 WHERE day_date = ?", (today_str,))
            else:
                max_day = conn.execute("SELECT MAX(day_date) AS max_d FROM daily_metrics WHERE day_date <= ?", (today_str,)).fetchone()
                if max_day and max_day["max_d"] and not self.is_clean_mode():
                    conn.execute("UPDATE daily_metrics SET disputes = disputes + 1 WHERE day_date = ?", (max_day["max_d"],))
                else:
                    conn.execute(
                        """
                        INSERT INTO daily_metrics (
                            day_date, installs, paid_orders, gross_usd, fees_usd, net_usd, disputes, release_version
                        ) VALUES (?, 0, 0, 0.0, 0.0, 0.0, 1, ?)
                        """,
                        (today_str, eff_rel),
                    )
            conn.commit()

        return self.get_dispute_by_id(dispute_id) or {}

    def sync_external_disputes(self, external_items: list[dict[str, Any]]) -> int:
        """Upsert real PayPal Sandbox disputes fetched from GET /v1/customer/disputes (and /disputes/{id})."""
        if not external_items:
            return 0
        added = 0
        with self._connect() as conn:
            for item in external_items:
                disp_id = item.get("dispute_id", "")
                if not disp_id:
                    continue
                dtx_list = item.get("disputed_transactions") or [{}]
                dtx = dtx_list[0] if dtx_list else {}
                seller_tx_id = dtx.get("seller_transaction_id", "")
                buyer_tx_id = dtx.get("buyer_transaction_id", "")
                tx_id = seller_tx_id or buyer_tx_id or "PP-SANDBOX-TX"
                dispute_amount = float(
                    item.get("dispute_amount", {}).get("value", 5.99)
                )
                curr = item.get("dispute_amount", {}).get("currency_code", "USD")
                reason = item.get("reason", "MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
                status = item.get("status", "OPEN")
                create_time = item.get("create_time", datetime.now(timezone.utc).isoformat())
                due_date_raw = item.get("seller_response_due_date", "")
                due_date = due_date_raw[:10] if due_date_raw else (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")

                matched_tx = conn.execute(
                    "SELECT release_version, platform_sku, country_code FROM transactions WHERE paypal_id IN (?, ?)",
                    (seller_tx_id or tx_id, buyer_tx_id or tx_id),
                ).fetchone()

                # Parse custom attribution string (e.g. "release:v0.1.0|US|PulseCore Pro") and item details
                custom_raw = str(dtx.get("custom", "") or "").strip()
                custom_parts = [p.strip() for p in custom_raw.split("|") if p.strip()]
                custom_rel = ""
                custom_country = ""
                custom_sku = ""
                for part in custom_parts:
                    if part.lower().startswith("release:"):
                        custom_rel = part.split(":", 1)[1].strip()
                    elif len(part) == 2 and part.isalpha() and part.isupper():
                        custom_country = part
                    elif not custom_rel and (part.startswith("v") or "." in part):
                        custom_rel = part
                    else:
                        custom_sku = part

                dtx_items = dtx.get("items") or [{}]
                first_item = dtx_items[0] if dtx_items else {}
                item_name = str(first_item.get("item_name", "") or "").strip()

                ctx = detect_project_context()
                rel_ver = (
                    matched_tx["release_version"]
                    if matched_tx
                    else (custom_rel or ctx.get("version") or "v1.2.1")
                )
                sku = (
                    matched_tx["platform_sku"]
                    if matched_tx
                    else (item_name or custom_sku or ctx.get("default_sku") or "Universal Platform")
                )
                country = (
                    matched_tx["country_code"]
                    if matched_tx
                    else (custom_country or "US")
                )

                buyer_obj = dtx.get("buyer") or {}
                buyer_email = str(buyer_obj.get("email", "") or "").strip() or "sandbox-buyer@example.com"
                buyer_name = str(buyer_obj.get("name", "") or "").strip()

                # Reconcile associated transaction in local ledger if missing
                if not matched_tx and tx_id and tx_id != "PP-SANDBOX-TX" and dispute_amount > 0:
                    tx_fx = FX_TO_USD.get(curr, 1.0)
                    tx_fee = compute_paypal_fee(dispute_amount, curr, cross_border=(country != "US"))
                    tx_net = round(dispute_amount - tx_fee, 2)
                    conn.execute(
                        """
                        INSERT INTO transactions (
                            paypal_id, created_at, gross_amount, fee_amount, net_amount,
                            currency, usd_gross, usd_fee, usd_net, release_version,
                            country_code, product_name, platform_sku, status,
                            is_live_sandbox, approve_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '')
                        ON CONFLICT(paypal_id) DO NOTHING
                        """,
                        (
                            tx_id,
                            create_time,
                            dispute_amount,
                            tx_fee,
                            tx_net,
                            curr,
                            round(dispute_amount * tx_fx, 2),
                            round(tx_fee * tx_fx, 2),
                            round(tx_net * tx_fx, 2),
                            rel_ver,
                            country,
                            item_name or f"{sku} ({rel_ver})",
                            sku,
                            "COMPLETED",
                        ),
                    )

                # Extract buyer conversation thread and evidence notes
                buyer_notes: list[str] = []
                for msg in item.get("messages") or []:
                    if str(msg.get("posted_by", "")).upper() == "BUYER":
                        content = str(msg.get("content", "") or "").strip()
                        if content and content not in buyer_notes:
                            buyer_notes.append(content)
                for ev in item.get("evidences") or []:
                    if str(ev.get("source", "")).upper() == "SUBMITTED_BY_BUYER":
                        note = str(ev.get("notes", "") or "").strip()
                        if note and note not in buyer_notes:
                            buyer_notes.append(note)

                stage = str(item.get("dispute_life_cycle_stage", "") or "").strip()
                if buyer_notes:
                    joined_notes = " | ".join(buyer_notes)
                    who = f"Buyer ({buyer_name})" if buyer_name else "Buyer"
                    stage_suffix = f" [Stage: {stage}]" if stage else ""
                    description = f'{who}: "{joined_notes}"{stage_suffix}'
                else:
                    description = f"Live Sandbox Dispute: {reason}"

                # Check if seller already submitted evidence or messages upstream
                seller_notes: list[str] = []
                for ev in item.get("evidences") or []:
                    if str(ev.get("source", "")).upper() == "SUBMITTED_BY_SELLER":
                        s_note = str(ev.get("notes", "") or "").strip()
                        if s_note and s_note not in seller_notes:
                            seller_notes.append(s_note)
                for msg in item.get("messages") or []:
                    if str(msg.get("posted_by", "")).upper() == "SELLER":
                        s_content = str(msg.get("content", "") or "").strip()
                        if s_content and s_content not in seller_notes:
                            seller_notes.append(s_content)
                upstream_seller_evidence = "\n\n".join(seller_notes)

                exists = conn.execute(
                    "SELECT status, resolved_at, ai_evidence_draft, description, buyer_email, platform_sku FROM disputes WHERE dispute_id = ?",
                    (disp_id,),
                ).fetchone()
                if not exists:
                    conn.execute(
                        """
                        INSERT INTO disputes (
                            dispute_id, paypal_transaction_id, created_at, buyer_country,
                            buyer_email, platform_sku, release_version, amount, currency,
                            reason, description, status, due_date, ai_evidence_draft
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            disp_id,
                            tx_id,
                            create_time,
                            country,
                            buyer_email,
                            sku,
                            rel_ver,
                            dispute_amount,
                            curr,
                            reason,
                            description,
                            status,
                            due_date,
                            upstream_seller_evidence,
                        ),
                    )
                    added += 1
                else:
                    effective_status = status  # Authoritative PayPal gateway status; never falsely override with local resolved_at
                    new_desc = description if (buyer_notes or str(exists["description"]).startswith("Live Sandbox Dispute:")) else exists["description"]
                    new_email = buyer_email if buyer_email != "sandbox-buyer@example.com" else exists["buyer_email"]
                    new_sku = sku if sku != "Universal Platform" else exists["platform_sku"]
                    new_evidence = exists["ai_evidence_draft"] or upstream_seller_evidence
                    conn.execute(
                        """
                        UPDATE disputes
                        SET status = ?, due_date = ?, amount = ?, currency = ?,
                            paypal_transaction_id = ?, buyer_country = ?, buyer_email = ?,
                            platform_sku = ?, release_version = ?, description = ?,
                            ai_evidence_draft = ?
                        WHERE dispute_id = ?
                        """,
                        (
                            effective_status,
                            due_date,
                            dispute_amount,
                            curr,
                            tx_id,
                            country,
                            new_email,
                            new_sku,
                            rel_ver,
                            new_desc,
                            new_evidence,
                            disp_id,
                        ),
                    )
            conn.commit()
        return added

    def sync_external_transactions(self, external_tx_details: list[dict[str, Any]]) -> int:
        """Upsert real PayPal Sandbox transactions fetched from GET /v1/reporting/transactions."""
        if not external_tx_details:
            return 0
        added = 0
        ctx = detect_project_context()
        default_rel = ctx.get("version") or "v1.2.1"
        default_sku = ctx.get("default_sku") or "PulseCore Pro"

        with self._connect() as conn:
            for item in external_tx_details:
                tx_info = item.get("transaction_info", {})
                tx_id = tx_info.get("transaction_id", "")
                if not tx_id:
                    continue
                event_code = str(tx_info.get("transaction_event_code", "") or "").upper()
                # Keep payments (T00) and reversals/refunds (T11). Skip fees,
                # currency conversions, deposits, withdrawals, bonuses, and
                # adjustments; those are not merchant sales.
                if event_code.startswith(
                    ("T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09", "T10", "T12", "T13", "T14", "T15")
                ):
                    continue
                amt_obj = tx_info.get("transaction_amount", {})
                # Preserve the sign. PayPal reports refunds as negative amounts;
                # taking the absolute value turns them into extra revenue.
                gross_amt = float(amt_obj.get("value", 0.0) or 0.0)
                if gross_amt == 0:
                    continue
                curr = str(amt_obj.get("currency_code", "USD")).upper()
                payer_info = item.get("payer_info", {})
                country = str(payer_info.get("country_code", "US") or "US").upper()
                fee_obj = tx_info.get("fee_amount", {})
                raw_fee = float(fee_obj.get("value", 0.0) or 0.0) if fee_obj else 0.0
                if raw_fee:
                    fee_amt = abs(raw_fee) if gross_amt > 0 else -abs(raw_fee)
                else:
                    cross_border = country != "US" or curr != "USD"
                    base_fee = compute_paypal_fee(
                        abs(gross_amt), curr, cross_border=cross_border
                    )
                    fee_amt = base_fee if gross_amt > 0 else -base_fee
                net_amt = round(gross_amt - fee_amt, 2)

                fx = FX_TO_USD.get(curr, 1.0)
                usd_gross = round(gross_amt * fx, 2)
                usd_fee = round(fee_amt * fx, 2)
                usd_net = round(net_amt * fx, 2)

                created_at = (
                    tx_info.get("transaction_initiation_date")
                    or tx_info.get("transaction_updated_date")
                    or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                )

                raw_status = str(tx_info.get("transaction_status", "")).upper()
                if raw_status == "V":
                    status = "REVERSED"
                elif raw_status == "D":
                    status = "DENIED"
                elif gross_amt < 0:
                    status = "REFUNDED"
                elif raw_status in ("S", "SUCCESS", "COMPLETED"):
                    status = "COMPLETED"
                else:
                    status = "PENDING"

                cart_info = item.get("cart_info", {})
                item_details = cart_info.get("item_details") or []
                first_item = item_details[0] if item_details else {}
                item_name = str(first_item.get("item_name", "") or "").strip()
                item_sku = str(first_item.get("item_sku", "") or "").strip()

                custom_raw = str(tx_info.get("custom_field", "") or "").strip()
                custom_parts = [p.strip() for p in custom_raw.split("|") if p.strip()]
                custom_rel = ""
                custom_country = ""
                custom_sku = ""
                for part in custom_parts:
                    if part.lower().startswith("release:"):
                        custom_rel = part.split(":", 1)[1].strip()
                    elif len(part) == 2 and part.isalpha() and part.isupper():
                        custom_country = part
                    elif not custom_rel and (part.startswith("v") or "." in part):
                        custom_rel = part
                    else:
                        custom_sku = part

                rel_ver = custom_rel or default_rel
                sku = item_sku or custom_sku or item_name or default_sku
                country = custom_country or country
                prod_name = item_name or f"{sku} ({rel_ver})"

                # A capture's reporting row carries the order ID in
                # paypal_reference_id. Match the local order row so the two
                # engines do not count the same payment twice.
                ref_id = str(tx_info.get("paypal_reference_id", "") or "").strip()
                ref_type = str(tx_info.get("paypal_reference_id_type", "") or "").upper()
                ledger_id = ref_id if ref_type == "ODR" and ref_id else tx_id
                existing = conn.execute(
                    "SELECT id, status, release_version, usd_gross, usd_fee, usd_net, created_at "
                    "FROM transactions WHERE paypal_id = ?",
                    (ledger_id,),
                ).fetchone()
                is_refund = status in ("REFUNDED", "REVERSED")
                # A same-ID reversal voids the original sale. Store zero so the
                # row cannot keep the sale's positive amount.
                void_existing = bool(existing) and is_refund
                stored_gross = 0.0 if void_existing else gross_amt
                stored_fee = 0.0 if void_existing else fee_amt
                stored_net = 0.0 if void_existing else net_amt
                stored_usd_gross = 0.0 if void_existing else usd_gross
                stored_usd_fee = 0.0 if void_existing else usd_fee
                stored_usd_net = 0.0 if void_existing else usd_net
                conn.execute(
                    """
                    INSERT INTO transactions (
                        paypal_id, created_at, gross_amount, fee_amount, net_amount,
                        currency, usd_gross, usd_fee, usd_net, release_version,
                        country_code, product_name, platform_sku, status,
                        is_live_sandbox, approve_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '')
                    ON CONFLICT(paypal_id) DO UPDATE SET
                        status = excluded.status,
                        gross_amount = excluded.gross_amount,
                        fee_amount = excluded.fee_amount,
                        net_amount = excluded.net_amount,
                        usd_gross = excluded.usd_gross,
                        usd_fee = excluded.usd_fee,
                        usd_net = excluded.usd_net
                    """,
                    (
                        ledger_id,
                        created_at,
                        stored_gross,
                        stored_fee,
                        stored_net,
                        curr,
                        stored_usd_gross,
                        stored_usd_fee,
                        stored_usd_net,
                        rel_ver,
                        country,
                        prod_name,
                        sku,
                        status,
                    ),
                )
                prev_status = str(existing["status"] or "").upper() if existing else ""
                was_paid = prev_status in PAID_TRANSACTION_STATUSES
                was_refund = prev_status in ("REFUNDED", "REVERSED")
                is_paid = status in PAID_TRANSACTION_STATUSES
                day = str(created_at)[:10]
                if not existing:
                    added += 1
                    if is_paid:
                        self._apply_paid_rollup(
                            conn, rel_ver, usd_gross, usd_fee, usd_net, day
                        )
                    elif is_refund:
                        self._apply_paid_rollup(
                            conn, rel_ver, usd_gross, usd_fee, usd_net, day,
                            count_order=False,
                        )
                elif is_paid and not was_paid:
                    self._apply_paid_rollup(
                        conn, rel_ver, usd_gross, usd_fee, usd_net, day
                    )
                elif was_paid and not is_paid:
                    # The gateway reversed a payment we already counted.
                    self._apply_paid_rollup(
                        conn,
                        str(existing["release_version"] or rel_ver),
                        -float(existing["usd_gross"] or 0.0),
                        -float(existing["usd_fee"] or 0.0),
                        -float(existing["usd_net"] or 0.0),
                        str(existing["created_at"] or "")[:10],
                        count_order=False,
                    )
                elif is_refund and not was_refund:
                    self._apply_paid_rollup(
                        conn, rel_ver, usd_gross, usd_fee, usd_net, day,
                        count_order=False,
                    )
            conn.commit()
        return added
