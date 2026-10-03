"""Cost lookup. Order: env override > data/cost.json > auto-fetch (then saved to cost.json).

Neither OpenAI nor Gemini exposes prices through its API, so auto-fetch reads the
community-maintained LiteLLM price table (third-party; verify against the provider's
pricing page before quoting). Each saved entry records its source and fetch time.
Disable with EXPLAIN_PRICE_AUTOFETCH=0. Prices are never guessed.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path

from .config import _get, prices
from .schema import InferenceUsage

PRICE_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
_table: dict | None = None  # per-process cache of the remote table


def cost_file() -> Path:
    return Path(_get("EXPLAIN_COST_FILE") or Path(__file__).resolve().parent.parent / "data" / "cost.json")


def _load() -> dict:
    try:
        return json.loads(cost_file().read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    p = cost_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, p)  # atomic: never leaves a half-written file


def _lookup(models: dict, model: str) -> tuple[str, dict] | None:
    """Exact match, else the longest key that prefixes a served name like 'x-001'."""
    if model in models:
        return model, models[model]
    hits = [k for k in models if model.startswith(k)]
    return (max(hits, key=len), models[max(hits, key=len)]) if hits else None


def _fetch(provider: str, model: str) -> dict | None:
    global _table
    if _get("EXPLAIN_PRICE_AUTOFETCH") == "0":
        return None
    if _table is None:
        try:
            with urllib.request.urlopen(PRICE_URL, timeout=15) as r:
                _table = json.load(r)
        except Exception:  # noqa: BLE001 - offline etc.: stay UNAVAILABLE
            _table = {}
    pref = {"openai": ("", "openai/"), "gemini": ("gemini/", ""), "anthropic": ("", "anthropic/")}[provider]
    cands = {}
    for k, v in _table.items():
        if not isinstance(v, dict) or v.get("litellm_provider") != provider:
            continue
        for pre in pref:
            if k.startswith(pre) and (pre or "/" not in k):
                cands[k[len(pre):]] = v
    hit = _lookup(cands, model)
    if not hit or hit[1].get("input_cost_per_token") is None or hit[1].get("output_cost_per_token") is None:
        return None
    e = hit[1]
    per_m = lambda x: None if x is None else round(x * 1e6, 6)  # noqa: E731
    return {"input_per_m": per_m(e["input_cost_per_token"]),
            "output_per_m": per_m(e["output_cost_per_token"]),
            "cached_per_m": per_m(e.get("cache_read_input_token_cost")),
            "source": f"litellm:{hit[0]}", "source_url": PRICE_URL,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def get_price(provider: str, model: str | None) -> tuple[dict | None, str]:
    """(price dict, origin) with origin in env|file|fetched|none."""
    env = prices(provider)
    if env["input"] is not None and env["output"] is not None:
        return {"input_per_m": env["input"], "output_per_m": env["output"],
                "cached_per_m": env["cached"]}, "env"
    if not model:
        return None, "none"
    data = _load()
    # served names (e.g. '-001', or a concrete version behind a '-latest' alias) may
    # differ from the configured model, so try both.
    names = [model] + [m for m in [_get(f"{provider.upper()}_MODEL")] if m and m != model]
    for name in names:
        hit = _lookup(data.get(provider, {}), name)
        if hit:
            return hit[1], "file"
    for name in names:
        fetched = _fetch(provider, name)
        if fetched:
            data.setdefault(provider, {})[name] = fetched
            _save(data)
            return fetched, "fetched"
    return None, "none"


def compute_cost(u: InferenceUsage) -> tuple[float | None, str | None]:
    p, origin = get_price(u.provider, u.model)
    if p is None:
        return None, f"no price for {u.model} in {cost_file().name}; add it or set {u.provider.upper()}_PRICE_*"
    if u.input_tokens is None or u.output_tokens is None:
        return None, "provider did not report token counts"
    cached = u.cached_tokens or 0
    fresh_in = max(u.input_tokens - cached, 0)
    out = u.output_tokens
    if not u.output_includes_reasoning:
        out += u.reasoning_tokens or 0  # thinking is billed as output
    cp = p.get("cached_per_m")
    cp = p["input_per_m"] if cp is None else cp
    cost = (fresh_in * p["input_per_m"] + cached * cp + out * p["output_per_m"]) / 1e6
    return cost, None
