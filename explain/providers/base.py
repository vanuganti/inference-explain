"""Provider-neutral interface. Adapters own SDK types; callers get EXPLAIN types."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Iterator, Optional

from ..pricing import compute_cost
from ..schema import (InferenceRequest, InferenceResult, InferenceUsage,
                      ProviderCapabilities, ProviderError, StreamEvent)


class InferenceProvider(ABC):
    name: str = "base"
    tier: str = "standard"  # capability tier for the plan's pass 1: small | standard | frontier
    MAX_ATTEMPTS = 3
    BACKOFF_S = 1.0

    def __init__(self, api_key: str, model: str):
        self.api_key, self.model = api_key, model

    # ---- public interface -------------------------------------------------
    def generate(self, req: InferenceRequest) -> InferenceResult:
        t0 = time.perf_counter()
        raw, retries, errors = self._retry(lambda: self._call(req))
        return self._finish(self.normalize_response(raw, req), t0, None, retries, errors)

    def stream(self, req: InferenceRequest) -> Iterator[StreamEvent]:
        """Yield text deltas, then one final event. TTFT = first non-empty delta.
        Retries only happen before the first chunk arrives."""
        t0 = time.perf_counter()
        errors: list[str] = []
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            started, ttft, parts, last = False, None, [], None
            try:
                for text, raw in self._stream_call(req):
                    if raw is not None:
                        last = raw
                    if text:
                        if ttft is None:
                            ttft = time.perf_counter() - t0
                        started = True
                        parts.append(text)
                        yield StreamEvent("delta", text=text)
                res = self.normalize_response(last, req, text="".join(parts))
                yield StreamEvent("final", result=self._finish(res, t0, ttft, attempt - 1, errors))
                return
            except Exception as e:  # noqa: BLE001 - classified below
                errors.append(f"{type(e).__name__}: {e}")
                if started or not self.is_retryable(e) or attempt == self.MAX_ATTEMPTS:
                    raise ProviderError(self.name, errors[-1], attempt, errors) from e
                time.sleep(self.BACKOFF_S * 2 ** (attempt - 1))

    @abstractmethod
    def count_tokens(self, req: InferenceRequest) -> Optional[int]:
        """Pre-flight input token count, or None if the provider can't say."""

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    def normalize_usage(self, raw: Any, req: InferenceRequest) -> InferenceUsage:
        """SDK response/usage object -> InferenceUsage. Missing fields stay None."""

    @abstractmethod
    def normalize_response(self, raw: Any, req: InferenceRequest,
                           text: Optional[str] = None) -> InferenceResult: ...

    # ---- adapter hooks -----------------------------------------------------
    @abstractmethod
    def _call(self, req: InferenceRequest) -> Any: ...

    @abstractmethod
    def _stream_call(self, req: InferenceRequest) -> Iterator[tuple[str, Any]]:
        """Yield (text_delta, raw_or_None); raw carries the latest full response/usage."""

    @abstractmethod
    def is_retryable(self, exc: Exception) -> bool: ...

    # ---- shared ------------------------------------------------------------
    def _retry(self, fn: Callable[[], Any]) -> tuple[Any, int, list[str]]:
        errors: list[str] = []
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                return fn(), attempt - 1, errors
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
                if not self.is_retryable(e) or attempt == self.MAX_ATTEMPTS:
                    raise ProviderError(self.name, errors[-1], attempt, errors) from e
                time.sleep(self.BACKOFF_S * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    @staticmethod
    def _finish(res: InferenceResult, t0: float, ttft: Optional[float],
                retries: int, errors: list[str]) -> InferenceResult:
        res.latency_s = time.perf_counter() - t0
        res.ttft_s, res.retries, res.errors = ttft, retries, errors
        res.cost_usd, res.cost_note = compute_cost(res.usage)
        return res
