"""EXPLAIN INFERENCE / ANALYZE / comparison renderers. Work on EXPLAIN types only."""
from __future__ import annotations

from .style import Panel
from .schema import (DERIVED, ESTIMATED, OBSERVED, UNAVAILABLE, InferenceResult,
                     ProviderCapabilities)

W = 68
DISPLAY = {"openai": "OpenAI", "gemini": "Gemini"}


def pname(p: str) -> str:
    return DISPLAY.get(p, p.capitalize())


def n(v) -> str:
    return UNAVAILABLE if v is None else f"{v:,}"


def secs(v) -> str:
    return UNAVAILABLE if v is None else f"{v:.2f}s"


def money(r: InferenceResult) -> str:
    if r.cost_usd is None:
        return f"UNAVAILABLE ({r.cost_note})" if r.cost_note else UNAVAILABLE
    return f"${r.cost_usd:.6f}"


def box(title: str, rows: list[tuple[str, str] | None], width: int = W) -> str:
    panel = Panel(title)
    for r in rows:
        panel.row() if r is None else panel.kv(r[0], r[1])
    return panel.render(min_width=width)


def cap(supported: bool, value: str = "") -> str:
    return value if supported else UNAVAILABLE


def tag(value: str, provenance: str) -> str:
    """value + a dim provenance tag (OBSERVED / DERIVED / ESTIMATED)."""
    from .style import paint
    return value if value.startswith(UNAVAILABLE) else f"{value}  " + paint(provenance, "dim")


def explain_inference(r: InferenceResult, caps: ProviderCapabilities, router: str = "configured provider/model") -> str:
    pv = caps.provenance()
    return box("EXPLAIN INFERENCE", [
        ("Provider", pname(r.provider)),
        ("Model", r.model),
        ("Request", tag(r.usage.request_id or UNAVAILABLE, pv["request_id"])),
        ("Router Decision", router),
        ("Finish", r.finish_reason or UNAVAILABLE),
        None,
        ("Placement", cap(caps.gpu_placement)),
        ("KV Occupancy", cap(caps.kv_occupancy)),
        ("Graph Breaks", cap(caps.graph_breaks)),
        ("Kernel Fusion", cap(caps.kernel_fusion)),
    ])


def explain_analyze(r: InferenceResult, caps: ProviderCapabilities | None = None) -> str:  # caps kept for API symmetry
    u = r.usage
    return box("EXPLAIN ANALYZE", [
        ("Input Tokens", tag(n(u.input_tokens), OBSERVED)),
        ("Output Tokens", tag(n(u.output_tokens), OBSERVED)),
        ("Cached Tokens", tag(n(u.cached_tokens), OBSERVED)),
        ("Reasoning Tokens", tag(n(u.reasoning_tokens), OBSERVED)),
        ("Tool Tokens", tag(n(u.tool_tokens), OBSERVED)),
        ("Total Tokens", tag(n(u.total_tokens), OBSERVED)),
        ("TTFT", tag(secs(r.ttft_s), OBSERVED) if r.ttft_s is not None else f"{UNAVAILABLE} (not streamed)"),
        ("Total Latency", tag(secs(r.latency_s), OBSERVED)),
        ("Retries", str(r.retries)),
        ("Estimated Cost", tag(money(r), DERIVED) if r.cost_usd is not None else money(r)),
    ])


def explain_memory(m: dict) -> str:
    """Render a MEMORY_LOOKUP event payload. Only fields the store really produced."""
    from .style import paint
    top = UNAVAILABLE if m.get("top_score") is None else f"{m['top_score']:.4f}  (bm25, higher is better)"
    rows = [
        ("Query", m["query"]),
        ("Retrieval", m["retrieval"]),
        ("Candidates", str(m["candidates"])),
        ("Returned", str(m["returned"])),
        ("Top Score", top),
        ("Source", ", ".join(m["sources"]) or UNAVAILABLE),
        ("PII Filter", m["pii_filter"]),
        ("Tokens Injected", tag(f"~{m['tokens_injected_est']}", ESTIMATED) + paint(" (chars/4)", "dim")),
    ]
    for it in m.get("items", []):
        rows += [None, (f"  #{it['id']}  {it['score']:.4f}", it["text"])]
    return box("EXPLAIN MEMORY", rows)


def step(num: int, text: str, detail: str = "") -> str:
    """One progress line: ① text  detail"""
    from .style import paint
    return f"{paint(f'[{num}]', 'bold', 'cyan')} {text}" + (paint(f"  {detail}", "dim") if detail else "")


def compare(results: list[InferenceResult]) -> str:
    """Side-by-side observability table. No ranking, no winner."""
    from .style import PROVIDER_COLOR, cell, paint
    w = 26
    rows = [
        ("Model", lambda r: r.model),
        ("Input Tokens", lambda r: n(r.usage.input_tokens)),
        ("Output Tokens", lambda r: n(r.usage.output_tokens)),
        ("Reasoning Tokens", lambda r: n(r.usage.reasoning_tokens)),
        ("Cached Tokens", lambda r: n(r.usage.cached_tokens)),
        ("Total Tokens", lambda r: n(r.usage.total_tokens)),
        ("TTFT", lambda r: secs(r.ttft_s)),
        ("Latency", lambda r: secs(r.latency_s)),
        ("Estimated Cost", lambda r: UNAVAILABLE if r.cost_usd is None else f"${r.cost_usd:.6f}"),
    ]
    P = Panel("EXPLAIN COMPARE", "same request, each provider")
    P.row(" " * 18 + "".join(cell(r.provider.upper(), w, False, "bold", PROVIDER_COLOR.get(r.provider, "cyan"))
                              for r in results))
    P.sep("OBSERVED")
    for label, f in rows:
        P.row(paint(f"{label:<18}", "dim") + "".join(cell(f(r), w) for r in results))
    P.sep("READ THIS FIRST")
    for line in ("Observability comparison, not a benchmark: no ranking, no winner.",
                 "Providers tokenize and execute differently, so token counts are not",
                 "equivalent work. Output: Gemini excludes thinking tokens, OpenAI includes",
                 "them, Anthropic includes them and reports no separate reasoning count.",
                 "Input includes cached tokens for all three. Cost is estimated from a local",
                 "price table, not a provider invoice."):
        P.row(paint(line, "dim"))
    return P.render()
