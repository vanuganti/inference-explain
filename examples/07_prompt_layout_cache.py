"""Prompt layout and cache reuse, with real cached-token numbers.

    python examples/07_prompt_layout_cache.py

Providers only reuse a cached prefix when the prompt is larger than a minimum size AND the
start of the prompt is byte-identical between calls. This sends a ~4-5k token shared handbook
two ways, twice each, to every configured provider:

  stable-first    the handbook comes first and never changes; only the question varies
  volatile-first  a per-request header comes first, so no two calls share a prefix

Cached tokens are whatever each provider reports (OpenAI and Gemini cache automatically,
Anthropic needs the explicit cache breakpoint this example sets). Caching is best-effort:
if a provider reports 0 or nothing, the table says so rather than implying a hit.
"""
import _common  # noqa: F401
import sys
import time

from explain.agent import Runtime
from explain.config import Settings, configured_providers
from explain.journal import Journal
from explain.providers import get_provider
from explain.render import pname, n, secs
from explain.schema import UNAVAILABLE, InferenceRequest
from explain.style import PROVIDER_COLOR, Panel, cell, paint

AREAS = ["cloud licences", "laptops and peripherals", "software subscriptions", "contractor services",
         "travel and events", "office facilities", "training and conferences", "telecom and network"]


def handbook(rules: int = 64) -> str:
    """A deterministic shared prompt: stable text, large enough to be cacheable."""
    out = ["PROCUREMENT HANDBOOK. Answer every question strictly from these rules, in one short sentence."]
    for i in range(1, rules + 1):
        area = AREAS[i % len(AREAS)]
        out.append(
            f"Rule {i:02d} ({area}): requests under {50 + i * 5} USD may be self-approved by the requester; "
            f"requests between {50 + i * 5} and {500 + i * 25} USD need the team lead; anything above that needs "
            f"finance sign-off and a second quote. Record the vendor, the cost centre {1000 + i * 7} and the "
            f"renewal date for {area} in the purchasing log within {2 + i % 5} business days of approval.")
    return "\n".join(out)


QUESTIONS = ["Who approves a 120 USD cloud licence request?", "Within how many days must travel purchases be logged?"]


def layouts(book: str, run_id: str):
    """(name, system prompt for call k). Only the placement of the volatile part differs.
    run_id makes this run's handbook unique, so call 1 is genuinely cold even if you re-run
    within a provider's cache lifetime; it is identical for every call within the run."""
    stable = f"Handbook revision {run_id}\n{book}"
    return [
        ("stable-first", lambda k: stable),
        ("volatile-first", lambda k: f"Request id: req-{int(time.time() * 1000)}-{k}\n{stable}"),
    ]


def main() -> int:
    st = Settings.load()
    names = configured_providers()
    if not names:
        print("no provider configured (set <PROVIDER>_API_KEY and <PROVIDER>_MODELS)", file=sys.stderr)
        return 2
    book = handbook()
    run_id = time.strftime('%Y%m%d-%H%M%S')
    j = Journal(st.db_path)
    rt = Runtime(j, time.strftime("cache-%Y%m%d-%H%M%S"), "prompt layout vs cache reuse", st.budget_usd)
    print(paint("EXPLAIN CACHE REUSE · prompt layout", "bold", "cyan"))
    rows = []
    for name in names:
        prov = get_provider(name)
        size = prov.count_tokens(InferenceRequest(prompt=QUESTIONS[0], system=book))
        print(paint(f"\n{pname(name)}/{prov.model}: shared prompt = {size if size else UNAVAILABLE} tokens", "dim"))
        for layout, mk in layouts(book, run_id):
            for k, q in enumerate(QUESTIONS, 1):
                step = f"{name}-{layout}-{k}"
                print(f"  {layout:<15} call {k} ...", end=" ", flush=True)
                rt.infer(step, prov, InferenceRequest(prompt=q, system=mk(k), max_output_tokens=2000,
                                                      cache_system=True), role=layout)
                ev = [e for e in j.events(rt.task_id) if e["type"] == "INFERENCE_END" and e["step"] == step][-1]
                print(f"{ev['latency_s']:.2f}s")
                rows.append((name, layout, k, ev))
                time.sleep(2.5 if k == 1 else 0)  # let a just-written cache entry settle
    rt.finish()

    P = Panel("EXPLAIN CACHE REUSE", "what each provider reported for the SAME shared prompt")
    P.row(paint("  " + cell("PROVIDER", 11) + cell("LAYOUT", 16) + cell("CALL", 5) + cell("INPUT", 8, True)
                + cell("CACHED", 13, True) + cell("CACHE WRITE", 14, True) + cell("CACHED %", 13, True)
                + cell("TTFT", 12, True) + cell("COST", 12, True), "dim"))
    last = None
    for name, layout, k, ev in rows:
        if last and last != name:
            P.sep(pname(name))
        last = name
        u = ev["usage"]
        pct = UNAVAILABLE if u.get("cached_tokens") is None or not u.get("input_tokens") else \
            f"{u['cached_tokens'] / u['input_tokens']:.0%}"
        cost = UNAVAILABLE if ev.get("cost_usd") is None else f"${ev['cost_usd']:.6f}"
        P.row("  " + paint(cell(pname(name), 11), PROVIDER_COLOR.get(name, "cyan")) + cell(layout, 16)
              + cell(str(k), 5) + cell(n(u.get("input_tokens")), 8, True) + cell(n(u.get("cached_tokens")), 13, True)
              + cell(n(u.get("cache_write_tokens")), 14, True) + cell(pct, 13, True)
              + cell(secs(ev.get("ttft_s")), 12, True) + cell(cost, 12, True))
    P.sep("HOW TO READ THIS")
    for line in ("Call 1 is cold (the prefix is unique to this run), call 2 repeats it. CACHED is the provider-reported count of input tokens",
                 "served from cache; UNAVAILABLE means the provider reported no figure. Gemini omits the field when it",
                 "reports no cache use, so UNAVAILABLE there means no hit was reported, not a measured zero.",
                 "Only the placement of the volatile text differs between layouts. Cost is estimated from your price table.",
                 "TTFT is time to the first visible text token. Caching is best-effort and provider-specific:",
                 "this is an observation, not a guarantee or a ranking."):
        P.row(paint(line, "dim"))
    print("\n" + P.render(min_width=100))
    return 0


if __name__ == "__main__":
    sys.exit(main())
