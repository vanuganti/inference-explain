"""Gemini adapter (google-genai SDK, not the deprecated google-generativeai)."""
from __future__ import annotations

from typing import Any, Iterator, Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from ..config import reasoning_effort
from ..schema import (InferenceRequest, InferenceResult, InferenceUsage,
                      ProviderCapabilities)
from .base import InferenceProvider


def _g(obj: Any, *path: str) -> Any:
    for p in path:
        obj = getattr(obj, p, None) if obj is not None else None
    return obj


class GeminiProvider(InferenceProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str):
        super().__init__(api_key, model)
        self.client = genai.Client(api_key=api_key)
        self._thinking_ok = True  # flips off if the model rejects thinking_level

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            token_usage=True, cached_tokens=True, reasoning_tokens=True,
            tool_tokens=True, response_id=True, streaming_ttft=True,
            count_tokens=True)

    # shared REASONING_EFFORT -> Gemini thinking_level
    _LEVELS = {"none": "MINIMAL", "minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM",
               "high": "HIGH", "xhigh": "HIGH"}

    def _config(self, req: InferenceRequest) -> types.GenerateContentConfig:
        level = self._LEVELS.get(reasoning_effort() or "")
        return types.GenerateContentConfig(
            system_instruction=req.system,
            max_output_tokens=req.max_output_tokens,
            temperature=req.temperature,
            thinking_config=types.ThinkingConfig(thinking_level=level)
            if level and self._thinking_ok else None)

    def _rejects_thinking(self, e: Exception) -> bool:
        """Older Gemini models (thinking_budget era) 400 on thinking_level; drop it once."""
        if (self._thinking_ok and isinstance(e, genai_errors.ClientError)
                and getattr(e, "code", None) == 400 and "think" in str(e).lower()):
            self._thinking_ok = False
            return True
        return False

    def _call(self, req):
        try:
            return self.client.models.generate_content(
                model=req.model or self.model, contents=req.prompt, config=self._config(req))
        except genai_errors.ClientError as e:
            if not self._rejects_thinking(e):
                raise
            return self._call(req)

    def _stream_call(self, req) -> Iterator[tuple[str, Any]]:
        try:
            yield from self._stream_once(req)
        except genai_errors.ClientError as e:
            if not self._rejects_thinking(e):
                raise
            yield from self._stream_once(req)

    def _stream_once(self, req) -> Iterator[tuple[str, Any]]:
        for chunk in self.client.models.generate_content_stream(
                model=req.model or self.model, contents=req.prompt, config=self._config(req)):
            # usage_metadata is cumulative; the last chunk carries the final totals.
            try:
                text = chunk.text or ""
            except Exception:  # noqa: BLE001 - blocked/empty chunk
                text = ""
            yield text, chunk

    def count_tokens(self, req) -> Optional[int]:
        try:
            # the Gemini API's count_tokens takes contents only, so count system + prompt as one text
            text = f"{req.system}\n\n{req.prompt}" if req.system else req.prompt
            r = self.client.models.count_tokens(model=req.model or self.model, contents=text)
            return r.total_tokens
        except Exception:  # noqa: BLE001
            return None

    def normalize_usage(self, raw, req) -> InferenceUsage:
        u = _g(raw, "usage_metadata")
        return InferenceUsage(
            provider=self.name,
            model=_g(raw, "model_version") or req.model or self.model,
            request_id=_g(raw, "response_id"),
            input_tokens=_g(u, "prompt_token_count"),
            output_tokens=_g(u, "candidates_token_count"),
            cached_tokens=_g(u, "cached_content_token_count"),
            reasoning_tokens=_g(u, "thoughts_token_count"),
            tool_tokens=_g(u, "tool_use_prompt_token_count"),
            total_tokens=_g(u, "total_token_count"),
            output_includes_reasoning=False)

    def normalize_response(self, raw, req, text=None) -> InferenceResult:
        usage = self.normalize_usage(raw, req)
        fr = _g(raw, "candidates")
        fr = _g(fr[0], "finish_reason") if fr else None
        if text is None:
            try:
                text = raw.text or ""
            except Exception:  # noqa: BLE001
                text = ""
        return InferenceResult(
            provider=self.name, model=usage.model or self.model, text=text,
            usage=usage, finish_reason=getattr(fr, "name", fr))

    def is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, genai_errors.ServerError):
            return True
        if isinstance(exc, genai_errors.APIError):
            return getattr(exc, "code", None) in (408, 429)
        return isinstance(exc, (ConnectionError, TimeoutError))
