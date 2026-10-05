"""Provider registry. The only place that knows concrete adapter classes."""
from __future__ import annotations

from ..config import PROVIDERS, ConfigError, configured_providers, provider_credentials, provider_models
from .base import InferenceProvider


def get_providers() -> list[InferenceProvider]:
    """One adapter per configured (provider, model): the plan's candidate set."""
    return [get_provider(n, m) for n in configured_providers() for m, _ in provider_models(n)]


def get_provider(name: str, model: str | None = None) -> InferenceProvider:
    name = name.lower()
    key, default_model = provider_credentials(name)
    model = model or default_model
    if name not in PROVIDERS:
        raise ConfigError(f"unknown EXPLAIN_PROVIDER {name!r} (use {' | '.join(PROVIDERS)})")
    missing = [v for v, x in ((f"{name.upper()}_API_KEY", key), (f"{name.upper()}_MODELS", model)) if not x]
    if missing:
        raise ConfigError(f"{', '.join(missing)} not set (shell env or .env; see .env.example)")
    if name == "openai":
        from .openai import OpenAIProvider
        prov: InferenceProvider = OpenAIProvider(key, model)
    elif name == "anthropic":
        from .anthropic import AnthropicProvider
        prov = AnthropicProvider(key, model)
    else:
        from .gemini import GeminiProvider
        prov = GeminiProvider(key, model)
    prov.tier = dict(provider_models(name)).get(model, "standard")
    return prov


__all__ = ["InferenceProvider", "get_provider", "get_providers"]
