"""The task trajectory: plan every step before running, then compare the run with the plan.

A trajectory is an ordered list of steps (model calls and tool calls). Each model step is planned
with the same two passes as a single call (plan.py): pass 1 keeps the models whose tier fits the
step, pass 2 picks the cheapest worst case. The trajectory adds what only a multi-step task has:
a later step's input includes upstream output, so its input estimate carries an allowance for it,
and the summed worst case is checked against the task budget BEFORE anything runs.

The plan is journaled as one TRAJECTORY_PLANNED event; the view is rendered from that event and
the recorded INFERENCE_END / TOOL_END events of the run. A hosted API gives no foresight on output
length, so every number here is an upper bound or an ESTIMATE, labelled as such.
"""
from __future__ import annotations

from typing import Any, Optional

from .journal import Journal
from .memory import AgentMemory
from .plan import build_plan
from .providers.base import InferenceProvider
from .schema import ESTIMATED, UNAVAILABLE
from .style import Panel, cell, paint


def build_trajectory(steps: list[dict], providers: list[InferenceProvider], budget_usd: float) -> dict[str, Any]:
    """steps: [{name, kind: "inference"|"tool", ...}].
    inference: task (text pass 1 classifies), system, prompt (template), out_cap, after (upstream step names)
    tool:      tool, est_tokens (ESTIMATED size of what it returns)"""
    nomem = AgentMemory(":memory:")  # a trajectory step is never answered from memory
    out_tokens: dict[str, int] = {}
    planned: list[dict] = []
    for s in steps:
        if s["kind"] == "tool":
            out_tokens[s["name"]] = s["est_tokens"]
            planned.append({"name": s["name"], "kind": "tool", "tool": s["tool"], "est_output": s["est_tokens"]})
            continue
        allowance = sum(out_tokens.get(u, 0) for u in s.get("after", []))
        plan = build_plan(s["prompt"], s["system"], nomem, providers, s["out_cap"], task=s["task"], extra_input=allowance)
        chosen = plan["candidates"][plan["chosen"]] if plan["chosen"] is not None else None
        out_tokens[s["name"]] = s["out_cap"]
        planned.append({"name": s["name"], "kind": "inference", "task": plan["pass1"]["task"],
                        "needs": plan["pass1"]["needs"], "upstream_allowance": allowance,
                        "chosen": None if chosen is None else {k: chosen[k] for k in (
                            "label", "provider", "model", "tier", "est_input", "est_output_max", "est_cost_max")},
                        "rejected": [{"label": c["label"], "reason": c["reason"]}
                                     for c in plan["candidates"] if not c["chosen"] and c["kind"] == "model"]})
    infeasible = [p["name"] for p in planned if p["kind"] == "inference" and p["chosen"] is None]
    total = sum(p["chosen"]["est_cost_max"] or 0.0 for p in planned if p["kind"] == "inference" and p["chosen"])
    unpriced = [p["name"] for p in planned if p["kind"] == "inference" and p["chosen"]
                and p["chosen"]["est_cost_max"] is None]
    return {"steps": planned, "worst_case_usd": total, "budget_usd": budget_usd, "infeasible": infeasible,
            "unpriced": unpriced, "fits_budget": not infeasible and not unpriced and total <= budget_usd}


def _delta(act: Optional[int], est: Optional[int], over_only: bool = True) -> str:
    return "" if act is None or est is None else f"{act - est:+d}"


def render_trajectory(j: Journal, task_id: str) -> str:
    """EXPLAIN TRAJECTORY: the planned path, then plan vs actual for what ran. Reads events only."""
    ev = j.events(task_id)
    pe = next((e for e in ev if e["type"] == "TRAJECTORY_PLANNED"), None)
    if pe is None:
        return f"no trajectory was planned for task {task_id}"
    tr = pe["trajectory"]
    ends = {e["step"]: e for e in ev if e["type"] == "INFERENCE_END"}
    # what was asked for: a provider may serve an alias (e.g. "-latest") under another name
    asked = {e["step"]: f"{e['provider']}/{e['model']}" for e in ev if e["type"] == "INFERENCE_START"}
    tools = {e["step"]: e for e in ev if e["type"] == "TOOL_END"}
    P = Panel("EXPLAIN TRAJECTORY", f"{task_id}  plan vs actual")
    P.kv("Worst case", f"${tr['worst_case_usd']:.6f}  {paint(ESTIMATED, 'dim')}  vs budget ${tr['budget_usd']:.2f}  "
         + (paint("✔ fits", "green") if tr["fits_budget"] else paint("✖ does not fit", "red")), 12)
    P.sep("TRAJECTORY  (planned before the first call; est. cost = worst case, at each step's output cap)")
    P.row(paint(cell("#", 3) + cell("STEP", 11) + cell("TASK", 26) + cell("MODEL (TIER)", 44) + cell("EST IN", 8, True)
                + cell("OUT ≤", 8, True) + cell("COST ≤", 12, True), "dim"))
    for i, s in enumerate(tr["steps"], 1):
        if s["kind"] == "tool":
            P.row(cell(i, 3) + cell(s["name"], 11) + cell("tool call", 26) + cell(f"tool: {s['tool']}", 44)
                  + cell("-", 8, True) + cell(f"~{s['est_output']:,}", 8, True) + cell("-", 12, True))
            continue
        c = s["chosen"]
        if c is None:
            P.row(cell(i, 3) + cell(s["name"], 11) + cell(s["task"], 26)
                  + paint(f"NO FEASIBLE MODEL (needs tier ≥ {s['needs']})", "red"))
            continue
        cost = UNAVAILABLE if c["est_cost_max"] is None else f"${c['est_cost_max']:.6f}"
        P.row(cell(i, 3) + cell(s["name"], 11) + cell(s["task"], 26) + cell(f"{c['label']} ({c['tier']})", 44, False, "bold")
              + cell("UNAVAILABLE" if c["est_input"] is None else f"{c['est_input']:,}", 8, True)
              + cell(f"{c['est_output_max']:,}", 8, True) + cell(cost, 12, True))
    P.sep("PLAN VS ACTUAL")
    P.row(paint(cell("#", 3) + cell("STEP", 11) + cell("RAN", 44) + cell("IN", 14, True) + cell("OUT", 12, True)
                + cell("COST", 12, True) + "  VERDICT", "dim"))
    spent, ran = 0.0, 0
    for i, s in enumerate(tr["steps"], 1):
        n = s["name"]
        if s["kind"] == "tool":
            t = tools.get(n)
            P.row(cell(i, 3) + cell(n, 11) + cell(f"tool: {s['tool']}" if t else "not run", 44)
                  + cell("", 14, True) + cell("", 12, True) + cell("", 12, True)
                  + (f"  {t['latency_s']:.2f}s {t['outcome']}" if t else ""))
            ran += bool(t)
            continue
        e = ends.get(n)
        if e is None:
            P.row(cell(i, 3) + cell(n, 11) + paint("not run", "dim"))
            continue
        ran += 1
        u, c = e["usage"], s["chosen"] or {}
        flags = []
        if asked.get(n, c.get("label")) != c.get("label"):
            flags.append(f"ran {asked[n]}, planned {c.get('label')}")
        if c.get("est_input") is not None and u.get("input_tokens") is not None and u["input_tokens"] > c["est_input"]:
            flags.append(f"input +{u['input_tokens'] - c['est_input']} over estimate")
        if u.get("output_tokens") is not None and c.get("est_output_max") and u["output_tokens"] > c["est_output_max"]:
            flags.append("output over cap")
        fin = str(e.get("finish_reason") or "")
        if fin and fin.upper() not in ("STOP", "END_TURN", "COMPLETED", "FINISHREASON.STOP"):
            flags.append(f"finish {fin}: answer may be cut off")
        cost = e.get("cost_usd")
        if cost is not None:
            spent += cost
            if c.get("est_cost_max") is not None and cost > c["est_cost_max"]:
                flags.append("cost over bound")
        P.row(cell(i, 3) + cell(n, 11) + cell(f"{e['provider']}/{e['model']}", 44)
              + cell("UNAVAILABLE" if u.get("input_tokens") is None else f"{u['input_tokens']:,}", 14, True)
              + cell("UNAVAILABLE" if u.get("output_tokens") is None else f"{u['output_tokens']:,}", 12, True)
              + cell(UNAVAILABLE if cost is None else f"${cost:.6f}", 12, True) + "  "
              + (paint("▲ " + "; ".join(flags), "yellow") if flags else paint("✔ within plan", "green")))
    P.sep("TOTAL")
    bound = tr["worst_case_usd"]
    P.kv("Steps run", f"{ran} of {len(tr['steps'])}", 22)
    P.kv("Spent", f"${spent:.6f}  of ${bound:.6f} worst case" + (f"  ({spent / bound:.1%} of the bound)" if bound else ""), 22)
    P.sep("NOT EXPOSED BY THIS DEMO")
    for k, why in (("Output length ahead of time", "unknowable; only each step's output cap bounds it"),
                   ("Tool output size", "an ESTIMATE per tool, not observed before the call"),
                   ("Re-planning mid-task", "the trajectory is fixed before step 1; a deviation is flagged, not repaired")):
        P.kv(k, f"{UNAVAILABLE}  ({why})", 30)
    return P.render(min_width=110)
