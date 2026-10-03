"""explain-agent explain|trace|events|list <task-id>  (python -m explain ...)"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .config import Settings
from .explain_task import explain_agent_task
from .journal import Journal
from .render import secs
from .style import Panel, paint
from .trace import render_trace

KEYS = ("provider", "model", "tool", "txn_id", "tool_call_id", "idempotency_key", "payment_id", "checkpoint_id",
        "result", "outcome", "from_state", "to_state", "reason", "query", "rule", "name", "status")


def events_table(j: Journal, task_id: str) -> str:
    ev = j.events(task_id)
    if not ev:
        return f"no events for task {task_id}"
    t0 = ev[0]["ts"]
    P = Panel("EVENTS", task_id)
    P.row(paint(f"{'SEQ':>3}  {'+T':>7}  {'ATTEMPT':<12}{'EVENT':<28}{'STEP':<11}DETAIL", "dim"))
    for e in ev:
        detail = "  ".join(f"{k}={e[k]}" for k in KEYS if e.get(k) not in (None, ""))
        P.row(f"{e['seq']:>3}  {e['ts'] - t0:>6.2f}s  {e.get('attempt_id', ''):<12}{e['type']:<28}"
              f"{(e.get('step') or ''):<11}{detail}")
    return P.render()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="explain-agent")
    ap.add_argument("command", choices=["explain", "trace", "events", "list"])
    ap.add_argument("task_id", nargs="?")
    ap.add_argument("--db", help="journal path (default: EXPLAIN_AGENT_DB)")
    ap.add_argument("--json", action="store_true", help="machine-readable output (events only)")
    a = ap.parse_args(argv)
    j = Journal(a.db or Settings.load().db_path)
    if a.command == "list":
        for tid, goal, status, started, ended in j.tasks():
            print(f"{tid:<24}{status:<12}{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}  {goal}")
        return 0
    if not a.task_id:
        ap.error(f"{a.command} needs a task id (see: explain-agent list)")
    if a.command == "events" and a.json:
        print(json.dumps(j.events(a.task_id), indent=2))
        return 0
    print({"explain": explain_agent_task, "trace": render_trace, "events": events_table}[a.command](j, a.task_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
