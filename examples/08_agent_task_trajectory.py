"""Agent task trajectory: plan every step first, run it, then compare the run with the plan.

    python examples/08_agent_task_trajectory.py
    python examples/08_agent_task_trajectory.py --stories 5

Same task as example 03 (plan -> fetch live Hacker News -> research -> summarize), but each model
step is assigned a model BEFORE the first call: pass 1 keeps the models whose tier fits the step
(<PROVIDER>_MODELS), pass 2 takes the cheapest worst case. The summed worst case must fit the
budget or nothing runs. EXPLAIN TRAJECTORY then shows planned vs actual per step, and flags any
deviation (input above estimate, a different model, cost above the bound).
"""
import _common  # noqa: F401
import argparse
import json
import sys
import time

from explain.agent import BudgetExceeded, Runtime
from explain.config import Settings
from explain.journal import Journal
from explain.providers import get_providers
from explain.schema import InferenceRequest, ProviderError
from explain.style import paint
from explain.trajectory import build_trajectory, render_trajectory

HN = "https://hacker-news.firebaseio.com/v0"
GOAL = "What is the tech community discussing right now, and what should an engineer take from it?"
OUT_CAP = 2000  # reasoning models spend this budget on thinking too (Gemini counts it); a small cap truncates the answer
TOKENS_PER_STORY = 100  # ESTIMATE: title + url + score + comment count as JSON


def hn_top(n: int = 8) -> list[dict]:
    import urllib.request
    def get(url):
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.load(r)
    out = []
    for i in get(f"{HN}/topstories.json")[:n]:
        s = get(f"{HN}/item/{i}.json") or {}
        out.append({"title": s.get("title"), "url": s.get("url"), "score": s.get("score"), "comments": s.get("descendants")})
    return out


def steps(stories: int) -> list[dict]:
    return [
        {"name": "plan", "kind": "inference", "out_cap": OUT_CAP, "task": "List the 3 analysis angles to cover",
         "system": "You are a planning agent. Reply in under 80 words, plain text, no Markdown.",
         "prompt": f"Goal: {GOAL}\nData source: current top {stories} Hacker News stories. List the 3 analysis angles to cover."},
        {"name": "fetch", "kind": "tool", "tool": "hn_top", "est_tokens": stories * TOKENS_PER_STORY},
        {"name": "research", "kind": "inference", "out_cap": OUT_CAP, "after": ["plan", "fetch"],
         "task": "Analyze and compare the stories along the plan's angles",
         "system": "You are a research analyst. Be concrete; under 200 words. Plain text, no Markdown.",
         "prompt": "Plan:\n\nLive Hacker News stories (JSON):\n\nAnalyze the stories along the plan's angles."},
        {"name": "summarize", "kind": "inference", "out_cap": OUT_CAP, "after": ["research"],
         "task": "Summarize the research notes in 4 bullets",
         "system": "You write crisp executive summaries. Plain text, no Markdown. Each bullet starts with '- '.",
         "prompt": f"Goal: {GOAL}\n\nResearch notes:\n\nSummarize in 4 bullets."},
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stories", type=int, default=8)
    a = ap.parse_args()
    st = Settings.load()
    providers = get_providers()
    if not providers:
        print("no provider configured (set <PROVIDER>_API_KEY and <PROVIDER>_MODELS)", file=sys.stderr)
        return 2
    spec = steps(a.stories)
    tr = build_trajectory(spec, providers, st.budget_usd)
    j = Journal(st.db_path)
    task_id = time.strftime("traj-%Y%m%d-%H%M%S")
    rt = Runtime(j, task_id, GOAL, st.budget_usd, tools={"hn_top": hn_top})
    rt.emit("TRAJECTORY_PLANNED", trajectory=tr)
    print(paint(f"EXPLAIN TRAJECTORY  {task_id}   candidates: {', '.join(f'{p.name}/{p.model}' for p in providers)}", "bold", "cyan"))
    if not tr["fits_budget"]:
        why = (f"no feasible model for: {', '.join(tr['infeasible'])}" if tr["infeasible"]
               else f"unpriced step: {', '.join(tr['unpriced'])}" if tr["unpriced"]
               else f"worst case ${tr['worst_case_usd']:.4f} exceeds the ${st.budget_usd:.2f} budget")
        rt.finish("rejected")
        print(render_trajectory(j, task_id)); print(paint(f"\ntrajectory rejected before running: {why}", "red"))
        return 1

    by_label = {f"{p.name}/{p.model}": p for p in providers}
    plan = {s["name"]: s for s in tr["steps"]}
    prov = lambda n: by_label[plan[n]["chosen"]["label"]]  # noqa: E731
    cap = {s["name"]: s.get("out_cap") for s in spec}
    try:
        p1 = rt.infer("plan", prov("plan"), InferenceRequest(system=spec[0]["system"], prompt=spec[0]["prompt"],
                                                              max_output_tokens=cap["plan"]), role="planner")
        data = rt.tool("fetch", "hn_top", n=a.stories)
        r = rt.infer("research", prov("research"), InferenceRequest(
            system=spec[2]["system"], max_output_tokens=cap["research"],
            prompt=f"Plan:\n{p1}\n\nLive Hacker News stories (JSON):\n{json.dumps(data)}\n\n"
                   "Analyze the stories along the plan's angles."), role="research")
        summary = rt.infer("summarize", prov("summarize"), InferenceRequest(
            system=spec[3]["system"], max_output_tokens=cap["summarize"],
            prompt=f"Goal: {GOAL}\n\nResearch notes:\n{r}\n\nSummarize in 4 bullets."), role="summarizer")
        rt.finish()
    except (ProviderError, BudgetExceeded) as e:
        rt.finish("failed")
        print(render_trajectory(j, task_id)); print(paint(f"task failed: {e}", "red"))
        return 1
    print(render_trajectory(j, task_id), "\n")
    print(paint("result:\n", "bold") + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
