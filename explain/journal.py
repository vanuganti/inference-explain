"""SQLite journal: append-only events plus the durable tables the runtime recovers from.

Events are the record. The other tables (steps, state, checkpoints, transactions,
side_effects, compensations) are current-state indexes the runtime needs to resume.
Nothing here knows about providers, and agent *memory* lives elsewhere (memory.py).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Optional

from .events import EVENT_TYPES
from .redact import redact, redact_text

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  task_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL,
  type TEXT NOT NULL, step TEXT, payload TEXT NOT NULL,
  PRIMARY KEY (task_id, seq));
CREATE TABLE IF NOT EXISTS steps (            -- completed-step ledger (no re-execution)
  task_id TEXT NOT NULL, step_key TEXT NOT NULL, result TEXT NOT NULL,
  PRIMARY KEY (task_id, step_key));
CREATE TABLE IF NOT EXISTS state (            -- execution state, NOT agent memory
  task_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (task_id, key));
CREATE TABLE IF NOT EXISTS checkpoints (
  task_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL, seq INTEGER NOT NULL,
  ts REAL NOT NULL, state TEXT NOT NULL,
  PRIMARY KEY (task_id, checkpoint_id));
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, goal TEXT, status TEXT, started REAL, ended REAL);
CREATE TABLE IF NOT EXISTS transactions (
  txn_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, state TEXT NOT NULL,
  retry_state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS side_effects (
  txn_id TEXT NOT NULL, tool_call_id TEXT NOT NULL, task_id TEXT NOT NULL,
  tool TEXT NOT NULL, idem_key TEXT NOT NULL, status TEXT NOT NULL,
  external_id TEXT, detail TEXT, updated REAL NOT NULL,
  PRIMARY KEY (txn_id, tool_call_id));
CREATE TABLE IF NOT EXISTS compensations (
  comp_id TEXT PRIMARY KEY, txn_id TEXT NOT NULL, name TEXT NOT NULL,
  args TEXT NOT NULL, status TEXT NOT NULL, executed_at REAL);
"""


def dumps(v: Any) -> str:
    return json.dumps(redact(v), default=str)


class Journal:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self._migrate_legacy()
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _migrate_legacy(self) -> None:
        """A journal from before schema v2 is moved aside, never silently reused or overwritten."""
        if self.path == ":memory:" or self.db.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
            return  # brand-new file
        self.db.close()
        backup = f"{self.path}.v1-{int(time.time())}.bak"
        os.replace(self.path, backup)
        print(f"note: old-format journal moved to {backup}; starting a fresh journal")
        self.db = sqlite3.connect(self.path)

    def close(self) -> None:
        self.db.close()

    # ---- events ------------------------------------------------------------
    def emit(self, task_id: str, type_: str, step: Optional[str] = None, **payload: Any) -> None:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type_!r}")
        with self.db:  # atomic seq allocation + insert
            seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE task_id=?",
                                  (task_id,)).fetchone()[0]
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                            (task_id, seq, time.time(), type_, step, dumps(payload)))

    def events(self, task_id: str) -> list[dict]:
        rows = self.db.execute("SELECT seq,ts,type,step,payload FROM events WHERE task_id=? ORDER BY seq",
                               (task_id,)).fetchall()
        return [{"seq": s, "ts": t, "type": ty, "step": st, **json.loads(p)} for s, t, ty, st, p in rows]

    def tasks(self) -> list[tuple]:
        return self.db.execute("SELECT task_id, goal, status, started, ended FROM tasks "
                               "ORDER BY started DESC").fetchall()

    # ---- completed-step ledger ----------------------------------------------
    def done(self, task_id: str, key: str) -> Optional[Any]:
        r = self.db.execute("SELECT result FROM steps WHERE task_id=? AND step_key=?",
                            (task_id, key)).fetchone()
        return None if r is None else json.loads(r[0])

    def commit_step(self, task_id: str, key: str, result: Any) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO steps VALUES (?,?,?)", (task_id, key, dumps(result)))

    # ---- execution state (not agent memory) ----------------------------------
    def state_set(self, task_id: str, key: str, value: Any) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?)", (task_id, key, dumps(value)))

    def state_get(self, task_id: str, key: str) -> Any:
        r = self.db.execute("SELECT value FROM state WHERE task_id=? AND key=?", (task_id, key)).fetchone()
        return None if r is None else json.loads(r[0])

    # ---- checkpoints -----------------------------------------------------------
    def checkpoint(self, task_id: str, checkpoint_id: str, state: dict) -> None:
        with self.db:
            seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM checkpoints WHERE task_id=?",
                                  (task_id,)).fetchone()[0]
            self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?)",
                            (task_id, checkpoint_id, seq, time.time(), dumps(state)))

    def latest_checkpoint(self, task_id: str) -> Optional[tuple[str, dict]]:
        r = self.db.execute("SELECT checkpoint_id, state FROM checkpoints WHERE task_id=? "
                            "ORDER BY seq DESC LIMIT 1", (task_id,)).fetchone()
        return None if r is None else (r[0], json.loads(r[1]))

    # ---- tasks -----------------------------------------------------------------
    def task(self, task_id: str, goal: str = "") -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO tasks VALUES (?,?,?,?,NULL)",
                            (task_id, redact_text(goal), "running", time.time()))

    def set_status(self, task_id: str, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE tasks SET status=?, ended=? WHERE task_id=?",
                            (status, time.time(), task_id))
