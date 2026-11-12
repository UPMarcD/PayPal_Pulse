import os
import pytest
from textual.widgets import Select, Input, Static, TabbedContent
from pulse.tui import PayPalPulseTUI
from pulse.config import PulseSettings
from pulse.store import PulseStore


@pytest.mark.asyncio
async def test_tui_settings_tab_composition(tmp_path):
    db_file = tmp_path / "test_settings.db"
    settings = PulseSettings(
        paypal_client_id="test_client_id_12345",
        paypal_client_secret="test_secret_67890",
        paypal_mode="sandbox",
        ai_provider="openai",
        openai_api_key="sk-test-key",
        ai_model="gpt-4o",
        db_path=db_file,
    )
    store = PulseStore(db_file, settings=settings)
    app = PayPalPulseTUI(settings=settings, store=store, demo_mode=True)

    async with app.run_test(size=(120, 100)) as pilot:
        tabs = app.query_one("#main-tabs", TabbedContent)
        assert tabs is not None

        # Switch to tab-settings via key '6'
        await pilot.press("6")
        assert tabs.active == "tab-settings"

        # Verify settings form widgets
        mode_select = app.query_one("#settings-paypal-mode", Select)
        assert mode_select.value == "sandbox"

        pp_id = app.query_one("#settings-paypal-id", Input)
        assert pp_id.value == "test_client_id_12345"

        ai_prov = app.query_one("#settings-ai-provider", Select)
        assert ai_prov.value == "openai"
        # Assert non-Ollama options do not contain parentheses
        options = [str(opt[0]) for opt in getattr(ai_prov, "_options", [])]
        if options:
            assert "Groq" in options
            assert "OpenAI" in options
            assert "Google Gemini" in options
            assert "Anthropic Claude" in options
            assert "xAI Grok" in options
            assert "Ollama / Local (http://localhost:11434/v1)" in options
            assert "Custom OpenAI-Compatible" in options
            for opt in options:
                if "Ollama" not in opt:
                    assert "(" not in opt and ")" not in opt

        # Verify switching to all supported providers works via public API
        for prov_key in ("groq", "gemini", "anthropic", "xai", "ollama", "custom"):
            ai_prov.value = prov_key
            assert ai_prov.value == prov_key

        ai_model = app.query_one("#settings-ai-model", Input)
        assert ai_model.value == "gpt-4o"

        # Verify unjargoned button labels
        reset_btn = app.query_one("#btn-reset-ledger")
        toggle_btn = app.query_one("#btn-toggle-demo-mode")
        save_btn = app.query_one("#btn-save-settings")
        assert str(reset_btn.label) == "Reset Ledger"
        assert str(toggle_btn.label) == "Switch Demo / Live"
        assert str(save_btn.label) == "Save Changes"

        # Verify unjargoned database info text
        db_info_widget = app.query_one("#settings-db-info", Static)
        rendered_db_info = str(db_info_widget.render())
        assert "Account Status:" in rendered_db_info
        assert "30-Day Activity:" in rendered_db_info
        assert "Gross Revenue:" in rendered_db_info
        assert "Dual-Engine SQLite Ledger" not in rendered_db_info

        # Verify reset button works
        store.seed_telemetry()
        assert len(store.get_transactions()) >= 1

        # Click reset ledger button
        reset_btn.press()
        await pilot.pause()
        assert len(store.get_transactions()) == 0

        # Toggle demo mode button
        assert app.demo_mode is True
        toggle_btn.press()
        await pilot.pause()
        assert app.demo_mode is False


@pytest.mark.asyncio
async def test_tui_settings_save_and_bindings(tmp_path, monkeypatch):
    db_file = tmp_path / "test_settings_2.db"
    settings = PulseSettings(
        paypal_client_id="id_val",
        paypal_client_secret="sec_val",
        paypal_mode="sandbox",
        db_path=db_file,
    )
    store = PulseStore(db_file, settings=settings)
    app = PayPalPulseTUI(settings=settings, store=store, demo_mode=True)

    saved_calls = []

    def mock_save(**kwargs):
        saved_calls.append(kwargs)
        return tmp_path / "config.json"

    monkeypatch.setattr("pulse.tui.save_global_config", mock_save)

    async with app.run_test() as pilot:
        # Press 's' to switch to settings
        await pilot.press("s")
        tabs = app.query_one("#main-tabs", TabbedContent)
        assert tabs.active == "tab-settings"

        # Change AI provider to anthropic
        ai_prov = app.query_one("#settings-ai-provider", Select)
        ai_prov.value = "anthropic"

        ai_key = app.query_one("#settings-ai-key", Input)
        ai_key.value = "sk-ant-test"

        # Press save button
        app.query_one("#btn-save-settings").press()
        await pilot.pause()

        assert len(saved_calls) == 1
        assert saved_calls[0]["ai_provider"] == "anthropic"
        assert saved_calls[0]["anthropic_api_key"] == "sk-ant-test"


@pytest.mark.asyncio
async def test_tui_settings_form_edits_not_clobbered_by_refresh(tmp_path):
    db_file = tmp_path / "test_settings_no_clobber.db"
    settings = PulseSettings(
        paypal_client_id="original_client_id",
        paypal_client_secret="original_secret",
        paypal_mode="sandbox",
        ai_provider="groq",
        groq_api_key="gsk-original",
        db_path=db_file,
    )
    store = PulseStore(db_file, settings=settings)
    app = PayPalPulseTUI(settings=settings, store=store, demo_mode=True)

    async with app.run_test() as pilot:
        # Navigate to Settings
        await pilot.press("6")

        # User modifies input fields and select
        pp_id = app.query_one("#settings-paypal-id", Input)
        pp_id.value = "user_modified_id_123"

        ai_prov = app.query_one("#settings-ai-provider", Select)
        ai_prov.value = "openai"

        ai_key = app.query_one("#settings-ai-key", Input)
        ai_key.value = "sk-user-key-456"

        # Now simulate 3-second background polling calling _populate_all()
        app._populate_all()
        await pilot.pause()

        # Assert that user modifications were NOT clobbered by background polling!
        assert pp_id.value == "user_modified_id_123"
        assert ai_prov.value == "openai"
        assert ai_key.value == "sk-user-key-456"


def test_cli_setup_keep_paypal_configure_ai(monkeypatch, tmp_path):
    from pulse.cli import run_setup_wizard
    from pulse.config import PulseSettings

    saved_calls = []

    def mock_save(**kwargs):
        saved_calls.append(kwargs)
        return tmp_path / "config.json"

    monkeypatch.setattr("pulse.cli.save_global_config", mock_save)

    existing = PulseSettings(
        paypal_client_id="existing_pp_client_id",
        paypal_client_secret="existing_pp_secret",
        paypal_mode="sandbox",
    )
    monkeypatch.setattr("pulse.cli.get_settings", lambda demo_mode=False: existing)

    # Mock PayPalClient.authenticate to succeed
    from pulse.paypal_client import PayPalAuthResult
    mock_auth = PayPalAuthResult(
        authenticated=True,
        app_id="APP-TEST-123",
        mode="sandbox",
        latency_ms=45.0,
    )
    monkeypatch.setattr("pulse.paypal_client.PayPalClient.authenticate", lambda self, force_refresh=True: mock_auth)

    # Simulate user inputs:
    # 1. Empty Client ID -> keep existing PayPal credentials
    # 2. Select AI Provider -> '2' (OpenAI)
    # 3. OpenAI API Key -> 'sk-openai-new-key'
    import sys
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    inputs = iter(["", "2", "sk-openai-new-key"])
    monkeypatch.setattr("typer.prompt", lambda prompt, **kwargs: next(inputs))

    run_setup_wizard()

    assert len(saved_calls) == 1
    assert saved_calls[0]["paypal_client_id"] == "existing_pp_client_id"
    assert saved_calls[0]["paypal_client_secret"] == "existing_pp_secret"
    assert saved_calls[0]["ai_provider"] == "openai"
    assert saved_calls[0]["openai_api_key"] == "sk-openai-new-key"


def test_no_env_onboarding_lifecycle_and_bom_config(monkeypatch, tmp_path):
    """Verify onboarding without .env using PULSE_CONFIG_FILE, non-interactive AI update, and UTF-8 BOM resilience."""
    import os
    from typer.testing import CliRunner
    from pulse.cli import app
    from pulse.config import get_settings, save_global_config
    from pulse.paypal_client import PayPalAuthResult

    runner = CliRunner()
    cfg_file = tmp_path / "custom_config.json"
    monkeypatch.setenv("PULSE_CONFIG_FILE", str(cfg_file))
    monkeypatch.setenv("PULSE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("pulse.config._load_env_files", lambda: None)

    for k in (
        "PAYPAL_CLIENT_ID",
        "PAYPAL_CLIENT_SECRET",
        "PAYPAL_MODE",
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "XAI_API_KEY",
        "AI_PROVIDER",
        "AI_API_KEY",
        "AI_MODEL",
        "AI_BASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)

    # 1. Zero-credential state (no .env, no config.json)
    s0 = get_settings()
    assert not s0.has_paypal_credentials
    assert not s0.has_ai_credentials

    # 2. Onboarding via `pp setup --client-id ... --client-secret ...` (without AI key -> local deterministic fallback)
    mock_auth = PayPalAuthResult(
        authenticated=True,
        app_id="APP-NOENV-999",
        mode="sandbox",
        latency_ms=32.0,
    )
    monkeypatch.setattr("pulse.paypal_client.PayPalClient.authenticate", lambda self, force_refresh=True: mock_auth)

    res1 = runner.invoke(
        app,
        ["setup", "--client-id", "noenv_client_123456", "--client-secret", "noenv_secret_654321", "--mode", "sandbox"],
    )
    assert res1.exit_code == 0
    assert cfg_file.exists()
    assert "Local Deterministic Engine" in res1.stdout

    # 3. Non-interactive AI-only update (`pp setup --provider openai --ai-key sk-noenv-ai`) retaining PayPal credentials
    res2 = runner.invoke(
        app,
        ["setup", "--provider", "openai", "--ai-key", "sk-noenv-ai"],
    )
    assert res2.exit_code == 0
    s2 = get_settings()
    assert s2.paypal_client_id == "noenv_client_123456"
    assert s2.paypal_client_secret == "noenv_secret_654321"
    assert s2.active_ai_provider == "openai"
    assert s2.active_ai_api_key == "sk-noenv-ai"

    # 4. Subsequent PayPal credential rotation without --ai-key preserves existing AI credentials
    res3 = runner.invoke(
        app,
        ["setup", "--client-id", "noenv_client_rotated", "--client-secret", "noenv_secret_rotated"],
    )
    assert res3.exit_code == 0
    s3 = get_settings()
    assert s3.paypal_client_id == "noenv_client_rotated"
    assert s3.active_ai_provider == "openai"
    assert s3.active_ai_api_key == "sk-noenv-ai"

    # 5. UTF-8 BOM resilience on ~/.pulse/config.json
    for k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    cfg_file.write_bytes(
        '\ufeff{"PAYPAL_CLIENT_ID": "bom_client_id", "PAYPAL_CLIENT_SECRET": "bom_secret", "PAYPAL_MODE": "sandbox"}'.encode("utf-8")
    )
    s_bom = get_settings()
    assert s_bom.paypal_client_id == "bom_client_id"
    assert s_bom.paypal_client_secret == "bom_secret"


def test_auto_capture_settings_toggle_and_config(tmp_path, monkeypatch):
    """Verify auto_capture defaults to True, can be toggled via config, and updates settings hierarchically."""
    from pulse.config import get_settings, save_global_config, PulseSettings

    # Snapshot PAYPAL env vars so monkeypatch restores them on teardown
    for k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_MODE", "AUTO_CAPTURE"):
        monkeypatch.setenv(k, os.environ.get(k, ""))

    # 1. Default PulseSettings has auto_capture == True
    default_settings = PulseSettings()
    assert default_settings.auto_capture is True

    # 2. Toggle via global config to False
    cfg_file = tmp_path / "toggle_config.json"
    monkeypatch.setenv("PULSE_CONFIG_FILE", str(cfg_file))
    save_global_config(
        paypal_client_id="test_id",
        paypal_client_secret="test_secret",
        auto_capture=False,
    )
    s_off = get_settings()
    assert s_off.auto_capture is False

    # 3. Toggle back to True
    save_global_config(
        paypal_client_id="test_id",
        paypal_client_secret="test_secret",
        auto_capture=True,
    )
    s_on = get_settings()
    assert s_on.auto_capture is True



