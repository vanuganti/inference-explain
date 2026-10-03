"""Provider-independent agent runtime: durable steps, tools, state, memory lookups, policy.

The only code that touches a model is Runtime.infer(), through InferenceProvider.
Every action is appended to the journal as an event; EXPLAIN is a projection of those
events. Completed steps are recorded in a ledger so a resumed task does not re-execute
them ("recovered from journal"). That is recovery, not idempotency: idempotency is about
reissued side effects, see transaction.py.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import asdict
from typing import Any, Callable, Optional

from .journal import Journal
from .memory import AgentMemory
from .providers.base import InferenceProvider
from .redact import summarize_args
from .schema import InferenceRequest, ProviderError


class BudgetExceeded(Exception):
    pass


def _h(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


class Runtime:
    def __init__(self, journal: Journal, task_id: str, goal: str, budget_usd: float,
                 tools: Optional[dict[str, Callable[..., Any]]] = None,
                 progress: Optional[Callable[[str, str, dict], None]] = None):
        self.j, self.task_id, self.goal, self.budget = journal, task_id, goal, budget_usd
        self.tools = tools or {}
        self.progress = progress or (lambda kind, step, info: None)
        prior = self.j.events(task_id)
        resumed = bool(prior)
        # attempt 1 = TASK_START; each TASK_RESUME opens the next attempt
        self.attempt_no = 1 + sum(1 for e in prior if e["type"] == "TASK_RESUME") + (1 if resumed else 0)
        self.attempt_id = f"attempt-{self.attempt_no:02d}"
        self.j.task(task_id, goal)
        self.emit("TASK_RESUME" if resumed else "TASK_START", goal=goal, budget_usd=budget_usd)

    def emit(self, type_: str, step: Optional[str] = None, **payload: Any) -> None:
        self.j.emit(self.task_id, type_, step, attempt_id=self.attempt_id, **payload)

    # ---- spend -----------------------------------------------------------
    def spent(self) -> float:
        return sum(e.get("cost_usd") or 0.0 for e in self.j.events(self.task_id)
                   if e["type"] == "INFERENCE_END")

    # ---- durable steps ---------------------------------------------------
    def step(self, step: str, fn: Callable[[], Any], ident: str = "", kind: str = "step") -> Any:
        """Run fn once per task. On resume the recorded result is returned, fn is not called."""
        key = _h(step, kind, ident)
        cached = self.j.done(self.task_id, key)
        if cached is not None:
            self.emit("STEP_RECOVERED", step, step_key=key, kind=kind)
            self.progress("recovered", step, {})
            return cached["value"]
        value = fn()
        self.j.commit_step(self.task_id, key, {"value": value})
        return value

    def infer(self, step: str, provider: InferenceProvider, req: InferenceRequest, role: str = "") -> str:
        model = req.model or provider.model
        key = _h(step, "inference", provider.name, model, req.system or "", req.prompt)
        cached = self.j.done(self.task_id, key)
        if cached is not None:
            self.emit("STEP_RECOVERED", step, step_key=key, kind="inference")
            self.progress("recovered", step, {})
            return cached["text"]
        if self.spent() >= self.budget:
            self.emit("BUDGET_EXCEEDED", step, spent_usd=self.spent())
            raise BudgetExceeded(f"budget ${self.budget:.2f} reached")
        self.emit("INFERENCE_START", step, provider=provider.name, model=model, role=role, step_key=key)
        final = None
        try:
            for ev in provider.stream(req):  # streaming so TTFT is observable
                if ev.kind == "final":
                    final = ev.result
        except ProviderError as e:
            self.emit("INFERENCE_ERROR", step, provider=provider.name, model=model,
                      retries=e.attempts - 1, errors=e.errors)
            raise
        self.emit("INFERENCE_END", step, role=role, inference_request_id=final.usage.request_id,
                  **final.to_event())
        self.progress("inference", step, final.to_event())
        self.j.commit_step(self.task_id, key, {"text": final.text})
        return final.text

    def tool(self, step: str, name: str, **args: Any) -> Any:
        key = _h(step, "tool", name, repr(sorted(args.items())))
        cached = self.j.done(self.task_id, key)
        if cached is not None:
            self.emit("STEP_RECOVERED", step, step_key=key, kind="tool", tool=name)
            self.progress("recovered", step, {})
            return cached["value"]
        call_id = f"{step}-call-01"
        self.emit("TOOL_START", step, tool=name, tool_call_id=call_id, args=summarize_args(args))
        t0 = time.perf_counter()
        try:
            value = self.tools[name](**args)
        except Exception as e:  # noqa: BLE001
            self.emit("TOOL_END", step, tool=name, tool_call_id=call_id, outcome="error",
                      error=f"{type(e).__name__}: {e}", latency_s=time.perf_counter() - t0)
            raise
        lat = time.perf_counter() - t0
        self.emit("TOOL_END", step, tool=name, tool_call_id=call_id, outcome="ok", latency_s=lat)
        self.progress("tool", step, {"tool": name, "latency_s": lat})
        self.j.commit_step(self.task_id, key, {"value": value})
        return value

    # ---- agent memory (a separate store, see memory.py) ------------------
    def memory_lookup(self, step: str, memory: AgentMemory, query: str, k: int = 3) -> dict:
        def run() -> dict:
            lk = memory.search(query, k)
            self.emit("MEMORY_LOOKUP", step, query=query, retrieval=lk.retrieval,
                      candidates=lk.candidates, returned=len(lk.returned), top_score=lk.top_score,
                      sources=lk.sources, pii_filter=lk.pii_filter,
                      tokens_injected_est=lk.tokens_injected_est, hit=lk.hit,
                      items=[{"id": r["id"], "text": r["text"], "score": r["score"]} for r in lk.returned])
            self.progress("memory", step, asdict(lk))
            return asdict(lk)
        return self.step(step, run, ident=query, kind="memory")

    # ---- policy ------------------------------------------------------------
    def policy_check(self, step: str, rule: str, fn: Callable[[], dict]) -> dict:
        def run() -> dict:
            r = fn()
            self.emit("POLICY_CHECK", step, rule=rule, result="PASSED" if r["passed"] else "FAILED",
                      detail=r.get("detail", ""))
            self.progress("policy", step, r)
            return r
        return self.step(step, run, ident=rule, kind="policy")

    # ---- execution state (not agent memory) -------------------------------
    def save_state(self, key: str, value: Any) -> None:
        self.j.state_set(self.task_id, key, value)
        self.emit("STATE_WRITE", key=key)

    def load_state(self, key: str) -> Any:
        return self.j.state_get(self.task_id, key)

    def checkpoint(self, checkpoint_id: str, **state: Any) -> None:
        self.j.checkpoint(self.task_id, checkpoint_id, state)
        self.emit("CHECKPOINT", checkpoint_id=checkpoint_id)

    def finish(self, status: str = "completed") -> None:
        self.emit("TASK_END", status=status, spent_usd=self.spent())
        self.j.set_status(self.task_id, status)
