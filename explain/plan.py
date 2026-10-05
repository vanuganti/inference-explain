"""The inference plan: chosen execution path, rejected alternatives, and why.

Hosted APIs hide physical placement, but the decisions the *runtime* makes are real and
inspectable: answer from memory instead of inferring, which configured model to call, and
what that is expected to cost. Every number here is either observed (pre-flight token
counts from the provider), derived (from your price table) or an explicit upper bound.
The plan is journaled as a PLAN_CHOSEN event; EXPLAIN renders from that event.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from .config import TIERS
from .memory import AgentMemory
from .pricing import get_price
from .providers.base import InferenceProvider
from .schema import ESTIMATED, InferenceRequest, OBSERVED, UNAVAILABLE
from .style import Panel, cell, paint

STOP = set("a an the of for to is are was were what which who how do does did in on at by with and or "
           "me my our we us it its this that be can you your please".split())
MIN_COVERAGE = 0.75
RULE = "pass 1: memory if ≥75% covered, else tier must fit the task; pass 2: cheapest worst-case"

# Pass 1 is deliberately crude keyword rules, not a classifier. Cost never enters it.
_FRONTIER = r"\b(prove|derive|debug|refactor|implement|algorithm|architecture|multi-?step|step by step|code)\b"
_STANDARD = r"\b(analy[sz]e|compare|evaluate|trade-?offs?|recommend|justify|strategy|why|reason(ing)?)\b"
# What the demo cannot see on a hosted API. Shown as UNAVAILABLE, never invented.
NOT_EXPOSED = [
    ("KV / cache locality", "no placement or prefix-affinity control on a hosted API"),
    ("Reuse guarantee", "cached state equivalent to recompute is provider-asserted; the client cannot verify it"),
    ("Workload statistics", "no aggregated hit-rate or output-length history is collected, so no cost model is learned"),
    ("Expected output length", "not predictable; the plan carries only the output cap as an upper bound"),
]


def classify(question: str) -> tuple[str, str]:
    """(task kind, minimum tier) from the question text alone."""
    q = question.lower()
    if re.search(_FRONTIER, q):
        return "code / multi-step reasoning", "frontier"
    if re.search(_STANDARD, q):
        return "analysis / reasoning", "standard"
    return "lookup / drafting", "small"


def _terms(text: str) -> set[str]:
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w
            for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP}


def coverage(question: str, text: str) -> float:
    q = _terms(question)
    return len(q & _terms(text)) / len(q) if q else 0.0


def build_plan(question: str, system: str, memory: AgentMemory, providers: list[InferenceProvider],
               output_cap: int = 2000, task: Optional[str] = None, extra_input: int = 0) -> dict[str, Any]:
    """`task` (default: the question) is the text pass 1 classifies; `extra_input` is an ESTIMATED allowance
    added to each pre-flight count for input that does not exist yet (a trajectory step's upstream output)."""
    t0 = time.perf_counter()
    lk = memory.search(question, k=1)
    lookup_s = time.perf_counter() - t0  # the real cost of the "don't infer" check
    best = lk.returned[0] if lk.returned else None
    cov = coverage(question, best["text"]) if best else 0.0
    cands: list[dict] = [{
        "kind": "memory", "label": "answer from memory (no inference)", "est_input": 0, "est_output_max": 0,
        "est_cost_max": 0.0, "feasible": bool(best) and cov >= MIN_COVERAGE, "chosen": False,
        "evidence": {"lookup_s": lookup_s, "coverage": round(cov, 2), "top_score": lk.top_score, "item_id": best["id"] if best else None,
                     "answer": best["text"] if best else None},
    }]
    task, need = classify(task or question)
    for p in providers:
        tier = getattr(p, "tier", "standard")
        req = InferenceRequest(prompt=question, system=system, max_output_tokens=output_cap)
        n_in = p.count_tokens(req)  # OBSERVED: the provider's own pre-flight count
        if n_in is not None:
            n_in += extra_input
        price, origin = get_price(p.name, p.model)
        cost = None
        if n_in is not None and price:
            cost = (n_in * price["input_per_m"] + output_cap * price["output_per_m"]) / 1e6
        capable = TIERS.index(tier) >= TIERS.index(need)
        cands.append({"kind": "model", "label": f"{p.name}/{p.model}", "provider": p.name, "model": p.model,
                      "tier": tier, "capable": capable,
                      "est_input": n_in, "est_output_max": output_cap, "est_cost_max": cost,
                      "feasible": capable and cost is not None, "chosen": False,
                      "evidence": {"price_origin": origin if price else None}})

    mem = cands[0]
    if mem["feasible"]:
        chosen = mem
    else:
        priced = [c for c in cands[1:] if c["feasible"]]
        chosen = min(priced, key=lambda c: c["est_cost_max"]) if priced else None
    for c in cands:
        c["chosen"] = c is chosen
    for c in cands:
        c["reason"] = _reason(c, chosen, cov, best)
    return {"question": question, "rule": RULE, "output_cap": output_cap, "candidates": cands,
            "pass1": {"task": task, "needs": need,
                      "feasible": [c["label"] for c in cands if c["kind"] == "model" and c["capable"]]},
            "chosen": next((i for i, c in enumerate(cands) if c["chosen"]), None)}


def _reason(c: dict, chosen: Optional[dict], cov: float, best: Optional[dict]) -> str:
    if c["chosen"]:
        if c["kind"] == "memory":
            return f"memory item #{c['evidence']['item_id']} covers {cov:.0%} of the question (>= {MIN_COVERAGE:.0%})"
        return f"pass 2: lowest worst-case cost among {c['tier']}+ models that fit the task"
    if c["kind"] == "memory":
        return "no memory hit" if not best else f"top item covers only {cov:.0%} (< {MIN_COVERAGE:.0%})"
    if chosen and chosen["kind"] == "memory":
        return "not needed: answered from memory"
    if not c["capable"]:
        return f"pass 1: tier {c['tier']} is below what the task needs"
    if c["est_input"] is None:
        return "rejected: provider could not pre-count tokens"
    if c["est_cost_max"] is None:
        return "rejected: no price, so cost cannot be bounded"
    if chosen:
        return f"rejected: worst-case ${c['est_cost_max']:.4f} vs ${chosen['est_cost_max']:.4f} chosen"
    return "rejected"


def render_plan(ev: dict) -> str:
    """EXPLAIN INFERENCE (plan) from a PLAN_CHOSEN event payload."""
    cands = ev["plan"]["candidates"]
    ch = ev["plan"]["chosen"]
    P = Panel("EXPLAIN INFERENCE", "plan")
    P.kv("Question", ev["plan"]["question"], 16)
    P.kv("Router Decision", paint(cands[ch]["label"] if ch is not None else "none feasible", "bold", "green"), 16)
    p1 = ev["plan"].get("pass1")
    if p1:
        P.kv("Pass 1", f"task: {p1['task']}  ·  needs tier ≥ {p1['needs']}  ·  "
             f"{len(p1['feasible'])} of {sum(c['kind'] == 'model' for c in cands)} models fit", 16)
    P.kv("Rule", ev["plan"]["rule"], 16)
    P.sep("CANDIDATES  (chosen first; pass 2 ranks by worst-case cost at the output cap)")
    P.row(paint("    " + cell("PLAN", 38) + cell("TIER", 10) + cell("EST IN", 9, True) + cell("EST OUT ≤", 11, True)
                + cell("EST COST ≤", 13, True) + "  WHY", "dim"))
    order = sorted(range(len(cands)), key=lambda i: (i != ch, i))
    for i in order:
        c = cands[i]
        mark = paint("▶ ", "bold", "green") if c["chosen"] else paint("✗ ", "dim")
        cost = UNAVAILABLE if c["est_cost_max"] is None else f"${c['est_cost_max']:.6f}"
        P.row(mark + " " + cell(c["label"], 38, False, "bold" if c["chosen"] else "") +
              cell(c.get("tier", "-"), 10) + cell("UNAVAILABLE" if c["est_input"] is None else f"{c['est_input']:,}", 9, True) +
              cell(f"{c['est_output_max']:,}", 11, True) + cell(cost, 13, True) + "  " + c["reason"])
    P.sep("PROVENANCE")
    P.row(paint(f"EST IN is {OBSERVED} (provider pre-flight count). EST OUT is the {ESTIMATED} upper bound "
                f"(the output cap). EST COST is {ESTIMATED}, derived from your price table. TIER is configured by you "
                f"(<PROVIDER>_MODELS), not measured.", "dim"))
    P.sep("NOT EXPOSED BY THIS DEMO")
    for k, why in NOT_EXPOSED:
        P.kv(k, f"{UNAVAILABLE}  ({why})", 24)
    return P.render(min_width=100)


def render_analyze(plan_ev: dict, end_ev: Optional[dict]) -> str:
    """EXPLAIN ANALYZE against the plan: estimate vs what actually happened."""
    plan = plan_ev["plan"]
    c = plan["candidates"][plan["chosen"]]
    P = Panel("EXPLAIN ANALYZE", "plan vs actual")
    if c["kind"] == "memory":
        P.kv("Executed", "memory lookup (no model call)", 22)
        P.kv("Inference calls", "0", 22)
        P.kv("Tokens", "0 in / 0 out", 22)
        P.kv("Estimated Cost", "$0.000000  (nothing to bill)", 22)
        P.kv("Lookup latency", f"{c['evidence']['lookup_s'] * 1000:.2f} ms  {paint('OBSERVED', 'dim')}", 22)
        return P.render(min_width=84)
    u = end_ev["usage"]
    est_in, act_in = c["est_input"], u.get("input_tokens")
    delta = "" if est_in is None or act_in is None else f"   Δ {act_in - est_in:+d}"
    P.kv("Executed", f"{end_ev['provider']}/{end_ev['model']}", 22)
    P.kv("Input tokens", f"est {est_in:,} → actual {act_in:,}{delta}" if est_in is not None and act_in is not None
         else f"actual {act_in}", 22)
    reasoning = u.get("reasoning_tokens")
    P.kv("Output tokens", f"≤ {plan['output_cap']:,} → actual {u.get('output_tokens'):,}"
         + (f"  (reasoning {reasoning:,}, reported separately)" if reasoning is not None else ""), 22)
    cost = end_ev.get("cost_usd")
    if cost is None:
        P.kv("Estimated Cost", UNAVAILABLE, 22)
    else:
        P.kv("Estimated Cost", f"≤ ${c['est_cost_max']:.6f} → actual ${cost:.6f}"
             f"  ({cost / c['est_cost_max']:.1%} of the bound)", 22)
    cached = u.get("cached_tokens")
    P.kv("Cached input", UNAVAILABLE if cached is None else f"{cached:,} tokens  {paint('OBSERVED', 'dim')}", 22)
    P.kv("Reuse guarantee", f"{UNAVAILABLE}  (provider-asserted; not verifiable by the client)", 22)
    P.kv("TTFT", f"{end_ev['ttft_s']:.2f}s" if end_ev.get("ttft_s") is not None else UNAVAILABLE, 22)
    P.kv("Total latency", f"{end_ev['latency_s']:.2f}s", 22)
    return P.render(min_width=84)
