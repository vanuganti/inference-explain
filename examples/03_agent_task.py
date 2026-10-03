"""Live agent task: plan -> fetch live Hacker News data -> research -> summarize.

    EXPLAIN_PROVIDER=gemini python examples/03_agent_task.py
    python examples/03_agent_task.py --multi-provider     # needs both providers
    python examples/03_agent_task.py --roles planner=gemini,research=openai,summarizer=anthropic
    python examples/03_agent_task.py --no-resume          # skip the automatic recovery check
    python examples/03_agent_task.py --task-id <id>       # continue a specific task

The runtime, tools, memory, journal and EXPLAIN AGENT TASK are provider independent.
"""
import _common  # noqa: F401
import argparse
import os
import json
import sys
import time
import urllib.request

from explain.agent import BudgetExceeded, Runtime
from explain.config import ConfigError, Settings, configured_providers
from explain.explain_task import explain_agent_task
from explain.journal import Journal
from explain.providers import get_provider
from explain.schema import InferenceRequest, ProviderError
from explain.console import Out, progress_line
from explain.style import Panel, paint
import textwrap

HN = "https://hacker-news.firebaseio.com/v0"


def _get(url: str):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def hn_top(n: int = 8) -> list[dict]:
    """Live: current top Hacker News stories (public API, no key)."""
    ids = _get(f"{HN}/topstories.json")[:n]
    out = []
    for i in ids:
        s = _get(f"{HN}/item/{i}.json") or {}
        out.append({"title": s.get("title"), "url": s.get("url"), "score": s.get("score"),
                    "comments": s.get("descendants")})
    return out


def result_panel(text: str, width: int = 92) -> str:
    p = Panel("RESULT", "summary")
    for para in [t for t in text.strip().split("\n") if t.strip()]:
        lines = textwrap.wrap(para, width, subsequent_indent="  ") or [""]
        for ln in lines:
            p.row(ln)
        p.row()
    p.items.pop()  # drop trailing blank
    return p.render(min_width=width + 4)


GOAL = "What is the tech community discussing right now, and what should an engineer take from it?"


def run_task(j, st, roles, task_id, stories, out):
    rt = Runtime(j, task_id, GOAL, st.budget_usd, tools={"hn_top": hn_top}, progress=progress_line(out))
    plan = rt.infer("plan", roles["planner"], InferenceRequest(
        system="You are a planning agent. Reply in under 80 words, plain text, no Markdown.",
        prompt=f"Goal: {GOAL}\nData source: current top {stories} Hacker News stories. "
               "List the 3 analysis angles to cover."), role="planner")
    rt.save_state("plan", plan); rt.checkpoint("after_plan")
    data = rt.tool("fetch", "hn_top", n=stories)
    rt.save_state("stories", data); rt.checkpoint("after_fetch")
    research = rt.infer("research", roles["research"], InferenceRequest(
        system="You are a research analyst. Be concrete; under 200 words. Plain text, no Markdown.",
        prompt=f"Plan:\n{plan}\n\nLive Hacker News stories (JSON):\n{json.dumps(data)}\n\n"
               "Analyze the stories along the plan's angles."), role="research")
    rt.save_state("research", research); rt.checkpoint("after_research")
    summary = rt.infer("summarize", roles["summarizer"], InferenceRequest(
        system="You write crisp executive summaries. Plain text for a terminal: no Markdown, no bold. "
               "Each bullet starts with '- ' and is at most 2 sentences.",
        prompt=f"Goal: {GOAL}\n\nResearch notes:\n{research}\n\nSummarize in 4 bullets."),
        role="summarizer")
    rt.save_state("summary", summary); rt.checkpoint("after_summary")
    rt.finish()
    return summary, rt


def count(j, task_id, type_):
    return sum(1 for e in j.events(task_id) if e["type"] == type_)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--multi-provider", action="store_true")
    ap.add_argument("--task-id")
    ap.add_argument("--stories", type=int, default=8)
    ap.add_argument("--roles", help="e.g. planner=gemini,research=openai,summarizer=anthropic")
    ap.add_argument("--no-resume", action="store_true", help="skip the automatic recovery check")
    a = ap.parse_args()
    st = Settings.load()
    try:
        if a.roles:
            roles = {k: get_provider(v) for k, v in (kv.split("=") for kv in a.roles.split(","))}
            missing = {"planner", "research", "summarizer"} - set(roles)
            if missing:
                raise ConfigError(f"--roles missing {sorted(missing)}")
        elif a.multi_provider:
            have = configured_providers()
            if len(have) < 2:
                print(f"--multi-provider needs both providers configured; found {have or 'none'}", file=sys.stderr)
                return 2
            roles = {"planner": get_provider("gemini"), "research": get_provider("openai"),
                     "summarizer": get_provider("gemini")}
        else:
            p = get_provider(st.provider)
            roles = {"planner": p, "research": p, "summarizer": p}
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    out = Out()
    task_id = a.task_id or time.strftime("task-%Y%m%d-%H%M%S")
    j = Journal(st.db_path)
    mode = "multi-provider" if (a.multi_provider or a.roles) else f"single provider ({st.provider})"
    out(paint(f"EXPLAIN AGENT TASK  {task_id}  ·  {mode}", "bold", "cyan"))
    out()
    out(paint("RUN 1  live", "bold") + paint("  plan → fetch live Hacker News → research → summarize", "dim"))
    try:
        summary, rt = run_task(j, st, roles, task_id, a.stories, out)
    except (ProviderError, BudgetExceeded) as e:
        j.emit(task_id, "TASK_END", status="failed", attempt_id="attempt-01")
        j.set_status(task_id, "failed")
        out(paint(f"task failed: {e}", "red"))
        out(f"re-run with --task-id {task_id} to resume.")
        out(); out(explain_agent_task(j, task_id))
        return 1
    out(); out(result_panel(summary)); out()

    if not a.no_resume:
        calls0, tools0, spent0 = count(j, task_id, "INFERENCE_END"), count(j, task_id, "TOOL_RESULT"), rt.spent()
        out(paint("RUN 2  automatic recovery", "bold") + paint("  same task id, expect zero re-execution", "dim"))
        summary2, rt2 = run_task(j, st, roles, task_id, a.stories, out)
        calls1, tools1, spent1 = count(j, task_id, "INFERENCE_END"), count(j, task_id, "TOOL_RESULT"), rt2.spent()
        ok = (calls1 == calls0 and tools1 == tools0 and abs(spent1 - spent0) < 1e-12 and summary2 == summary)
        mark = lambda b: paint("✔ yes", "green") if b else paint("✖ NO", "red")  # noqa: E731
        v = Panel("RECOVERY CHECK", "resume with same task id")
        v.kv("New model calls", f"{calls1 - calls0}  {mark(calls1 == calls0)}", 22)
        v.kv("New tool calls", f"{tools1 - tools0}  {mark(tools1 == tools0)}", 22)
        v.kv("Spend unchanged", f"${spent0:.6f} → ${spent1:.6f}  {mark(abs(spent1 - spent0) < 1e-12)}", 22)
        v.kv("Result identical", mark(summary2 == summary), 22)
        out(); out(v.render()); out()

    out(explain_agent_task(j, task_id))
    path = os.path.join(os.path.dirname(os.path.abspath(st.db_path)), f"{task_id}.txt")
    out.save(path)
    print(paint(f"\nsaved plain-text report: {path}", "dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
