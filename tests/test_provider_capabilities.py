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


def test_unavailable_renders_for_hosted_fields_and_is_never_a_number():
    for p in providers():
        r = InferenceResult(p.name, "m", "t", InferenceUsage(p.name, "m", "id", 1, 2, total_tokens=3))
        out = explain_inference(r, p.capabilities())
        for label in ("Placement", "KV Occupancy", "Graph Breaks", "Kernel Fusion"):
            line = next(l for l in out.splitlines() if label in l)
            assert UNAVAILABLE in line


def test_provenance_distinguishes_observed_derived_unavailable():
    pv = ProviderCapabilities(token_usage=True, reasoning_tokens=False, streaming_ttft=True).provenance()
    assert pv["input_tokens"] == "OBSERVED" and pv["cost"] == "DERIVED"
    assert pv["reasoning_tokens"] == UNAVAILABLE and pv["gpu_placement"] == UNAVAILABLE


def test_anthropic_reports_no_reasoning_tokens_rather_than_guessing():
    assert AnthropicProvider("k", "m").capabilities().reasoning_tokens is False
