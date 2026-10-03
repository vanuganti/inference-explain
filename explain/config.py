"""Settings from .env and/or the shell. Shell variables win over .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # .env support is optional
    load_dotenv = None


class ConfigError(Exception):
    pass


def load_env(path: str | None = None) -> None:
    if load_dotenv:
        load_dotenv(path or Path.cwd() / ".env", override=False)


def _get(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def _float(name: str) -> float | None:
    v = _get(name)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {v!r}")


@dataclass
class Settings:
    provider: str
    db_path: str
    budget_usd: float
    payments_db: str = ""
    memory_db: str = ""

    @staticmethod
    def load() -> "Settings":
        load_env()
        db = _get("EXPLAIN_AGENT_DB") or ".explain-agent/runtime.db"
        here = Path(db).parent
        return Settings(
            provider=(_get("EXPLAIN_PROVIDER") or "openai").lower(),
            db_path=db,
            budget_usd=_float("EXPLAIN_AGENT_BUDGET_USD") or 2.00,
            # separate files on purpose: the payment service and agent memory are not the journal
            payments_db=_get("EXPLAIN_PAYMENTS_DB") or str(here / "payments.db"),
            memory_db=_get("EXPLAIN_MEMORY_DB") or str(here / "memory.db"),
        )


def provider_credentials(provider: str) -> tuple[str | None, str | None]:
    """(api_key, model) for a provider name, from the environment."""
    load_env()
    p = provider.upper()
    return _get(f"{p}_API_KEY"), _get(f"{p}_MODEL")


PROVIDERS = ("openai", "gemini", "anthropic")


def configured_providers() -> list[str]:
    return [p for p in PROVIDERS if all(provider_credentials(p))]


def prices(provider: str) -> dict[str, float | None]:
    load_env()
    p = provider.upper()
    return {
        "input": _float(f"{p}_PRICE_INPUT_PER_M"),
        "output": _float(f"{p}_PRICE_OUTPUT_PER_M"),
        "cached": _float(f"{p}_PRICE_CACHED_PER_M"),
    }


def reasoning_effort() -> str | None:
    """Shared effort level for every provider; adapters translate it."""
    load_env()
    v = (_get("REASONING_EFFORT") or "").lower()
    return v or None
