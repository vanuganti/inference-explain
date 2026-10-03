"""EXPLAIN INFERENCE as a plan, then EXPLAIN ANALYZE against it.

    python examples/06_explain_plan.py

Question 1 can be answered from memory, so the plan is "don't infer" and no model is called.
Question 2 cannot, so the plan picks among the configured models by worst-case estimated cost
and the real call is compared with the estimate. The plan is journaled as a PLAN_CHOSEN event
and the views are rendered from the recorded events.
"""
import _common  # noqa: F401
import sys
import textwrap
import time

from explain.agent import Runtime
from explain.config import Settings, configured_providers
from explain.journal import Journal
from explain.memory import DEMO_SEED, AgentMemory
from explain.plan import build_plan, render_analyze, render_plan
from explain.providers import get_provider
from explain.schema import InferenceRequest
from explain.style import paint

SYSTEM = "You are a concise procurement assistant. Plain text, no Markdown, at most two sentences."
QUESTIONS = [
    "What is the preferred vendor for cloud licences?",
    "Draft a polite two-sentence email asking Acme Cloud for a ten percent volume discount on three seats.",
]


def main() -> int:
    st = Settings.load()
    names = configured_providers()
    if not names:
        print("no provider configured (set <PROVIDER>_API_KEY and <PROVIDER>_MODEL)", file=sys.stderr)
        return 2
    providers = [get_provider(n) for n in names]
    mem = AgentMemory(st.memory_db)
    for t, s in DEMO_SEED:
        mem.add(t, s)
    j = Journal(st.db_path)
    task_id = time.strftime("plan-%Y%m%d-%H%M%S")
    rt = Runtime(j, task_id, "explain the plan, then analyze it", st.budget_usd)
    print(paint(f"EXPLAIN INFERENCE · plan vs actual   candidates: {', '.join(p.name for p in providers)}", "bold", "cyan"))

    for i, q in enumerate(QUESTIONS, 1):
        step = f"q{i}"
        print(paint(f"\n── Question {i} ──", "bold"))
        plan = build_plan(q, SYSTEM, mem, providers)
        rt.emit("PLAN_CHOSEN", step, plan=plan)
        plan_ev = [e for e in j.events(task_id) if e["type"] == "PLAN_CHOSEN"][-1]
        print(render_plan(plan_ev), "\n")

        chosen = plan["candidates"][plan["chosen"]]
        if chosen["kind"] == "memory":
            answer = chosen["evidence"]["answer"]
            end_ev = None
        else:
            prov = next(p for p in providers if p.name == chosen["provider"])
            answer = rt.infer(step, prov, InferenceRequest(prompt=q, system=SYSTEM,
                                                           max_output_tokens=plan["output_cap"]), role="answer")
            end_ev = [e for e in j.events(task_id) if e["type"] == "INFERENCE_END" and e["step"] == step][-1]
        print(render_analyze(plan_ev, end_ev), "\n")
        print(paint("answer: ", "bold") + textwrap.fill(answer, 100, subsequent_indent="        "))
    rt.finish()
    print(paint(f"\njournal events: {len(j.events(task_id))}  (task {task_id})", "dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
