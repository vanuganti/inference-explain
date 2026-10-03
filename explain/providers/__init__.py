"""Provider registry. The only place that knows concrete adapter classes."""
from __future__ import annotations

from ..config import PROVIDERS, ConfigError, provider_credentials
from .base import InferenceProvider


def get_provider(name: str) -> InferenceProvider:
    name = name.lower()
    key, model = provider_credentials(name)
    if name not in PROVIDERS:
        raise ConfigError(f"unknown EXPLAIN_PROVIDER {name!r} (use {' | '.join(PROVIDERS)})")
    missing = [v for v, x in ((f"{name.upper()}_API_KEY", key), (f"{name.upper()}_MODEL", model)) if not x]
    if missing:
        raise ConfigError(f"{', '.join(missing)} not set (shell env or .env; see .env.example)")
    if name == "openai":
        from .openai import OpenAIProvider
        return OpenAIProvider(key, model)
    if name == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider(key, model)
    from .gemini import GeminiProvider
    return GeminiProvider(key, model)


__all__ = ["InferenceProvider", "get_provider"]
