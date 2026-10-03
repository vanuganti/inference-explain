"""Transaction manager for side effects whose outcome can be unknown.

A timeout does not mean the external action failed: the action may have committed
while only the acknowledgement was lost. This manager therefore:

  1. records DURABLE INTENT before calling the external system,
  2. gives the call a stable idempotency key,
  3. on a lost acknowledgement moves to COMMIT_UNKNOWN and BLOCKS retry (in code),
  4. reconciles by asking the external system, then moves to COMMITTED (or, if the
     system has no record, allows a retry),
  5. supports compensation as a distinct, recorded, at-most-once action.

Limits: this is a reference implementation. External tools do not join an ACID
transaction; compensation is a new action that reverses an effect, not a rollback.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

from .journal import dumps
from .payments import AcknowledgementTimeout
from .redact import summarize_args

ACTIVE, COMMIT_UNKNOWN, COMMITTED, COMPENSATED = "ACTIVE", "COMMIT_UNKNOWN", "COMMITTED", "COMPENSATED"
RETRY_NONE, RETRY_BLOCKED, RETRY_ALLOWED = "NONE", "BLOCKED_PENDING_COMMIT_CHECK", "ALLOWED"


class CommitUnknown(Exception):
    def __init__(self, txn_id: str, tool_call_id: str, idem_key: str):
        super().__init__(f"{txn_id}/{tool_call_id}: side effect may have committed (key {idem_key})")
        self.txn_id, self.tool_call_id, self.idem_key = txn_id, tool_call_id, idem_key


class RetryBlocked(Exception):
    pass


class TransactionManager:
    def __init__(self, rt, handlers: Optional[dict[str, Callable[..., Any]]] = None):
        self.rt, self.j, self.task_id = rt, rt.j, rt.task_id
        self.handlers = handlers or {}  # compensation handlers by name; must be idempotent
        self._observed: set[str] = set()
        self._step: Optional[str] = None

    # ---- storage helpers -------------------------------------------------------
    def txn(self, txn_id: str) -> Optional[dict]:
        r = self.j.db.execute("SELECT txn_id, state, retry_state FROM transactions WHERE txn_id=?",
                              (txn_id,)).fetchone()
        return None if r is None else {"txn_id": r[0], "state": r[1], "retry_state": r[2]}

    def _set_txn(self, txn_id: str, state: Optional[str] = None, retry_state: Optional[str] = None) -> None:
        with self.j.db:
            if state:
                self.j.db.execute("UPDATE transactions SET state=?, updated=? WHERE txn_id=?",
                                  (state, time.time(), txn_id))
            if retry_state:
                self.j.db.execute("UPDATE transactions SET retry_state=?, updated=? WHERE txn_id=?",
                                  (retry_state, time.time(), txn_id))

    def side_effect(self, txn_id: str, tool_call_id: str) -> Optional[dict]:
        r = self.j.db.execute("SELECT tool, idem_key, status, external_id, detail FROM side_effects "
                              "WHERE txn_id=? AND tool_call_id=?", (txn_id, tool_call_id)).fetchone()
        return None if r is None else {"tool": r[0], "idem_key": r[1], "status": r[2],
                                       "external_id": r[3], "detail": r[4]}

    def _set_se(self, txn_id: str, tool_call_id: str, status: str, external_id: Optional[str] = None,
                detail: Any = None) -> None:
        with self.j.db:
            self.j.db.execute(
                "UPDATE side_effects SET status=?, external_id=COALESCE(?, external_id), "
                "detail=COALESCE(?, detail), updated=? WHERE txn_id=? AND tool_call_id=?",
                (status, external_id, None if detail is None else dumps(detail), time.time(), txn_id, tool_call_id))

    # ---- lifecycle ---------------------------------------------------------------
    def begin(self, txn_id: str, **meta: Any) -> None:
        now = time.time()
        with self.j.db:
            cur = self.j.db.execute("INSERT OR IGNORE INTO transactions VALUES (?,?,?,?,?,?)",
                                    (txn_id, self.task_id, ACTIVE, RETRY_NONE, now, now))
        if cur.rowcount:
            self.rt.emit("TRANSACTION_START", txn_id=txn_id, state=ACTIVE, **meta)

    def observe_commit(self, info: dict) -> None:
        """Out-of-band report from the external service after ITS own commit. The runtime
        does not use this to decide anything: its state stays what its acknowledgement says."""
        self._observed.add(info["idempotency_key"])
        self.rt.emit("SIDE_EFFECT_COMMITTED", self._step, source="payment-service observer",
                     txn_id=info.get("transaction_id"), payment_id=info["payment_id"],
                     idempotency_key=info["idempotency_key"], status=info["status"])

    def execute_side_effect(self, txn_id: str, tool_call_id: str, tool: str, idem_key: str,
                            fn: Callable[[], dict], step: str, args: Optional[dict] = None) -> dict:
        self._step = step
        t, row = self.txn(txn_id), self.side_effect(txn_id, tool_call_id)
        if row and row["status"] == "COMMITTED":  # already done: restore, never re-execute
            self.rt.emit("STEP_RECOVERED", step, kind="side_effect", txn_id=txn_id, tool_call_id=tool_call_id)
            return json.loads(row["detail"]) if row["detail"] else {}
        if t["state"] == COMMIT_UNKNOWN or (row and row["status"] in ("COMMIT_UNKNOWN", "INTENT")):
            self.rt.emit("RETRY_REQUESTED", step, txn_id=txn_id, tool_call_id=tool_call_id,
                         idempotency_key=idem_key, reason="caller re-issued after ambiguous outcome")
            self._set_txn(txn_id, retry_state=RETRY_BLOCKED)
            self.rt.emit("RETRY_BLOCKED", step, txn_id=txn_id, tool_call_id=tool_call_id,
                         idempotency_key=idem_key, state=COMMIT_UNKNOWN,
                         reason="pending commit reconciliation")
            raise RetryBlocked(f"{txn_id}/{tool_call_id}: pending commit reconciliation")
        # durable intent BEFORE the external call
        with self.j.db:
            self.j.db.execute("INSERT OR REPLACE INTO side_effects VALUES (?,?,?,?,?,?,NULL,NULL,?)",
                              (txn_id, tool_call_id, self.task_id, tool, idem_key, "INTENT", time.time()))
        self.rt.emit("DURABLE_INTENT", step, txn_id=txn_id, tool_call_id=tool_call_id, tool=tool,
                     idempotency_key=idem_key, args=summarize_args(args or {}))
        self.rt.emit("TOOL_START", step, tool=tool, tool_call_id=tool_call_id, txn_id=txn_id,
                     idempotency_key=idem_key)
        t0 = time.perf_counter()
        try:
            result = fn()
        except (AcknowledgementTimeout, TimeoutError, ConnectionError) as e:
            self.rt.emit("ACKNOWLEDGEMENT_LOST", step, txn_id=txn_id, tool_call_id=tool_call_id,
                         idempotency_key=idem_key, error=f"{type(e).__name__}: {e}")
            self.rt.emit("TOOL_END", step, tool=tool, tool_call_id=tool_call_id, txn_id=txn_id,
                         outcome="no_acknowledgement", latency_s=time.perf_counter() - t0)
            self._set_se(txn_id, tool_call_id, "COMMIT_UNKNOWN")
            self._set_txn(txn_id, COMMIT_UNKNOWN, RETRY_BLOCKED)
            self.rt.emit("TRANSACTION_COMMIT_UNKNOWN", step, txn_id=txn_id, from_state=ACTIVE,
                         to_state=COMMIT_UNKNOWN, retry_state=RETRY_BLOCKED, tool_call_id=tool_call_id,
                         idempotency_key=idem_key)
            raise CommitUnknown(txn_id, tool_call_id, idem_key) from e
        except Exception as e:  # noqa: BLE001 - a definite failure, not an ambiguous one
            self._set_se(txn_id, tool_call_id, "FAILED", detail={"error": str(e)})
            self.rt.emit("TOOL_END", step, tool=tool, tool_call_id=tool_call_id, txn_id=txn_id,
                         outcome="error", error=f"{type(e).__name__}: {e}", latency_s=time.perf_counter() - t0)
            raise
        if idem_key not in self._observed:
            self.rt.emit("SIDE_EFFECT_COMMITTED", step, source="acknowledged", txn_id=txn_id,
                         idempotency_key=idem_key)
        self._set_se(txn_id, tool_call_id, "COMMITTED", result.get("payment_id"), result)
        self.rt.emit("TOOL_END", step, tool=tool, tool_call_id=tool_call_id, txn_id=txn_id,
                     outcome="ok", latency_s=time.perf_counter() - t0)
        return result

    def reconcile(self, txn_id: str, tool_call_id: str, lookup: Callable[[str], Optional[dict]]) -> str:
        """Ask the external system what actually happened. Returns COMMITTED | NOT_FOUND | PENDING."""
        row = self.side_effect(txn_id, tool_call_id)
        self.rt.emit("RECONCILIATION_START", txn_id=txn_id, tool_call_id=tool_call_id,
                     idempotency_key=row["idem_key"], reason="commit status unknown")
        found = lookup(row["idem_key"])
        if found and found["status"] in ("COMMITTED", "COMPENSATED"):
            self._set_se(txn_id, tool_call_id, "COMMITTED", found["payment_id"], found)
            self.rt.emit("RECONCILIATION_RESULT", txn_id=txn_id, tool_call_id=tool_call_id, result="COMMITTED",
                         payment_id=found["payment_id"], external_status=found["status"],
                         idempotency_key=row["idem_key"])
            self._maybe_commit(txn_id, via="reconciliation")
            return "COMMITTED"
        if found:  # PENDING / FAILED at the service: still not safe to retry
            self.rt.emit("RECONCILIATION_RESULT", txn_id=txn_id, tool_call_id=tool_call_id, result="PENDING",
                         external_status=found["status"], idempotency_key=row["idem_key"])
            return "PENDING"
        self._set_se(txn_id, tool_call_id, "NOT_COMMITTED")
        self._set_txn(txn_id, ACTIVE, RETRY_ALLOWED)  # policy: no record => safe to retry
        self.rt.emit("RECONCILIATION_RESULT", txn_id=txn_id, tool_call_id=tool_call_id, result="NOT_FOUND",
                     retry_state=RETRY_ALLOWED, idempotency_key=row["idem_key"])
        return "NOT_FOUND"

    def _maybe_commit(self, txn_id: str, via: str) -> None:
        pending = self.j.db.execute("SELECT COUNT(*) FROM side_effects WHERE txn_id=? AND status!='COMMITTED'",
                                    (txn_id,)).fetchone()[0]
        t = self.txn(txn_id)
        if pending == 0 and t["state"] in (ACTIVE, COMMIT_UNKNOWN):
            prev = t["state"]
            self._set_txn(txn_id, COMMITTED, RETRY_NONE)
            self.rt.emit("TRANSACTION_COMMIT", txn_id=txn_id, from_state=prev, to_state=COMMITTED, via=via)

    def commit(self, txn_id: str) -> None:
        t = self.txn(txn_id)
        unresolved = self.j.db.execute("SELECT tool_call_id, status FROM side_effects WHERE txn_id=? "
                                       "AND status!='COMMITTED'", (txn_id,)).fetchall()
        if unresolved:
            raise RetryBlocked(f"{txn_id}: cannot commit with unresolved side effects {unresolved}")
        if t["state"] == ACTIVE:
            self._maybe_commit(txn_id, via="normal")

    # ---- idempotency proof -------------------------------------------------------
    def assert_idempotent(self, txn_id: str, tool_call_id: str, fn: Callable[[], dict],
                          count: Callable[[], int]) -> dict:
        """Re-issue the same request with the same idempotency key and record what happened."""
        row = self.side_effect(txn_id, tool_call_id)
        before = count()
        returned = fn()
        after = count()
        info = {"txn_id": txn_id, "idempotency_key": row["idem_key"], "first_payment_id": row["external_id"],
                "returned_payment_id": returned.get("payment_id"), "records_before": before,
                "records_after": after, "new_side_effect": after != before,
                "duplicate_prevented": after == before and returned.get("payment_id") == row["external_id"]}
        self.rt.emit("IDEMPOTENCY_CHECK", self._step, **info)
        return info

    # ---- compensation (distinct from rollback) -----------------------------------
    def register_compensation(self, txn_id: str, name: str, args: dict) -> str:
        n = self.j.db.execute("SELECT COUNT(*)+1 FROM compensations WHERE txn_id=?", (txn_id,)).fetchone()[0]
        comp_id = f"{txn_id}-comp-{n:02d}"
        with self.j.db:
            self.j.db.execute("INSERT OR IGNORE INTO compensations VALUES (?,?,?,?,'REGISTERED',NULL)",
                              (comp_id, txn_id, name, dumps(args)))
        self.rt.emit("COMPENSATION_REGISTERED", txn_id=txn_id, comp_id=comp_id, name=name)
        return comp_id

    def compensate(self, txn_id: str, reason: str) -> list[dict]:
        """Run registered compensations that have not run. A second call executes nothing.
        Handlers must themselves be idempotent (a crash mid-handler re-runs that one handler)."""
        out = []
        rows = self.j.db.execute("SELECT comp_id, name, args, status FROM compensations WHERE txn_id=? "
                                 "AND status IN ('REGISTERED','EXECUTING') ORDER BY comp_id DESC",
                                 (txn_id,)).fetchall()
        for comp_id, name, args, status in rows:
            with self.j.db:
                self.j.db.execute("UPDATE compensations SET status='EXECUTING' WHERE comp_id=?", (comp_id,))
            result = self.handlers[name](**json.loads(args))
            with self.j.db:
                self.j.db.execute("UPDATE compensations SET status='EXECUTED', executed_at=? WHERE comp_id=?",
                                  (time.time(), comp_id))
            self._set_txn(txn_id, COMPENSATED, RETRY_NONE)
            self.rt.emit("TRANSACTION_COMPENSATE", txn_id=txn_id, comp_id=comp_id, name=name, reason=reason,
                         to_state=COMPENSATED, result=result)
            out.append({"comp_id": comp_id, "name": name, "result": result})
        return out

    # ---- recovery after restart --------------------------------------------------
    def recover(self, lookups: dict[str, Callable[[str], Optional[dict]]]) -> list[dict]:
        """Rebuild from SQLite only: find ambiguous side effects and reconcile each."""
        cp = self.j.latest_checkpoint(self.task_id)
        self.rt.emit("RECOVERY_START", checkpoint_id=cp[0] if cp else None)
        # an INTENT row with no recorded outcome means the runtime died mid-call: outcome unknown
        for txn_id, call_id, idem in self.j.db.execute(
                "SELECT txn_id, tool_call_id, idem_key FROM side_effects WHERE task_id=? AND status='INTENT'",
                (self.task_id,)).fetchall():
            self._set_se(txn_id, call_id, "COMMIT_UNKNOWN")
            self._set_txn(txn_id, COMMIT_UNKNOWN, RETRY_BLOCKED)
            self.rt.emit("TRANSACTION_COMMIT_UNKNOWN", txn_id=txn_id, from_state=ACTIVE, to_state=COMMIT_UNKNOWN,
                         tool_call_id=call_id, idempotency_key=idem, reason="restart found intent without outcome")
        results = []
        for txn_id, call_id, tool in self.j.db.execute(
                "SELECT txn_id, tool_call_id, tool FROM side_effects WHERE task_id=? AND status='COMMIT_UNKNOWN'",
                (self.task_id,)).fetchall():
            results.append({"txn_id": txn_id, "tool_call_id": call_id,
                            "result": self.reconcile(txn_id, call_id, lookups[tool])})
        self.rt.emit("RECOVERY_END", checkpoint_id=cp[0] if cp else None,
                     reconciled=[r["result"] for r in results])
        return results
