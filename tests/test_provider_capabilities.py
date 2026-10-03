from explain.providers.anthropic import AnthropicProvider
from explain.providers.gemini import GeminiProvider
from explain.providers.openai import OpenAIProvider
from explain.render import explain_inference
from explain.schema import (InferenceResult, InferenceUsage, ProviderCapabilities, UNAVAILABLE)

HOSTED = ("gpu_placement", "kv_occupancy", "graph_breaks", "kernel_fusion")


def providers():
    return [OpenAIProvider("k", "m"), GeminiProvider("k", "m"), AnthropicProvider("k", "m")]


def test_hosted_runtime_internals_are_never_claimed():
    for p in providers():
        caps = p.capabilities()
        for f in HOSTED:
            assert getattr(caps, f) is False, (p.name, f)
            assert caps.provenance()[f] == UNAVAILABLE


def test_hosted_internals_collapse_to_one_unavailable_line():
    for p in providers():
        r = InferenceResult(p.name, "m", "t", InferenceUsage(p.name, "m", "id", 1, 2, total_tokens=3))
        out = explain_inference(r, p.capabilities())
        line = next(l for l in out.splitlines() if "Physical internals" in l)
        assert UNAVAILABLE in line and "hosted provider" in line
        assert "Graph Breaks" not in out  # collapsed, not four rows of noise


def test_physical_rows_expand_when_a_provider_really_exposes_them():
    caps = ProviderCapabilities(kv_occupancy=True)  # e.g. a future vLLM adapter
    r = InferenceResult("vllm", "m", "t", InferenceUsage("vllm", "m", "id", 1, 2, total_tokens=3))
    out = explain_inference(r, caps)
    assert "KV Occupancy" in out and "Physical internals" not in out
    assert caps.provenance()["kv_occupancy"] == "OBSERVED" and caps.provenance()["gpu_placement"] == UNAVAILABLE


def test_provenance_distinguishes_observed_derived_unavailable():
    pv = ProviderCapabilities(token_usage=True, reasoning_tokens=False, streaming_ttft=True).provenance()
    assert pv["input_tokens"] == "OBSERVED" and pv["cost"] == "DERIVED"
    assert pv["reasoning_tokens"] == UNAVAILABLE and pv["gpu_placement"] == UNAVAILABLE


def test_anthropic_reports_no_reasoning_tokens_rather_than_guessing():
    assert AnthropicProvider("k", "m").capabilities().reasoning_tokens is False
