import importlib.util
import json
import os

from explain.explain_task import project_task
from explain.journal import Journal
from explain.memory import DEMO_SEED, AgentMemory
from explain.plan import MIN_COVERAGE, build_plan, coverage, render_plan
from explain.providers.base import InferenceProvider
from explain.schema import InferenceRequest, ProviderCapabilities


class P(InferenceProvider):
    """A provider with a fixed pre-flight count; prices come from env, like the real thing."""
    def __init__(self, name, model, n_in):
        super().__init__("k", model); self.name, self.n_in = name, n_in
    def count_tokens(self, req): return self.n_in
    def capabilities(self): return ProviderCapabilities()
    def normalize_usage(self, raw, req): raise NotImplementedError
    def normalize_response(self, raw, req, text=None): raise NotImplementedError
    def _call(self, req): raise NotImplementedError
    def _stream_call(self, req): raise NotImplementedError
    def is_retryable(self, e): return False


def memory():
    m = AgentMemory(":memory:")
    for t, s in DEMO_SEED:
        m.add(t, s)
    return m


def prices(monkeypatch, **per):
    monkeypatch.setattr("explain.config.load_env", lambda *a, **k: None)
    for v in ("OPENAI", "GEMINI", "ANTHROPIC"):  # a developer's .env must not change the test
        monkeypatch.delenv(f"{v}_MODELS", raising=False)
    for name, (i, o) in per.items():
        monkeypatch.setenv(f"{name.upper()}_PRICE_INPUT_PER_M", str(i))
        monkeypatch.setenv(f"{name.upper()}_PRICE_OUTPUT_PER_M", str(o))


def test_memory_answerable_question_plans_no_inference(monkeypatch):
    prices(monkeypatch, openai=(1, 10), gemini=(0.1, 1))
    plan = build_plan("What is the preferred vendor for cloud licences?", "sys", memory(),
                      [P("openai", "o", 50), P("gemini", "g", 50)])
    chosen = plan["candidates"][plan["chosen"]]
    assert chosen["kind"] == "memory" and chosen["est_cost_max"] == 0.0
    assert chosen["evidence"]["coverage"] >= MIN_COVERAGE
    assert all("answered from memory" in c["reason"] for c in plan["candidates"][1:])


def test_otherwise_cheapest_worst_case_wins_and_rejections_carry_numbers(monkeypatch):
    prices(monkeypatch, openai=(5, 50), gemini=(0.1, 1), anthropic=(2, 10))
    plan = build_plan("Draft a polite email about discounts", "sys", memory(),
                      [P("openai", "o", 40), P("gemini", "g", 40), P("anthropic", "a", 40)], output_cap=1000)
    chosen = plan["candidates"][plan["chosen"]]
    assert chosen["label"] == "gemini/g"
    expected = (40 * 0.1 + 1000 * 1) / 1e6
    assert abs(chosen["est_cost_max"] - expected) < 1e-12
    rejected = [c for c in plan["candidates"] if c["kind"] == "model" and not c["chosen"]]
    assert len(rejected) == 2 and all("rejected: worst-case $" in c["reason"] for c in rejected)


def test_unpriced_or_uncountable_candidates_are_rejected_not_guessed(monkeypatch):
    monkeypatch.setenv("EXPLAIN_PRICE_AUTOFETCH", "0")
    prices(monkeypatch, gemini=(0.1, 1))
    for v in ("OPENAI_PRICE_INPUT_PER_M", "OPENAI_PRICE_OUTPUT_PER_M", "OPENAI_MODELS"):
        monkeypatch.delenv(v, raising=False)
    plan = build_plan("Draft a note", "sys", memory(), [P("openai", "o", 40), P("gemini", "g", 40), P("anthropic", "a", None)])
    reasons = {c["label"]: c["reason"] for c in plan["candidates"]}
    assert "no price" in reasons["openai/o"] and "pre-count" in reasons["anthropic/a"]
    assert plan["candidates"][plan["chosen"]]["label"] == "gemini/g"


def test_plan_is_journaled_and_rendered_from_the_event(monkeypatch, tmp_path):
    prices(monkeypatch, openai=(1, 10))
    j = Journal(str(tmp_path / "j.db"))
    plan = build_plan("Draft a note", "sys", memory(), [P("openai", "o", 40)])
    j.emit("t", "PLAN_CHOSEN", "q1", plan=plan)
    ev = Journal(str(tmp_path / "j.db")).events("t")[0]  # fresh connection
    out = render_plan(ev)
    assert "openai/o" in out and "▶" in out and "OBSERVED" in out


def test_coverage_uses_content_words():
    assert coverage("preferred vendor for cloud licences", "Preferred vendor for cloud licences is Acme") == 1.0
    assert coverage("email about discounts", "Laptop refresh happens every 36 months") == 0.0


def load_07():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples"))  # for the example's _common
    spec = importlib.util.spec_from_file_location("ex07", os.path.join(os.path.dirname(__file__), "..",
                                                                        "examples", "07_prompt_layout_cache.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_layouts_differ_only_in_where_the_volatile_text_sits():
    m = load_07()
    book = m.handbook()
    (n1, stable), (n2, volatile) = m.layouts(book, "run-A")
    # stable-first: the two calls share a byte-identical system prompt (a cacheable prefix)
    assert stable(1) == stable(2)
    # volatile-first: the very first line differs, so no two calls share a prefix
    import time
    a = volatile(1); time.sleep(0.003); b = volatile(2)
    assert a.splitlines()[0] != b.splitlines()[0] and a.splitlines()[1:] == b.splitlines()[1:]
    assert len(book) > 15000  # large enough to be a plausible cache candidate (thousands of tokens)


def test_replay_roundtrip_is_identical(stack):
    """export -> import into a fresh in-memory journal -> same projection (what `explain replay` does)."""
    from tests.test_journal_projection import run_scenario
    run_scenario(stack)
    stack.restart()
    recorded = json.loads(json.dumps(stack.j.events(stack.task_id)))
    fresh = Journal(":memory:")
    fresh.import_events(stack.task_id, recorded)
    a, b = project_task(stack.j, stack.task_id), project_task(fresh, stack.task_id)
    a.pop("by"), b.pop("by")
    assert a == b


def test_checked_in_samples_replay_without_keys_or_network():
    from explain.cli import load_recording
    from explain.explain_task import explain_agent_task
    root = os.path.join(os.path.dirname(__file__), "..", "samples")
    for f in sorted(os.listdir(root)):
        tid, j = load_recording(os.path.join(root, f))
        assert "EXPLAIN AGENT TASK" in explain_agent_task(j, tid)


def tiered(name, model, tier, n_in=40):
    p = P(name, model, n_in); p.tier = tier
    return p


def test_pass1_filters_by_task_before_pass2_ranks_by_cost(monkeypatch):
    prices(monkeypatch, openai=(1, 10), gemini=(0.1, 1))  # gemini is the cheapest
    cands = [tiered("openai", "big", "frontier"), tiered("gemini", "mini", "small")]
    easy = build_plan("Draft a polite note", "sys", memory(), cands)
    assert easy["pass1"]["needs"] == "small" and easy["candidates"][easy["chosen"]]["label"] == "gemini/mini"
    hard = build_plan("Compare vendors and recommend one", "sys", memory(), [tiered("openai", "mid", "standard"), *cands])
    assert hard["pass1"]["needs"] == "standard"
    # cheapest model is excluded by capability, not by cost
    assert hard["candidates"][hard["chosen"]]["label"] != "gemini/mini"
    why = {c["label"]: c["reason"] for c in hard["candidates"]}
    assert why["gemini/mini"].startswith("pass 1:")


def test_no_capable_model_means_no_plan_not_a_silent_downgrade(monkeypatch):
    prices(monkeypatch, gemini=(0.1, 1))
    plan = build_plan("Debug this algorithm step by step", "sys", memory(), [tiered("gemini", "mini", "small")])
    assert plan["pass1"]["needs"] == "frontier" and plan["chosen"] is None


def test_plan_view_lists_what_the_demo_cannot_expose(monkeypatch, tmp_path):
    prices(monkeypatch, openai=(1, 10))
    j = Journal(str(tmp_path / "j.db"))
    j.emit("t", "PLAN_CHOSEN", "q1", plan=build_plan("Draft a note", "sys", memory(), [P("openai", "o", 40)]))
    out = render_plan(j.events("t")[0])
    assert "Pass 1" in out and "TIER" in out and "Reuse guarantee" in out and "UNAVAILABLE" in out


def test_models_list_parses_tiers(monkeypatch):
    from explain.config import provider_models
    monkeypatch.setattr("explain.config.load_env", lambda *a, **k: None)
    monkeypatch.setenv("OPENAI_MODELS", "a:small, b ,c:frontier")
    assert provider_models("openai") == [("a", "small"), ("b", "standard"), ("c", "frontier")]
    monkeypatch.setenv("OPENAI_MODELS", "solo")
    assert provider_models("openai") == [("solo", "standard")]


def test_empty_style_is_ignored_on_a_color_terminal(monkeypatch):
    from explain.style import paint
    monkeypatch.setenv("FORCE_COLOR", "1"); monkeypatch.delenv("NO_COLOR", raising=False)
    assert paint("x", "") == "x" and paint("x", "", "bold") == "\x1b[1mx\x1b[0m"
