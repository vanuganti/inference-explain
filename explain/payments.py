"""A local stand-in for an external payment provider. It moves no money.

It has its OWN SQLite file and connection, so it behaves like a system the agent
runtime does not control: its commit is durable and independent of the runtime's
journal. The idempotency key is a UNIQUE constraint enforced by SQLite, not by
Python state.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  payment_id TEXT UNIQUE NOT NULL,
  idempotency_key TEXT UNIQUE NOT NULL,
  task_id TEXT, transaction_id TEXT,
  amount REAL NOT NULL, currency TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING','COMMITTED','FAILED','COMPENSATED')),
  created_at REAL NOT NULL, committed_at REAL, compensated_at REAL);
"""
ACK_TIMEOUT = "payment-ack-timeout"


class AcknowledgementTimeout(TimeoutError):
    """The caller's wait for the acknowledgement expired. The charge may have committed."""


class PaymentService:
    def __init__(self, path: str, observer: Optional[Callable[[dict], None]] = None,
                 inject: Optional[str] = None):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)  # explicit transactions
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.observer, self.inject, self._fired = observer, inject, False

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _rec(row: sqlite3.Row, **extra: Any) -> dict:
        d = dict(row)
        d.pop("n", None)
        return {**d, **extra}

    def charge(self, amount: float, currency: str, idempotency_key: str,
               task_id: str = "", transaction_id: str = "") -> dict:
        """Commit a charge exactly once per idempotency key."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            n = self.db.execute("SELECT COALESCE(MAX(n),0)+1 FROM payments").fetchone()[0]
            now = time.time()
            self.db.execute(
                "INSERT INTO payments (payment_id, idempotency_key, task_id, transaction_id, amount,"
                " currency, status, created_at) VALUES (?,?,?,?,?,?,'PENDING',?)",
                (f"payment-{n:04d}", idempotency_key, task_id, transaction_id, amount, currency, now))
            self.db.execute("UPDATE payments SET status='COMMITTED', committed_at=? WHERE idempotency_key=?",
                            (time.time(), idempotency_key))
            self.db.execute("COMMIT")
        except sqlite3.IntegrityError:
            self.db.execute("ROLLBACK")  # same key already used: return the original, no new effect
            row = self.db.execute("SELECT * FROM payments WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            return self._rec(row, deduplicated=True)
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        row = self.db.execute("SELECT * FROM payments WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        rec = self._rec(row, deduplicated=False)
        if self.observer:  # out-of-band: what the service itself saw, after ITS commit
            self.observer(rec)
        # Failure injection happens strictly AFTER the durable commit: the side effect
        # happened, only the acknowledgement is lost.
        if self.inject == ACK_TIMEOUT and not self._fired:
            self._fired = True
            raise AcknowledgementTimeout(f"no acknowledgement for {idempotency_key} within timeout")
        return rec

    def get_status(self, idempotency_key: Optional[str] = None,
                   payment_id: Optional[str] = None) -> Optional[dict]:
        if idempotency_key:
            row = self.db.execute("SELECT * FROM payments WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        else:
            row = self.db.execute("SELECT * FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
        return None if row is None else self._rec(row)

    def compensate(self, idempotency_key: Optional[str] = None,
                   payment_id: Optional[str] = None) -> dict:
        """Record a reversal (not a rollback). Idempotent: a second call changes nothing."""
        cur = self.get_status(idempotency_key, payment_id)
        if cur is None:
            return {"status": "NOT_FOUND"}
        if cur["status"] == "COMPENSATED":
            return {**cur, "already_compensated": True}
        if cur["status"] != "COMMITTED":
            return {**cur, "compensated": False}
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("UPDATE payments SET status='COMPENSATED', compensated_at=? "
                        "WHERE payment_id=? AND status='COMMITTED'", (time.time(), cur["payment_id"]))
        self.db.execute("COMMIT")
        return {**self.get_status(payment_id=cur["payment_id"]), "already_compensated": False}

    def count(self, task_id: Optional[str] = None) -> int:
        """Payment records, optionally only those created for one task."""
        if task_id is None:
            return self.db.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        return self.db.execute("SELECT COUNT(*) FROM payments WHERE task_id=?", (task_id,)).fetchone()[0]
