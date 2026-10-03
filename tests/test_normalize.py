"""Offline tests: SDK-shaped objects -> common schema. No network, no keys."""
import sys, os
from types import SimpleNamespace as NS
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from explain.agent import Runtime
from explain.explain_task import explain_agent_task
from explain.journal import Journal
from explain.providers.base import InferenceProvider
from explain.providers.gemini import GeminiProvider
from explain.providers.openai import OpenAIProvider
from explain.render import explain_analyze, explain_inference
from explain.schema import InferenceRequest, InferenceResult, InferenceUsage, ProviderCapabilities

REQ = InferenceRequest(prompt="hi")


def test_gemini_full(monkeypatch):
    p = GeminiProvider("k", "gem-x")
    raw = NS(response_id="r1", model_version="gem-x-001", candidates=[NS(finish_reason=NS(name="STOP"))],
             usage_metadata=NS(prompt_token_count=4812, candidates_token_count=624, thoughts_token_count=7341,
                               cached_content_token_count=10, tool_use_prompt_token_count=5, total_token_count=12782),
             text="ok")
    r = p.normalize_response(raw, REQ)
    u = r.usage
    assert (u.input_tokens, u.output_tokens, u.reasoning_tokens, u.cached_tokens, u.tool_tokens, u.total_tokens) \
        == (4812, 624, 7341, 10, 5, 12782)
    assert u.request_id == "r1" and u.model == "gem-x-001" and r.finish_reason == "STOP"
    assert not u.output_includes_reasoning


def test_gemini_missing_fields_are_none():
    p = GeminiProvider("k", "gem-x")
    u = p.normalize_usage(NS(usage_metadata=NS(prompt_token_count=3, candidates_token_count=2, total_token_count=5)), REQ)
    assert u.reasoning_tokens is None and u.cached_tokens is None and u.tool_tokens is None
    assert p.normalize_usage(NS(), REQ).input_tokens is None


def test_openai_full_and_missing():
    p = OpenAIProvider("k", "oa-x")
    raw = NS(id="resp_1", model="oa-x-2026", status="completed", incomplete_details=None, output_text="ok",
             usage=NS(input_tokens=100, output_tokens=300, total_tokens=400,
                      input_tokens_details=NS(cached_tokens=64), output_tokens_details=NS(reasoning_tokens=250)))
    r = p.normalize_response(raw, REQ)
    assert (r.usage.reasoning_tokens, r.usage.cached_tokens, r.usage.tool_tokens) == (250, 64, None)
    assert r.finish_reason == "completed" and r.usage.request_id == "resp_1"
    u = p.normalize_usage(NS(id="x", usage=NS(input_tokens=1, output_tokens=2, total_tokens=3)), REQ)
    assert u.reasoning_tokens is None and u.cached_tokens is None


def test_cost_env_file_and_autofetch(monkeypatch, tmp_path):
    import json
    from explain import pricing
    cf = tmp_path / "cost.json"
    monkeypatch.setenv("EXPLAIN_COST_FILE", str(cf))
    monkeypatch.setattr("explain.config.load_env", lambda *a, **k: None)
    for k in ("GEMINI_PRICE_INPUT_PER_M", "GEMINI_PRICE_OUTPUT_PER_M"):
        monkeypatch.delenv(k, raising=False)
    u = InferenceUsage("gemini", model="gem-x-001", input_tokens=1_000_000, output_tokens=1_000_000,
                       reasoning_tokens=1_000_000, output_includes_reasoning=False)
    monkeypatch.setenv("EXPLAIN_PRICE_AUTOFETCH", "0")
    assert pricing.compute_cost(u)[0] is None  # nothing anywhere
    monkeypatch.delenv("EXPLAIN_PRICE_AUTOFETCH")
    monkeypatch.setattr(pricing, "_table", {
        "gemini/gem-x": {"litellm_provider": "gemini", "input_cost_per_token": 1e-6,
                         "output_cost_per_token": 2e-6, "cache_read_input_token_cost": 1e-7}})
    assert pricing.compute_cost(u)[0] == 1 + 2 * 2  # fetched; thinking billed as output
    saved = json.loads(cf.read_text())["gemini"]["gem-x-001"]
    assert saved["input_per_m"] == 1.0 and saved["source"] == "litellm:gem-x"
    monkeypatch.setattr(pricing, "_table", {})  # now served from file, no refetch
    assert pricing.get_price("gemini", "gem-x-001")[1] == "file"
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "5"); monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "5")
    assert pricing.get_price("gemini", "gem-x-001")[1] == "env"


def test_render_shows_unavailable():
    usage = InferenceUsage("gemini", model="m", request_id="r", input_tokens=1, output_tokens=2, total_tokens=3)
    r = InferenceResult("gemini", "m", "t", usage, latency_s=1.0)
    out = explain_inference(r, ProviderCapabilities(token_usage=True)) + explain_analyze(r)
    assert "Physical internals" in out
    assert out.count("UNAVAILABLE") >= 4
    assert "Estimated Cost" in out


class Fake(InferenceProvider):
    def __init__(self, name, model):
        super().__init__("k", model); self.name = name; self.calls = 0
    def count_tokens(self, req): return None
    def capabilities(self): return ProviderCapabilities()
    def normalize_usage(self, raw, req): return InferenceUsage(self.name, self.model, "id", 10, 5, total_tokens=15)
    def normalize_response(self, raw, req, text=None):
        return InferenceResult(self.name, self.model, text or "", self.normalize_usage(raw, req))
    def _call(self, req): self.calls += 1; return object()
    def _stream_call(self, req):
        self.calls += 1; yield "he", None; yield "llo", object()
    def is_retryable(self, e): return True


def test_agent_multi_provider_replay_and_explain():
    j = Journal(":memory:")
    a, b = Fake("gemini", "g1"), Fake("openai", "o1")
    rt = Runtime(j, "t", "goal", 2.0, tools={"t": lambda: [1]})
    rt.infer("plan", a, REQ, "planner"); rt.tool("fetch", "t"); rt.infer("research", b, REQ, "research")
    rt.infer("plan", a, REQ, "planner")  # idempotent replay: no new call
    rt.finish()
    assert a.calls == 1 and b.calls == 1
    out = explain_agent_task(j, "t")
    assert "Gemini/g1" in out and "OpenAI/o1" in out and "recovered from journal (no re-execution)" in out


def test_retry_counts(monkeypatch):
    class Flaky(Fake):
        BACKOFF_S = 0
        def _call(self, req):
            self.calls += 1
            if self.calls < 3: raise RuntimeError("503")
            return object()
    f = Flaky("gemini", "g")
    r = f.generate(REQ)
    assert r.retries == 2 and len(r.errors) == 2


def test_reasoning_effort_shared(monkeypatch):
    monkeypatch.setattr("explain.config.load_env", lambda *a, **k: None)
    monkeypatch.setenv("REASONING_EFFORT", "medium")
    assert OpenAIProvider("k", "m")._params(REQ)["reasoning"] == {"effort": "medium"}
    cfg = GeminiProvider("k", "m")._config(REQ)
    assert cfg.thinking_config.thinking_level.name == "MEDIUM"
    monkeypatch.delenv("REASONING_EFFORT")
    assert "reasoning" not in OpenAIProvider("k", "m")._params(REQ)
    assert GeminiProvider("k", "m")._config(REQ).thinking_config is None


def test_box_never_clips_long_values():
    from explain.render import box
    rid = "resp_" + "a" * 60
    out = box("T", [("Request", rid)])
    assert rid in out
    assert len({len(l) for l in out.split("\n")}) == 1  # borders stay aligned


def test_anthropic_normalization():
    from explain.providers.anthropic import AnthropicProvider
    p = AnthropicProvider("k", "claude-x")
    raw = NS(id="msg_1", model="claude-x", stop_reason="end_turn", stop_details=None,
             content=[NS(type="text", text="hi")],
             usage=NS(input_tokens=100, cache_read_input_tokens=40, cache_creation_input_tokens=10, output_tokens=200))
    r = p.normalize_response(raw, REQ)
    u = r.usage
    assert u.input_tokens == 150            # fresh + cache read + cache write: one meaning across providers
    assert (u.cached_tokens, u.output_tokens, u.total_tokens) == (40, 200, 350)
    assert u.reasoning_tokens is None and u.tool_tokens is None   # not reported -> not invented
    assert r.text == "hi" and r.finish_reason == "end_turn" and u.request_id == "msg_1"
    assert p.normalize_usage(NS(), REQ).input_tokens is None
