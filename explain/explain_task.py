"""EXPLAIN AGENT TASK: a projection of journal events. Nothing here executes anything,
and nothing is read from anywhere except the events themselves."""
from __future__ import annotations

from collections import OrderedDict

from .journal import Journal
from .render import n, pname, secs
from .schema import UNAVAILABLE
from .style import PROVIDER_COLOR, Panel, cell, paint

BAR = 14


def project_task(j: Journal, task_id: str) -> dict | None:
    """Journal events -> plain dict. This is the whole EXPLAIN model; render is just a view."""
    ev = j.events(task_id)
    if not ev:
        return None
    first = ev[0]
    start = next(e for e in ev if e["type"] == "TASK_START")
    ends = [e for e in ev if e["type"] == "TASK_END"]
    of = lambda t: [e for e in ev if e["type"] == t]  # noqa: E731

    rows: list[dict] = []
    for e in ev:
        t = e["type"]
        if t == "INFERENCE_END":
            u = e["usage"]
            rows.append(dict(kind="inference", step=e["step"], provider=e["provider"], model=e["model"],
                             input=u.get("input_tokens"), reasoning=u.get("reasoning_tokens"),
                             output=u.get("output_tokens"), ttft=e.get("ttft_s"), latency=e["latency_s"]))
        elif t == "TOOL_END":
            rows.append(dict(kind="tool", step=e["step"], tool=e["tool"], outcome=e.get("outcome"),
                             latency=e["latency_s"], txn=bool(e.get("txn_id"))))
        elif t == "MEMORY_LOOKUP":
            rows.append(dict(kind="memory", step=e["step"], query=e["query"], hit=e["hit"],
                             returned=e["returned"], candidates=e["candidates"], top_score=e["top_score"]))
        elif t == "POLICY_CHECK":
            rows.append(dict(kind="policy", step=e["step"], rule=e["rule"], result=e["result"]))
        elif t == "STEP_RECOVERED":
            rows.append(dict(kind="recovered", step=e["step"]))
        elif t == "INFERENCE_ERROR":
            rows.append(dict(kind="error", step=e["step"], provider=e["provider"], model=e["model"],
                             attempts=e["retries"] + 1))

    by: "OrderedDict[tuple, dict]" = OrderedDict()
    for e in of("INFERENCE_END"):
        a = by.setdefault((e["provider"], e["model"]),
                          dict(calls=0, input=0, output=0, reasoning=None, cost=0.0, priced=0))
        u = e["usage"]
        a["calls"] += 1
        a["input"] += u.get("input_tokens") or 0
        a["output"] += u.get("output_tokens") or 0
        if u.get("reasoning_tokens") is not None:
            a["reasoning"] = (a["reasoning"] or 0) + u["reasoning_tokens"]
        if e.get("cost_usd") is not None:
            a["cost"] += e["cost_usd"]
            a["priced"] += 1

    txn = None
    if of("TRANSACTION_START"):
        states = []
        for e in ev:
            if e["type"] in ("TRANSACTION_COMMIT_UNKNOWN", "TRANSACTION_COMMIT", "TRANSACTION_COMPENSATE"):
                if not states or states[-1] != e["to_state"]:
                    states.append(e["to_state"])
        blocked, recon = of("RETRY_BLOCKED"), of("RECONCILIATION_RESULT")
        idem, intents = of("IDEMPOTENCY_CHECK"), of("DURABLE_INTENT")
        policy = of("POLICY_CHECK")
        registered, compensated = of("COMPENSATION_REGISTERED"), of("TRANSACTION_COMPENSATE")
        txn = dict(
            txn_id=of("TRANSACTION_START")[0]["txn_id"],
            state_path=states,
            idempotency_key=intents[0]["idempotency_key"] if intents else None,
            retry=("BLOCKED pending commit check" if blocked else
                   ("ALLOWED after reconciliation" if any(r["result"] == "NOT_FOUND" for r in recon)
                    else "not requested")),
            reconciliation=recon[-1]["result"] if recon else None,
            checkpoints=[e["checkpoint_id"] for e in of("CHECKPOINT")],
            compensation=("executed" if compensated else
                          ("not required" if registered else "none registered")),
            duplicate=("not tested" if not idem else
                       ("PREVENTED" if idem[-1]["duplicate_prevented"] else "NOT PREVENTED")),
            policy=(policy[-1]["result"] if policy else None),
            attempts=sorted({e.get("attempt_id") for e in ev if e.get("attempt_id")}),
            recoveries=len(of("RECOVERY_START")),
            payment_ids=sorted({e["payment_id"] for e in ev if e.get("payment_id")}),
        )

    status = ends[-1]["status"] if ends else "incomplete"
    return dict(
        task_id=task_id, goal=start["goal"], budget=start["budget_usd"], status=status,
        wall_s=(ends[0]["ts"] - start["ts"]) if ends else None,
        rows=rows, by=by, txn=txn,
        counts=dict(
            inference_calls=len(of("INFERENCE_END")),
            tool_calls=len(of("TOOL_END")),
            memory_lookups=len(of("MEMORY_LOOKUP")),
            journal_events=len(ev),
            state_writes=len(of("STATE_WRITE")),
            checkpoints=len(of("CHECKPOINT")),
            recovered_steps=len(of("STEP_RECOVERED")),
            retries=sum(e.get("retries", 0) for e in of("INFERENCE_END") + of("INFERENCE_ERROR")),
            errors=len(of("INFERENCE_ERROR")),
        ))


def _bar(frac: float, color: str) -> str:
    k = max(1, round(frac * BAR)) if frac > 0 else 0
    return paint("█" * k, color) + paint("░" * (BAR - k), "dim")


STATUS_STYLE = {"completed": ("✔ completed", "green"), "recovered": ("✔ recovered", "cyan"),
                "failed": ("✖ failed", "red"), "incomplete": ("… incomplete (no TASK_END)", "yellow")}


def explain_agent_task(j: Journal, task_id: str) -> str:
    p = project_task(j, task_id)
    if p is None:
        return f"no events for task {task_id}"
    label, color = STATUS_STYLE.get(p["status"], (p["status"], "yellow"))
    P = Panel("EXPLAIN AGENT TASK", task_id)
    P.kv("Goal", p["goal"], 16).kv("Status", paint(label, "bold", color), 16)

    # ---- transaction (only when the journal contains one) ------------------------
    t = p["txn"]
    if t:
        P.sep("TRANSACTION")
        path = " → ".join(t["state_path"]) or "ACTIVE"
        P.kv("Transaction", t["txn_id"], 16)
        P.kv("Payment", paint(path, "bold", "green" if t["state_path"][-1:] == ["COMMITTED"] else "yellow"), 16)
        P.kv("Retry", paint(t["retry"], "red" if t["retry"].startswith("BLOCKED") else "dim"), 16)
        P.kv("Idempotency", t["idempotency_key"] or UNAVAILABLE, 16)
        P.kv("Checkpoint", ", ".join(t["checkpoints"][-1:]) or UNAVAILABLE, 16)
        P.kv("Compensation", t["compensation"], 16)
        P.kv("Duplicate", paint(t["duplicate"], "green" if t["duplicate"] == "PREVENTED" else "dim"), 16)
        P.kv("Policy", t["policy"] or "n/a", 16)
        P.kv("Attempts", f"{len(t['attempts'])}  ({', '.join(t['attempts'])})", 16)

    # ---- steps ---------------------------------------------------------------------
    P.sep("STEPS")
    P.row(paint("  #  " + cell("STEP", 11) + cell("WHAT", 38) + cell("IN", 7, True) + cell("REASONING", 11, True)
                + cell("OUT", 7, True) + cell("TTFT", 8, True) + cell("LATENCY", 9, True) + "  TIMELINE", "dim"))
    top = max([r.get("latency", 0) or 0 for r in p["rows"]] + [0.001])
    for i, r in enumerate(p["rows"], 1):
        head = f"{i:>3}  " + cell(r["step"], 11, False, "bold")
        k = r["kind"]
        if k == "inference":
            col = PROVIDER_COLOR.get(r["provider"], "cyan")
            P.row(head + paint(cell(f"{pname(r['provider'])}/{r['model']}", 38), col)
                  + cell(n(r["input"]), 7, True) + cell(n(r["reasoning"]), 11, True) + cell(n(r["output"]), 7, True)
                  + cell(secs(r["ttft"]), 8, True) + cell(secs(r["latency"]), 9, True)
                  + "  " + _bar(r["latency"] / top, col))
        elif k == "tool":
            bad = r["outcome"] not in ("ok",)
            what = f"tool: {r['tool']}" + (f"  [{r['outcome']}]" if bad else "")
            P.row(head + paint(cell(what, 38), "red" if bad else "yellow") + " " * 33
                  + cell(secs(r["latency"]), 9, True) + "  " + _bar(r["latency"] / top, "red" if bad else "yellow"))
        elif k == "memory":
            sc = UNAVAILABLE if r["top_score"] is None else f"{r['top_score']:.3f}"
            P.row(head + paint(f"memory: '{r['query']}' → {'HIT' if r['hit'] else 'MISS'}  "
                               f"({r['returned']}/{r['candidates']}, top bm25 {sc})", "magenta"))
        elif k == "policy":
            P.row(head + paint(f"policy: {r['rule']} → {r['result']}", "green" if r["result"] == "PASSED" else "red"))
        elif k == "recovered":
            P.row(head + paint("↺ recovered from journal (no re-execution)", "magenta"))
        elif k == "error":
            P.row(head + paint(f"{pname(r['provider'])}/{r['model']} failed after {r['attempts']} attempt(s)", "red"))

    # ---- models / tokens / cost ---------------------------------------------------
    by = p["by"]
    if by:
        P.sep("MODELS")
        for (pr, m), a in by.items():
            P.row(paint(f"{pname(pr)}/{m}", PROVIDER_COLOR.get(pr, "cyan"))
                  + paint(f"   {a['calls']} call{'s' if a['calls'] != 1 else ''}", "dim"))
        P.sep("TOKENS  (as reported by each provider; not directly comparable)")
        for (pr, m), a in by.items():
            P.row(cell(pname(pr), 10, False, PROVIDER_COLOR.get(pr, "cyan"))
                  + f"in {a['input']:>8,}   reasoning {n(a['reasoning']):>11}   out {a['output']:>8,}")
        P.sep("ESTIMATED COST  (calculated from a local price table, not a provider invoice)")
        total, priced, calls = 0.0, 0, 0
        for (pr, m), a in by.items():
            col = PROVIDER_COLOR.get(pr, "cyan")
            calls += a["calls"]; priced += a["priced"]; total += a["cost"]
            P.row(cell(pname(pr), 10, False, col) + (f"${a['cost']:.6f}" if a["priced"] == a["calls"]
                                                      else paint(f"{UNAVAILABLE} (no price for {m})", "dim", "yellow")))
        shown = (f"${total:.6f}" if priced == calls else
                 (f"${total:.6f} (partial: {calls - priced} unpriced call(s))" if priced else UNAVAILABLE))
        P.row(paint(f"{'Total':<10}", "bold") + paint(shown, "bold" if priced else "dim")
              + paint(f"   of ${p['budget']:.2f} budget", "dim"))
        if priced and p["budget"]:
            P.row(" " * 10 + _bar(min(total / p["budget"], 1.0), "green") + paint(f" {total / p['budget']:.1%} used", "dim"))

    # ---- runtime --------------------------------------------------------------------
    c = p["counts"]
    P.sep("RUNTIME")
    P.kv("Elapsed", secs(p["wall_s"]), 18)
    P.kv("Inference calls", str(c["inference_calls"]), 18).kv("Tool calls", str(c["tool_calls"]), 18)
    P.kv("Memory lookups", str(c["memory_lookups"]), 18)
    P.kv("Journal events", str(c["journal_events"]), 18).kv("State writes", str(c["state_writes"]), 18)
    P.kv("Checkpoints", str(c["checkpoints"]), 18).kv("Recovered steps", str(c["recovered_steps"]), 18)
    P.kv("Retries / errors", f"{c['retries']} / {c['errors']}", 18)
    P.row()
    for lab in ("GPU Placement", "KV Occupancy", "Graph Breaks", "Kernel Fusion"):
        P.kv(lab, f"{UNAVAILABLE} (hosted provider)", 18)
    return P.render(min_width=96)
