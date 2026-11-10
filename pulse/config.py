"""Configuration and environment management for PayPal Pulse."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Locate repository root (directory containing pulse/)
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Global user config directory (hierarchical fallback across any working directory)
GLOBAL_CONFIG_DIR = Path.home() / ".pulse"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.json"


def _load_global_config() -> dict[str, str]:
    """Read credentials from ~/.pulse/config.json if present."""
    if not GLOBAL_CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(GLOBAL_CONFIG_FILE.read_text(encoding="utf-8"))
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
) -> Path:
    """Save verified credentials to ~/.pulse/config.json with restricted permissions."""
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "PAYPAL_CLIENT_ID": paypal_client_id.strip(),
        "PAYPAL_CLIENT_SECRET": paypal_client_secret.strip(),
        "PAYPAL_MODE": paypal_mode.strip().lower(),
        "GROQ_API_KEY": groq_api_key.strip(),
    }
    GLOBAL_CONFIG_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        GLOBAL_CONFIG_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    return GLOBAL_CONFIG_FILE


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

    paypal_client_id: str = Field(default="")
    paypal_client_secret: str = Field(default="")
    paypal_mode: str = Field(default="sandbox")
    groq_api_key: str = Field(default="")
    gemini_api_key: str = Field(default="")
    openai_api_key: str = Field(default="")
    groq_model: str = Field(default="llama-3.3-70b-versatile")
    cache_dir: Path = Field(default=PACKAGE_ROOT / ".pulse_cache")
    env_path: Path | None = Field(default=None)
    demo_mode: bool = Field(default=False)

    @property
    def paypal_base_url(self) -> str:
        if self.paypal_mode.lower() == "live":
            return "https://api-m.paypal.com"
        return "https://api-m.sandbox.paypal.com"

    @property
    def db_path(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        filename = "demo.db" if self.demo_mode else "pulse.db"
        return self.cache_dir / filename

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
    def has_groq_credentials(self) -> bool:
        return bool(
            self.groq_api_key
            and not self.groq_api_key.startswith("your_")
        )


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
        groq_model=_resolve("GROQ_MODEL", "llama-3.3-70b-versatile"),
        cache_dir=resolved_cache,
        env_path=env_file,
        demo_mode=is_demo,
    )
