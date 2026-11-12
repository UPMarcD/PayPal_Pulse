"""Configuration and environment management for PayPal Pulse."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

# Locate repository root (directory containing pulse/)
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Global user config directory (hierarchical fallback across any working directory)
GLOBAL_CONFIG_DIR = Path.home() / ".pulse"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.json"


def _get_global_config_path() -> Path:
    """Return active global config file path, honoring PULSE_CONFIG_FILE override if set."""
    override = os.getenv("PULSE_CONFIG_FILE", "").strip()
    if override:
        return Path(override)
    return GLOBAL_CONFIG_FILE


def _load_global_config() -> dict[str, str]:
    """Read credentials from ~/.pulse/config.json if present (UTF-8 BOM resilient)."""
    cfg_file = _get_global_config_path()
    if not cfg_file.exists():
        return {}
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            return {k: str(v).strip() for k, v in data.items() if v is not None}
    except Exception:
        pass
    return {}


def save_global_config(
    paypal_client_id: str,
    paypal_client_secret: str,
    paypal_mode: str = "sandbox",
    groq_api_key: str = "",
    ai_provider: str = "",
    ai_base_url: str = "",
    ai_api_key: str = "",
    ai_model: str = "",
    openai_api_key: str = "",
    gemini_api_key: str = "",
    xai_api_key: str = "",
    anthropic_api_key: str = "",
    openrouter_api_key: str = "",
    auto_capture: bool | None = None,
) -> Path:
    """Save verified credentials to ~/.pulse/config.json with restricted permissions."""
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "PAYPAL_CLIENT_ID": paypal_client_id.strip(),
        "PAYPAL_CLIENT_SECRET": paypal_client_secret.strip(),
        "PAYPAL_MODE": paypal_mode.strip().lower(),
        "GROQ_API_KEY": groq_api_key.strip(),
        "AI_PROVIDER": ai_provider.strip(),
        "AI_BASE_URL": ai_base_url.strip(),
        "AI_API_KEY": ai_api_key.strip(),
        "AI_MODEL": ai_model.strip(),
        "OPENAI_API_KEY": openai_api_key.strip(),
        "GEMINI_API_KEY": gemini_api_key.strip(),
        "XAI_API_KEY": xai_api_key.strip(),
        "ANTHROPIC_API_KEY": anthropic_api_key.strip(),
        "OPENROUTER_API_KEY": openrouter_api_key.strip(),
    }
    if auto_capture is not None:
        payload["AUTO_CAPTURE"] = "true" if auto_capture else "false"
    # Clean out empty optional keys from config JSON
    clean_payload = {k: v for k, v in payload.items() if v or k.startswith("PAYPAL")}
    cfg_file = _get_global_config_path()
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(json.dumps(clean_payload, indent=2), encoding="utf-8")
    try:
        cfg_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass

    # Synchronize process environment variables so immediate in-memory get_settings() reflects newly saved keys
    for k, v in payload.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)

    return cfg_file


def _load_env_files() -> Path | None:
    """Load .env from current working directory or package root without overriding existing env vars."""
    cwd_env = Path.cwd() / ".env"
    root_env = PACKAGE_ROOT / ".env"

    if cwd_env.exists():
        load_dotenv(dotenv_path=cwd_env, override=False)
        return cwd_env
    if root_env.exists():
        load_dotenv(dotenv_path=root_env, override=False)
        return root_env
    load_dotenv(override=False)
    return None


class PulseSettings(BaseModel):
    """Typed runtime configuration loaded hierarchically from env vars, local .env, and ~/.pulse/config.json."""

    model_config = ConfigDict(populate_by_name=True)

    paypal_client_id: str = Field(default="")
    paypal_client_secret: str = Field(default="")
    paypal_mode: str = Field(default="sandbox")
    groq_api_key: str = Field(default="")
    gemini_api_key: str = Field(default="")
    openai_api_key: str = Field(default="")
    xai_api_key: str = Field(default="")
    anthropic_api_key: str = Field(default="")
    openrouter_api_key: str = Field(default="")
    ai_provider: str = Field(default="")
    ai_base_url: str = Field(default="")
    ai_api_key: str = Field(default="")
    ai_model: str = Field(default="")
    groq_model: str = Field(default="llama-3.3-70b-versatile")
    cache_dir: Path = Field(default=PACKAGE_ROOT / ".pulse_cache")
    custom_db_path: Path | None = Field(default=None, alias="db_path")
    env_path: Path | None = Field(default=None)
    demo_mode: bool = Field(default=False)
    auto_capture: bool = Field(default=True)

    @property
    def paypal_base_url(self) -> str:
        if self.paypal_mode.lower() == "live":
            return "https://api-m.paypal.com"
        return "https://api-m.sandbox.paypal.com"

    @property
    def db_path(self) -> Path:
        if self.custom_db_path is not None:
            self.custom_db_path.parent.mkdir(parents=True, exist_ok=True)
            return self.custom_db_path
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        filename = "demo.db" if self.demo_mode else "pulse.db"
        return self.cache_dir / filename

    @db_path.setter
    def db_path(self, value: Path | None) -> None:
        self.custom_db_path = value

    @property
    def token_cache_path(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        filename = "demo_token_cache.json" if self.demo_mode else "token_cache.json"
        return self.cache_dir / filename

    @property
    def has_paypal_credentials(self) -> bool:
        return bool(
            self.paypal_client_id
            and self.paypal_client_secret
            and not self.paypal_client_id.startswith("your_")
            and not self.paypal_client_secret.startswith("your_")
        )

    @property
    def active_ai_provider(self) -> str:
        """Infer active AI provider from explicit setting or available API keys."""
        if self.ai_provider:
            return self.ai_provider.strip().lower()
        if self.ai_base_url and ("localhost" in self.ai_base_url or "11434" in self.ai_base_url):
            return "ollama"
        if self.groq_api_key and not self.groq_api_key.startswith("your_"):
            return "groq"
        if self.openai_api_key and not self.openai_api_key.startswith("your_"):
            return "openai"
        if self.gemini_api_key and not self.gemini_api_key.startswith("your_"):
            return "gemini"
        if self.xai_api_key and not self.xai_api_key.startswith("your_"):
            return "xai"
        if self.anthropic_api_key and not self.anthropic_api_key.startswith("your_"):
            return "anthropic"
        if self.openrouter_api_key and not self.openrouter_api_key.startswith("your_"):
            return "openrouter"
        if self.ai_api_key and not self.ai_api_key.startswith("your_"):
            return "custom"
        return "groq"

    @property
    def active_ai_base_url(self) -> str:
        """Return base URL for the active AI provider."""
        if self.ai_base_url:
            return self.ai_base_url.rstrip("/")
        provider = self.active_ai_provider
        if provider == "openai":
            return "https://api.openai.com/v1"
        if provider == "gemini":
            return "https://generativelanguage.googleapis.com/v1beta/openai"
        if provider == "xai":
            return "https://api.x.ai/v1"
        if provider == "anthropic":
            return "https://api.anthropic.com/v1"
        if provider == "openrouter":
            return "https://openrouter.ai/api/v1"
        if provider == "ollama":
            return "http://localhost:11434/v1"
        return "https://api.groq.com/openai/v1"

    @property
    def active_ai_api_key(self) -> str:
        """Return API key for the active AI provider."""
        if self.ai_api_key and not self.ai_api_key.startswith("your_"):
            return self.ai_api_key
        provider = self.active_ai_provider
        if provider == "openai":
            return self.openai_api_key
        if provider == "gemini":
            return self.gemini_api_key
        if provider == "xai":
            return self.xai_api_key
        if provider == "anthropic":
            return self.anthropic_api_key
        if provider == "openrouter":
            return self.openrouter_api_key
        if provider == "ollama":
            return ""
        return self.groq_api_key

    @property
    def active_ai_model(self) -> str:
        """Return default model for the active AI provider."""
        if self.ai_model:
            return self.ai_model
        provider = self.active_ai_provider
        if provider == "openai":
            return "gpt-4o-mini"
        if provider == "gemini":
            return "gemini-2.0-flash"
        if provider == "xai":
            return "grok-2-latest"
        if provider == "anthropic":
            return "claude-3-5-sonnet-20241022"
        if provider == "openrouter":
            return "anthropic/claude-3.5-sonnet"
        if provider == "ollama":
            return "llama3.3"
        return self.groq_model or "llama-3.3-70b-versatile"

    @property
    def has_ai_credentials(self) -> bool:
        """Return True if active AI provider credentials are configured or if local Ollama is selected."""
        provider = self.active_ai_provider
        if provider == "ollama":
            return True
        key = self.active_ai_api_key
        return bool(key and not key.startswith("your_"))

    @property
    def has_groq_credentials(self) -> bool:
        """Backward compatibility alias for has_ai_credentials."""
        return self.has_ai_credentials


def get_settings(
    cache_dir: Path | None = None,
    demo_mode: bool = False,
) -> PulseSettings:
    """Load and return current settings using 3-tier hierarchical resolution."""
    env_file = _load_env_files()
    global_cfg = _load_global_config()

    resolved_cache = cache_dir or Path(
        os.getenv("PULSE_CACHE_DIR", str(PACKAGE_ROOT / ".pulse_cache"))
    )

    # Resolution hierarchy: os.environ -> local .env -> ~/.pulse/config.json
    def _resolve(key: str, default: str = "") -> str:
        val = os.getenv(key)
        if val is not None and val.strip():
            return val.strip()
        if key in global_cfg and global_cfg[key].strip():
            return global_cfg[key].strip()
        return default

    is_demo = demo_mode or os.getenv("PULSE_DEMO_MODE", "").lower() in ("1", "true", "yes")

    return PulseSettings(
        paypal_client_id=_resolve("PAYPAL_CLIENT_ID"),
        paypal_client_secret=_resolve("PAYPAL_CLIENT_SECRET"),
        paypal_mode=_resolve("PAYPAL_MODE", "sandbox").lower(),
        groq_api_key=_resolve("GROQ_API_KEY"),
        gemini_api_key=_resolve("GEMINI_API_KEY"),
        openai_api_key=_resolve("OPENAI_API_KEY"),
        xai_api_key=_resolve("XAI_API_KEY"),
        anthropic_api_key=_resolve("ANTHROPIC_API_KEY"),
        openrouter_api_key=_resolve("OPENROUTER_API_KEY"),
        ai_provider=_resolve("AI_PROVIDER"),
        ai_base_url=_resolve("AI_BASE_URL"),
        ai_api_key=_resolve("AI_API_KEY"),
        ai_model=_resolve("AI_MODEL"),
        groq_model=_resolve("GROQ_MODEL", "llama-3.3-70b-versatile"),
        cache_dir=resolved_cache,
        env_path=env_file,
        demo_mode=is_demo,
        auto_capture=_resolve("AUTO_CAPTURE", "true").lower() in ("true", "1", "yes"),
    )
