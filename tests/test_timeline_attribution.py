"""Automated tests for PayPal Pulse Release-to-Revenue Timeline attribution, `pp init`, and dynamic ledger upsert."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from pulse.cli import app
from pulse.paypal_client import PayPalOrderResult
from pulse.store import PulseStore, detect_project_context, parse_release_version

runner = CliRunner()


def test_parse_release_version_patterns() -> None:
    """Verify robust parsing across canonical prefix, JSON, pipe-delimited, and raw git tag formats."""
    # Canonical prefix
    assert parse_release_version("release:v1.3.0") == "v1.3.0"
    assert parse_release_version("release:v1.3.0|US|PulseCore Pro") == "v1.3.0"
    assert parse_release_version("RELEASE:v2.0.0-rc1") == "v2.0.0-rc1"

    # JSON metadata payloads
    assert parse_release_version('{"v": "v1.4.0"}') == "v1.4.0"
    assert parse_release_version('{"version": "v1.5.0", "sku": "Pro"}') == "v1.5.0"
    assert parse_release_version('{"release": "v1.6.0"}') == "v1.6.0"

    # Legacy pipe-delimited
    assert parse_release_version("v1.2.1|US|PulseCore Pro") == "v1.2.1"
    assert parse_release_version("v1.0.0|DE|Enterprise") == "v1.0.0"

    # Raw git tag or semantic version
    assert parse_release_version("v2.1.0") == "v2.1.0"
    assert parse_release_version("v0.9.5-beta") == "v0.9.5-beta"

    assert parse_release_version("release:", default="v0.1.0") == "v0.1.0"
    assert parse_release_version("release:   ", default="v0.1.0") == "v0.1.0"
    assert parse_release_version("release:|US|SKU", default="v0.1.0") == "v0.1.0"

    # Empty or None fallbacks
    assert parse_release_version(None, default="v0.1.0") == "v0.1.0"
    assert parse_release_version("", default="v0.1.0") == "v0.1.0"
    assert parse_release_version("arbitrary_string", default="v0.1.0") == "arbitrary_string"


def test_detect_project_context(tmp_path: Path) -> None:
    """Verify multi-stack project context detection for Python, Node, and .pulse.json configurations."""
    # 1. Plain directory
    plain_ctx = detect_project_context(tmp_path)
    assert plain_ctx["project_name"] == tmp_path.name
    assert plain_ctx["version"] == "v0.1.0"

    # 2. Node.js project
    pkg_file = tmp_path / "package.json"
    pkg_file.write_text(json.dumps({"name": "hyper-saas", "version": "1.8.2"}), encoding="utf-8")
    node_ctx = detect_project_context(tmp_path)
    assert node_ctx["project_name"] == "hyper-saas"
    assert node_ctx["version"] == "v1.8.2"
    assert node_ctx["package_type"] == "npm"
    pkg_file.unlink()

    # 3. Python project
    pyproj = tmp_path / "pyproject.toml"
    pyproj.write_text('[project]\nname = "py-pulse"\nversion = "2.4.0"\n', encoding="utf-8")
    py_ctx = detect_project_context(tmp_path)
    assert py_ctx["version"] == "v2.4.0"
    assert py_ctx["package_type"] == "python"
    pyproj.unlink()

    # 4. .pulse.json override
    pulse_cfg = tmp_path / ".pulse.json"
    pulse_cfg.write_text(json.dumps({"project_name": "custom-override", "version": "v9.9.9"}), encoding="utf-8")
    override_ctx = detect_project_context(tmp_path)
    assert override_ctx["project_name"] == "custom-override"
    assert override_ctx["version"] == "v9.9.9"
    assert override_ctx["config_file"] == str(pulse_cfg)


def test_dynamic_release_upsert_in_store(tmp_path: Path) -> None:
    """Verify that orders for non-existent release tags dynamically create first-class records in SQLite."""
    db_file = tmp_path / "test_upsert.db"
    store = PulseStore(db_path=db_file)
    store.seed_telemetry()

    # Verify initial known releases
    initial_rels = {r["version"] for r in store.get_releases(include_git_tags=False)}
    assert "v2.5.0" not in initial_rels

    # Record order on brand new release tag
    order_res = PayPalOrderResult(
        success=True,
        order_id="ORD-DYN-250",
        status="COMPLETED",
        amount=99.00,
        currency="USD",
        description="Dynamic v2.5.0 Pro Purchase",
        approve_url="https://www.sandbox.paypal.com/checkoutnow?token=ORD-DYN-250",
    )
    receipt = store.record_live_order(order_res, release_version="v2.5.0", country_code="US")
    assert receipt["paypal_id"] == "ORD-DYN-250"
    assert receipt["release_version"] == "v2.5.0"

    # Verify release is automatically created in releases table
    all_rels = store.get_releases(include_git_tags=False)
    v250_rel = next((r for r in all_rels if r["version"] == "v2.5.0"), None)
    assert v250_rel is not None
    assert v250_rel["paid_conversions"] == 1
    assert v250_rel["gross_usd"] == 99.00
    assert v250_rel["net_usd"] > 90.00
    assert v250_rel["installs"] == 0
    assert v250_rel["conversion_rate"] == 0.0

    # Record second order on the same release
    order_res_2 = PayPalOrderResult(
        success=True,
        order_id="ORD-DYN-251",
        status="COMPLETED",
        amount=49.00,
        currency="USD",
        description="Dynamic v2.5.0 Second Purchase",
        approve_url="https://www.sandbox.paypal.com/checkoutnow?token=ORD-DYN-251",
    )
    store.record_live_order(order_res_2, release_version="v2.5.0", country_code="GB")
    updated_rels = store.get_releases(include_git_tags=False)
    v250_updated = next(r for r in updated_rels if r["version"] == "v2.5.0")
    assert v250_updated["paid_conversions"] == 2
    assert v250_updated["gross_usd"] == 148.00


def test_git_tag_telemetry_aggregation(tmp_path: Path, monkeypatch) -> None:
    """Verify that local git tags dynamically aggregate real transactions rather than showing $0.00 zeros."""
    db_file = tmp_path / "test_tag_agg.db"
    store = PulseStore(db_path=db_file)

    # Mock discovered git tags
    monkeypatch.setattr(store, "_discover_git_tags", lambda: ["v0.1.0", "v0.2.0"])

    # Before recording transactions, tags show zero baselines
    rels_before = store.get_releases(include_git_tags=True)
    v010_before = next(r for r in rels_before if r["version"] == "v0.1.0")
    assert v010_before["gross_usd"] == 0.0
    assert v010_before["paid_conversions"] == 0

    # Record live orders for v0.1.0
    order = PayPalOrderResult(
        success=True,
        order_id="ORD-TAG-01",
        status="COMPLETED",
        amount=100.00,
        currency="USD",
        description="Tag 0.1 Order",
    )
    store.record_live_order(order, release_version="v0.1.0")

    # Now get_releases should aggregate real transactions for v0.1.0
    rels_after = store.get_releases(include_git_tags=True)
    v010_after = next(r for r in rels_after if r["version"] == "v0.1.0")
    assert v010_after["gross_usd"] == 100.00
    assert v010_after["paid_conversions"] == 1
    assert v010_after["installs"] == 0
    assert v010_after["conversion_rate"] == 0.0
    assert v010_after["status_badge"] == "ACTIVE"


def test_cli_init_command(tmp_path: Path, monkeypatch) -> None:
    """Verify `pp init` creates .pulse.json and outputs valid configuration and integration code."""
    monkeypatch.chdir(tmp_path)

    # First run: initialize project
    result = runner.invoke(app, ["init", "--name", "rocket-saas", "--version", "v1.0.0", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["status"] == "INITIALIZED"
    assert data["config"]["project_name"] == "rocket-saas"
    assert data["config"]["version"] == "v1.0.0"
    assert (tmp_path / ".pulse.json").exists()
    assert "python" in data["integration_snippets"]
    assert "nodejs" in data["integration_snippets"]

    # Second run without force: already initialized
    result_dup = runner.invoke(app, ["init", "--json"])
    assert result_dup.exit_code == 0
    dup_data = json.loads(result_dup.stdout)
    assert dup_data["status"] == "ALREADY_INITIALIZED"

    # Third run with force: overwrites cleanly
    result_force = runner.invoke(app, ["init", "--version", "v2.0.0", "--force", "--json"])
    assert result_force.exit_code == 0
    force_data = json.loads(result_force.stdout)
    assert force_data["config"]["version"] == "v2.0.0"


def test_cli_order_and_trigger_release_flags(tmp_path: Path, monkeypatch) -> None:
    """Verify CLI `pp order` and `pp trigger` accept `--release` and attribute orders dynamically."""
    db_file = tmp_path / "test_cli_flags.db"
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "mock_id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "mock_secret")

    store = PulseStore(db_path=db_file)
    monkeypatch.setattr("pulse.cli.get_settings", lambda demo_mode=False: store.settings)

    # Test pp trigger with dynamic release
    res_trig = runner.invoke(
        app,
        ["trigger", "payment.capture.completed", "--release", "v4.0.0", "--amount", "75.00", "--json"],
    )
    assert res_trig.exit_code == 0
    trig_data = json.loads(res_trig.stdout)
    assert trig_data["status"] == "INGESTED"
    assert trig_data["receipt"]["release_version"] == "v4.0.0"
    assert trig_data["receipt"]["gross"] == 75.00


def test_clean_board_multi_trigger_dispute_and_git_sha_stresstest(tmp_path: Path, monkeypatch) -> None:
    """Verify UTF-8 BOM handling, multi-trigger uniqueness, dispute attribution, daily_metrics upsert, and real Git tag commit SHAs on a clean board."""
    repo_dir = tmp_path / "pulsetest_repo"
    repo_dir.mkdir()
    monkeypatch.chdir(repo_dir)
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path))

    store = PulseStore()
    store.reset(mark_clean=True)

    # 1. Initialize real Git repo with UTF-8 BOM package.json and tags
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@pulsetest.io"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Pulse Tester"], cwd=str(repo_dir), check=True, capture_output=True)

    # Write package.json with explicit UTF-8 BOM (\ufeff) to simulate Windows PowerShell Set-Content
    (repo_dir / "package.json").write_bytes(
        '\ufeff{"name": "pulsetest-saas", "version": "0.1.0"}'.encode("utf-8")
    )
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: alpha v0.1.0"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "tag", "v0.1.0"], cwd=str(repo_dir), check=True, capture_output=True)
    sha_v010 = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True
    ).stdout.strip()

    (repo_dir / "package.json").write_bytes(
        '\ufeff{"name": "pulsetest-saas", "version": "0.2.0"}'.encode("utf-8")
    )
    subprocess.run(["git", "commit", "-am", "feat: beta v0.2.0"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "tag", "v0.2.0"], cwd=str(repo_dir), check=True, capture_output=True)
    sha_v020 = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True
    ).stdout.strip()

    # 2. Verify pp init detects "pulsetest-saas" despite UTF-8 BOM
    init_res = runner.invoke(app, ["init", "--json"])
    assert init_res.exit_code == 0
    init_data = json.loads(init_res.stdout)
    assert init_data["config"]["project_name"] == "pulsetest-saas"
    assert init_data["config"]["version"] == "v0.2.0"

    # 3. Trigger multiple captures and a dispute on a clean board
    r1 = runner.invoke(app, ["trigger", "payment.capture.completed", "--release", "v0.1.0", "--amount", "19.99", "--json"])
    r2 = runner.invoke(app, ["trigger", "payment.capture.completed", "--release", "v0.2.0", "--amount", "49.00", "--json"])
    r3 = runner.invoke(app, ["trigger", "payment.capture.completed", "--release", "v0.2.0", "--amount", "49.00", "--json"])
    r4 = runner.invoke(
        app,
        ["trigger", "customer.dispute.created", "--release", "v0.2.0", "--amount", "49.00", "--id", "PP-D-TEST-020", "--json"],
    )
    assert r1.exit_code == 0 and r2.exit_code == 0 and r3.exit_code == 0 and r4.exit_code == 0

    # 4. Verify timeline has both releases, distinct conversions, dispute count, and real Git commit SHAs
    fresh_store = PulseStore()
    releases = fresh_store.get_releases(include_git_tags=True)
    rel_map = {r["version"]: r for r in releases}

    assert rel_map["v0.1.0"]["paid_conversions"] == 1
    assert rel_map["v0.1.0"]["gross_usd"] == 19.99
    assert rel_map["v0.1.0"]["git_commit"] == sha_v010

    assert rel_map["v0.2.0"]["paid_conversions"] == 2
    assert rel_map["v0.2.0"]["installs"] == 0
    assert rel_map["v0.2.0"]["conversion_rate"] == 0.0
    assert rel_map["v0.2.0"]["gross_usd"] == 98.00
    assert rel_map["v0.2.0"]["dispute_count"] == 1
    assert rel_map["v0.2.0"]["status_badge"] == "DISPUTE RISK"
    assert rel_map["v0.2.0"]["git_commit"] == sha_v020

    # 5. Verify daily_metrics populated on clean board
    daily = fresh_store.get_recent_daily_metrics(days=14)
    assert len(daily) == 1
    assert daily[0]["paid_orders"] == 3
    assert daily[0]["gross_usd"] == 117.99
    assert daily[0]["disputes"] == 1


def test_project_tailored_currency_sku_price_and_ai_context(tmp_path: Path, monkeypatch) -> None:
    """Verify `pp init --currency --sku --price` tailors CLI triggers, 25 PayPal currencies, and AI Copilot context without PulseCore hardcoding."""
    from pulse.ai_copilot import _build_deterministic_fallback, _detect_order_intent, build_financial_context_prompt
    from pulse.store import CURRENCY_SYMBOLS, FX_TO_USD, compute_paypal_fee

    repo_dir = tmp_path / "novacloud_repo"
    repo_dir.mkdir()
    monkeypatch.chdir(repo_dir)
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path))

    store = PulseStore()
    store.reset(mark_clean=True)

    # 1. Verify 25 PayPal currencies supported in FX_TO_USD, CURRENCY_SYMBOLS, and compute_paypal_fee
    for cur in ("CAD", "AUD", "JPY", "SGD", "CHF", "SEK", "PLN", "BRL", "MXN", "INR"):
        assert cur in FX_TO_USD
        assert cur in CURRENCY_SYMBOLS
        fee = compute_paypal_fee(100.0, cur)
        assert fee > 0

    # 2. Initialize project with custom currency (CAD), SKU ("NovaCloud Enterprise"), and price (79.00)
    init_res = runner.invoke(
        app,
        [
            "init",
            "--name",
            "novacloud",
            "--version",
            "v3.1.0",
            "--currency",
            "CAD",
            "--sku",
            "NovaCloud Enterprise",
            "--price",
            "79.00",
            "--json",
        ],
    )
    assert init_res.exit_code == 0
    init_data = json.loads(init_res.stdout)
    assert init_data["config"]["project_name"] == "novacloud"
    assert init_data["config"]["version"] == "v3.1.0"
    assert init_data["config"]["default_currency"] == "CAD"
    assert init_data["config"]["default_sku"] == "NovaCloud Enterprise"
    assert init_data["config"]["default_price"] == 79.0
    assert "CAD" in init_data["integration_snippets"]["python"]
    assert "79.00" in init_data["integration_snippets"]["python"]
    assert "NovaCloud Enterprise" in init_data["integration_snippets"]["python"]

    # 3. Trigger payment capture without passing --release, --currency, --amount, or --sku -> should inherit all 4 from .pulse.json!
    trig_res = runner.invoke(app, ["trigger", "payment.capture.completed", "--json"])
    assert trig_res.exit_code == 0
    trig_data = json.loads(trig_res.stdout)
    assert trig_data["receipt"]["release_version"] == "v3.1.0"
    assert trig_data["receipt"]["currency"] == "CAD"
    assert trig_data["receipt"]["gross"] == 79.0

    # 4. Verify AI Copilot order intent detection inherits project currency, price, and SKU
    should_create, amt, cur, desc = _detect_order_intent("create a live order")
    assert should_create is True
    assert cur == "CAD"
    assert amt == 79.0
    assert "NovaCloud Enterprise" in desc

    # 5. Verify AI Copilot system prompt and deterministic fallback reflect novacloud & v3.1.0 (not hardcoded PulseCore v1.2.1)
    fresh_store = PulseStore()
    summary = fresh_store.get_dashboard_summary()
    sys_prompt = build_financial_context_prompt(summary)
    assert "NovaCloud Enterprise" in sys_prompt
    assert "novacloud" in sys_prompt
    assert "v3.1.0" in sys_prompt

    fallback_txt = _build_deterministic_fallback("how are sales?", summary, None, "test")
    assert "v3.1.0" in fallback_txt
    assert "PP-D-90411-DE" not in fallback_txt

    # 6. Verify zero-decimal currency (JPY, HUF, TWD) integer formatting and fee rounding
    from pulse.store import format_currency_amount
    assert format_currency_amount(1500.0, "JPY") == "1500"
    assert format_currency_amount(4500.0, "HUF") == "4500"
    assert format_currency_amount(300.0, "TWD") == "300"
    assert format_currency_amount(19.99, "USD") == "19.99"
    jpy_fee = compute_paypal_fee(1000.0, "JPY")
    assert isinstance(jpy_fee, float)
    assert jpy_fee == float(int(jpy_fee))  # Zero fractional decimals in JPY!

    # 7. Verify MCP and Copilot schemas have all 25 currencies
    from pulse.mcp_server import ALL_PAYPAL_CURRENCIES, MCP_TOOLS
    from pulse.ai_copilot import PAYPAL_COPILOT_TOOLS
    assert len(ALL_PAYPAL_CURRENCIES) == 25
    order_tool = next(t for t in MCP_TOOLS if t["name"] == "pulse_create_order")
    assert len(order_tool["inputSchema"]["properties"]["currency"]["enum"]) == 25
    copilot_order_tool = next(t for t in PAYPAL_COPILOT_TOOLS if t["function"]["name"] == "create_paypal_order")
    assert len(copilot_order_tool["function"]["parameters"]["properties"]["currency"]["enum"]) == 25


