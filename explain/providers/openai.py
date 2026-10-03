"""OpenAI adapter (official SDK, Responses API)."""
from __future__ import annotations

from typing import Any, Iterator, Optional

import openai as sdk  # absolute import: this module is explain.providers.openai

from ..config import reasoning_effort
from ..schema import (InferenceRequest, InferenceResult, InferenceUsage,
                      ProviderCapabilities)
from .base import InferenceProvider


def _g(obj: Any, *path: str) -> Any:
    for p in path:
        obj = getattr(obj, p, None) if obj is not None else None
    return obj


class OpenAIProvider(InferenceProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str):
        super().__init__(api_key, model)
        # max_retries=0: EXPLAIN does the retrying so retries are observable.
        self.client = sdk.OpenAI(api_key=api_key, max_retries=0)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            token_usage=True, cached_tokens=True, reasoning_tokens=True,
            tool_tokens=False, response_id=True, streaming_ttft=True,
            count_tokens=True)

    def _params(self, req: InferenceRequest) -> dict:
        p: dict[str, Any] = {"model": req.model or self.model, "input": req.prompt}
        if req.system:
            p["instructions"] = req.system
        if req.max_output_tokens:
            p["max_output_tokens"] = req.max_output_tokens
        if req.temperature is not None:
            p["temperature"] = req.temperature
        effort = reasoning_effort()
        if effort:  # for reasoning models
            p["reasoning"] = {"effort": effort}
        return p

    def _call(self, req):
        return self.client.responses.create(**self._params(req))

    def _stream_call(self, req) -> Iterator[tuple[str, Any]]:
        with self.client.responses.stream(**self._params(req)) as s:
            for ev in s:
                if ev.type == "response.output_text.delta":
                    yield ev.delta, None
            yield "", s.get_final_response()

    def count_tokens(self, req) -> Optional[int]:
        try:
            r = self.client.responses.input_tokens.count(
                model=req.model or self.model, input=req.prompt,
                **({"instructions": req.system} if req.system else {}))
            return r.input_tokens
        except Exception:  # noqa: BLE001 - endpoint may be unsupported for a model
            return None

    def normalize_usage(self, raw, req) -> InferenceUsage:
        u = _g(raw, "usage")
        return InferenceUsage(
            provider=self.name, model=_g(raw, "model") or req.model or self.model,
            request_id=_g(raw, "id"),
            input_tokens=_g(u, "input_tokens"),
            output_tokens=_g(u, "output_tokens"),
            cached_tokens=_g(u, "input_tokens_details", "cached_tokens"),
            reasoning_tokens=_g(u, "output_tokens_details", "reasoning_tokens"),
            tool_tokens=None,
            total_tokens=_g(u, "total_tokens"),
            output_includes_reasoning=True)

    def normalize_response(self, raw, req, text=None) -> InferenceResult:
        usage = self.normalize_usage(raw, req)
        status = _g(raw, "status")
        reason = _g(raw, "incomplete_details", "reason")
        finish = f"{status}:{reason}" if reason else status
        return InferenceResult(
            provider=self.name, model=usage.model or self.model,
            text=text if text is not None else (_g(raw, "output_text") or ""),
            usage=usage, finish_reason=finish)

    def is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, (sdk.APIConnectionError, sdk.RateLimitError, sdk.InternalServerError)):
            return True
        return isinstance(exc, sdk.APIStatusError) and exc.status_code in (408, 409, 429)
