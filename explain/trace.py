"""Transaction trajectory: a projection of journal events into a vertical trace."""
from __future__ import annotations

from .journal import Journal
from .style import Panel, paint

UP = {"NOT_FOUND": "NOT FOUND"}


def trace_nodes(ev: list[dict]) -> list[dict]:
    """Events -> [{label, children}]. Child events attach to the most recent main node."""
    nodes: list[dict] = []

    def main(label: str, style: str = "") -> None:
        nodes.append({"label": label, "children": [], "style": style})

    def child(text: str) -> None:
        if nodes:
            nodes[-1]["children"].append(text)

    for e in ev:
        t, step = e["type"], (e.get("step") or "")
        if t == "INFERENCE_END":
            main(step.upper())
        elif t == "TOOL_END" and not e.get("txn_id"):
            main(step.upper())
        elif t == "TOOL_END" and e.get("outcome") == "ok":
            child("acknowledged")
        elif t == "MEMORY_LOOKUP":
            main("MEMORY")
        elif t == "POLICY_CHECK":
            main("POLICY")
        elif t == "CHECKPOINT":
            main(f"CHECKPOINT {e['checkpoint_id']}")
        elif t == "STEP_RECOVERED":
            main(f"{step.upper()} (recovered, not re-executed)")
        elif t == "DURABLE_INTENT":
            main(step.upper()); child("durable intent recorded")
        elif t == "SIDE_EFFECT_COMMITTED":
            child("side effect COMMITTED")
        elif t == "ACKNOWLEDGEMENT_LOST":
            child("acknowledgement LOST")
        elif t == "TRANSACTION_COMMIT_UNKNOWN":
            main("COMMIT UNKNOWN", "yellow")
        elif t == "RETRY_REQUESTED":
            main("RETRY REQUESTED")
        elif t == "RETRY_BLOCKED":
            main("RETRY BLOCKED", "red")
        elif t == "RECOVERY_START":
            main(f"RECOVERY from {e.get('checkpoint_id') or 'no checkpoint'}  [{e.get('attempt_id')}]")
        elif t == "RECONCILIATION_START":
            main("STATUS RECONCILE")
        elif t == "RECONCILIATION_RESULT":
            main(UP.get(e["result"], e["result"]), "green" if e["result"] == "COMMITTED" else "yellow")
        elif t == "TRANSACTION_COMMIT" and e.get("via") != "reconciliation":
            main("TRANSACTION COMMITTED", "green")
        elif t == "IDEMPOTENCY_CHECK":
            main("IDEMPOTENCY CHECK")
            child(f"same key re-issued → {e['returned_payment_id']}")
            child("duplicate PREVENTED" if e["duplicate_prevented"] else "duplicate NOT prevented")
        elif t == "TRANSACTION_COMPENSATE":
            main("COMPENSATED", "yellow")
    return nodes


def render_trace(j: Journal, task_id: str) -> str:
    nodes = trace_nodes(j.events(task_id))
    P = Panel("TRACE", task_id)
    for i, nd in enumerate(nodes):
        P.row(paint(nd["label"], "bold", nd["style"]) if nd["style"] else paint(nd["label"], "bold"))
        ch = nd["children"]
        for k, c in enumerate(ch):
            P.row("  " + ("└─ " if k == len(ch) - 1 else "├─ ") + c)
        if i < len(nodes) - 1:
            P.row(paint("  ↓", "dim"))
    return P.render(min_width=60)
