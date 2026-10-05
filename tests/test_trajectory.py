from explain.journal import Journal
from explain.trajectory import build_trajectory, render_trajectory
from .test_plan_and_layout import P, prices, tiered

STEPS = [
    {"name": "plan", "kind": "inference", "out_cap": 300, "task": "List the 3 angles", "system": "s", "prompt": "p"},
    {"name": "fetch", "kind": "tool", "tool": "hn_top", "est_tokens": 800},
    {"name": "research", "kind": "inference", "out_cap": 600, "after": ["plan", "fetch"],
     "task": "Analyze and compare the stories", "system": "s", "prompt": "p"},
]


def test_each_step_gets_its_own_model_and_upstream_output_raises_the_input_estimate(monkeypatch):
    prices(monkeypatch, openai=(1, 10), gemini=(0.1, 1))
    cands = [tiered("gemini", "mini", "small"), tiered("openai", "mid", "standard")]
    tr = build_trajectory(STEPS, cands, budget_usd=1.0)
    by = {s["name"]: s for s in tr["steps"]}
    assert by["plan"]["chosen"]["label"] == "gemini/mini"        # drafting-grade step: cheapest tier fits
    assert by["research"]["chosen"]["label"] == "openai/mid"     # analysis step: pass 1 excludes small
    assert by["research"]["upstream_allowance"] == 300 + 800
    assert by["research"]["chosen"]["est_input"] == 40 + 1100    # pre-flight count + upstream allowance
    assert tr["fits_budget"] and tr["worst_case_usd"] > 0


def test_over_budget_or_infeasible_trajectories_do_not_fit(monkeypatch):
    prices(monkeypatch, gemini=(0.1, 1))
    cands = [tiered("gemini", "mini", "small")]
    assert not build_trajectory(STEPS, cands, budget_usd=1.0)["fits_budget"]       # research needs standard
    assert build_trajectory(STEPS, cands, 1.0)["infeasible"] == ["research"]
    ok = [tiered("gemini", "mid", "standard")]
    assert not build_trajectory(STEPS, ok, budget_usd=0.0000001)["fits_budget"]


def test_view_is_rendered_from_events_and_flags_deviations(monkeypatch, tmp_path):
    prices(monkeypatch, gemini=(0.1, 1))
    tr = build_trajectory(STEPS, [tiered("gemini", "mid", "standard")], 1.0)
    j = Journal(str(tmp_path / "j.db"))
    j.emit("t", "TRAJECTORY_PLANNED", None, trajectory=tr)
    j.emit("t", "INFERENCE_START", "plan", provider="gemini", model="mid")
    j.emit("t", "INFERENCE_END", "plan", provider="gemini", model="mid-served-alias", cost_usd=0.0001, ttft_s=1, latency_s=1,
           usage={"input_tokens": 40, "output_tokens": 50})
    j.emit("t", "TOOL_END", "fetch", tool="hn_top", outcome="ok", latency_s=0.5)
    j.emit("t", "INFERENCE_END", "research", provider="gemini", model="mid", cost_usd=0.0002, ttft_s=1, latency_s=2,
           usage={"input_tokens": 5000, "output_tokens": 100})
    out = render_trajectory(Journal(str(tmp_path / "j.db")), "t")  # fresh connection
    assert "EXPLAIN TRAJECTORY" in out and "within plan" in out
    assert "ran " not in out                                       # a served alias is not a deviation
    assert "input +" in out and "over estimate" in out             # research input blew past the estimate
    assert "Re-planning mid-task" in out and "UNAVAILABLE" in out


def test_a_different_model_than_planned_is_flagged(monkeypatch, tmp_path):
    prices(monkeypatch, gemini=(0.1, 1))
    tr = build_trajectory(STEPS[:1], [tiered("gemini", "mid", "standard")], 1.0)
    j = Journal(str(tmp_path / "j.db"))
    j.emit("t", "TRAJECTORY_PLANNED", None, trajectory=tr)
    j.emit("t", "INFERENCE_START", "plan", provider="openai", model="other")
    j.emit("t", "INFERENCE_END", "plan", provider="openai", model="other", cost_usd=0.0001, ttft_s=1, latency_s=1,
           usage={"input_tokens": 40, "output_tokens": 50})
    assert "ran openai/other, planned gemini/mid" in render_trajectory(j, "t")


def test_a_cut_off_answer_is_flagged(monkeypatch, tmp_path):
    prices(monkeypatch, gemini=(0.1, 1))
    tr = build_trajectory(STEPS[:1], [tiered("gemini", "mid", "standard")], 1.0)
    j = Journal(str(tmp_path / "j.db"))
    j.emit("t", "TRAJECTORY_PLANNED", None, trajectory=tr)
    j.emit("t", "INFERENCE_START", "plan", provider="gemini", model="mid")
    j.emit("t", "INFERENCE_END", "plan", provider="gemini", model="mid", cost_usd=0.0001, ttft_s=1, latency_s=1,
           finish_reason="MAX_TOKENS", usage={"input_tokens": 40, "output_tokens": 30})
    assert "MAX_TOKENS" in render_trajectory(j, "t")
