"""Common EXPLAIN schema. Nothing outside explain/providers/ sees SDK objects."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

UNAVAILABLE = "UNAVAILABLE"
OBSERVED, DERIVED, ESTIMATED = "OBSERVED", "DERIVED", "ESTIMATED"


@dataclass
class ProviderCapabilities:
    token_usage: bool = False
    cached_tokens: bool = False
    reasoning_tokens: bool = False
    tool_tokens: bool = False
    response_id: bool = False
    streaming_ttft: bool = False
    count_tokens: bool = False
    # Serving-runtime telemetry: hosted APIs do not expose these.
    gpu_placement: bool = False
    kv_occupancy: bool = False
    graph_breaks: bool = False
    kernel_fusion: bool = False


    def physical_observed(self) -> list[str]:
        """Physical-internals fields this provider actually exposes (none, for hosted APIs)."""
        return [f for f in ("gpu_placement", "kv_occupancy", "graph_breaks", "kernel_fusion") if getattr(self, f)]

    def provenance(self) -> dict[str, str]:
        """How each EXPLAIN field is known: OBSERVED (the provider/client reported it),
        DERIVED (calculated from observed values), ESTIMATED, or UNAVAILABLE. Never fabricated."""
        flag = lambda b: OBSERVED if b else UNAVAILABLE  # noqa: E731
        return {
            "input_tokens": flag(self.token_usage), "output_tokens": flag(self.token_usage),
            "total_tokens": flag(self.token_usage), "cached_tokens": flag(self.cached_tokens),
            "reasoning_tokens": flag(self.reasoning_tokens), "tool_tokens": flag(self.tool_tokens),
            "request_id": flag(self.response_id), "ttft": flag(self.streaming_ttft),
            "latency": OBSERVED, "cost": DERIVED,
            "gpu_placement": flag(self.gpu_placement), "kv_occupancy": flag(self.kv_occupancy),
            "graph_breaks": flag(self.graph_breaks), "kernel_fusion": flag(self.kernel_fusion),
        }


@dataclass
class InferenceUsage:
    """Token telemetry as the provider reported it. None = not reported."""
    provider: str
    model: Optional[str] = None
    request_id: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    tool_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None  # only Anthropic reports cache writes
    # OpenAI's output_tokens already contains reasoning; Gemini's
    # candidates_token_count does not. Kept so cost and display stay honest.
    output_includes_reasoning: bool = True


@dataclass
class InferenceRequest:
    prompt: str
    system: Optional[str] = None
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    model: Optional[str] = None  # None = provider's configured model
    # mark the system prompt as a cache breakpoint where the provider needs it (Anthropic);
    # OpenAI and Gemini cache stable prefixes automatically.
    cache_system: bool = False


@dataclass
class InferenceResult:
    provider: str
    model: str
    text: str
    usage: InferenceUsage
    finish_reason: Optional[str] = None
    latency_s: float = 0.0
    ttft_s: Optional[float] = None  # only when observed via streaming
    retries: int = 0
    cost_usd: Optional[float] = None
    cost_note: Optional[str] = None
    errors: list[str] = field(default_factory=list)  # errors from retried attempts

    def to_event(self) -> dict:
        d = asdict(self)
        d.pop("text")
        return d


@dataclass
class StreamEvent:
    kind: str  # "delta" | "final"
    text: str = ""
    result: Optional[InferenceResult] = None


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, attempts: int, errors: list[str]):
        super().__init__(f"[{provider}] {message}")
        self.provider, self.attempts, self.errors = provider, attempts, errors
