"""Configuration and environment management for PayPal Pulse."""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Locate repository root (directory containing pulse/)
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


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
    """Typed runtime configuration loaded from .env."""

    paypal_client_id: str = Field(default="")
    paypal_client_secret: str = Field(default="")
    paypal_mode: str = Field(default="sandbox")
    groq_api_key: str = Field(default="")
    gemini_api_key: str = Field(default="")
    openai_api_key: str = Field(default="")
    groq_model: str = Field(default="llama-3.3-70b-versatile")
    cache_dir: Path = Field(default=PACKAGE_ROOT / ".pulse_cache")
    env_path: Path | None = Field(default=None)

    @property
    def paypal_base_url(self) -> str:
        if self.paypal_mode.lower() == "live":
            return "https://api-m.paypal.com"
        return "https://api-m.sandbox.paypal.com"

    @property
    def db_path(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / "pulse.db"

    @property
    def token_cache_path(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / "token_cache.json"

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


def get_settings(cache_dir: Path | None = None) -> PulseSettings:
    """Load and return current settings."""
    env_file = _load_env_files()
    resolved_cache = cache_dir or Path(
        os.getenv("PULSE_CACHE_DIR", str(PACKAGE_ROOT / ".pulse_cache"))
    )
    return PulseSettings(
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", "").strip(),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", "").strip(),
        paypal_mode=os.getenv("PAYPAL_MODE", "sandbox").strip().lower(),
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip(),
        cache_dir=resolved_cache,
        env_path=env_file,
    )
