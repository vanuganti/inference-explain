"""Persistent agent memory (SQLite FTS5). Distinct from the task journal.

Retrieval is lexical (FTS5 + bm25), not semantic: there are no embeddings, so there
are no similarity scores, only bm25 relevance. Scores shown are real bm25 values.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field

PII_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone": re.compile(r"(?<!\d)(?:\+?\d{1,2}[ -]?)?\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}(?!\d)"),
    "ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    "card": re.compile(r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)"),
}


def filter_pii(text: str) -> tuple[str, dict[str, int]]:
    found: dict[str, int] = {}
    for kind, pat in PII_PATTERNS.items():
        text, n = pat.subn(f"[REDACTED:{kind}]", text)
        if n:
            found[kind] = n
    return text, found


@dataclass
class Lookup:
    query: str
    retrieval: str
    candidates: int
    returned: list[dict] = field(default_factory=list)
    top_score: float | None = None
    pii_filter: str = "PASSED"
    tokens_injected_est: int = 0
    sources: list[str] = field(default_factory=list)

    @property
    def hit(self) -> bool:
        return bool(self.returned)


class AgentMemory:
    RETRIEVAL = "FTS5 bm25 (porter stemming; no embeddings)"

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_items (
              id INTEGER PRIMARY KEY, text TEXT UNIQUE NOT NULL, source TEXT NOT NULL,
              created REAL DEFAULT (strftime('%s','now')));
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(text, tokenize='porter');""")

    def close(self) -> None:
        self.db.close()

    def add(self, text: str, source: str = "user") -> bool:
        """Insert unless the exact text exists. Returns True if newly stored."""
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO memory_items (text, source) VALUES (?,?)", (text, source))
            if cur.rowcount:
                self.db.execute("INSERT INTO memory_fts (rowid, text) VALUES (?,?)", (cur.lastrowid, text))
        return bool(cur.rowcount)

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]

    def search(self, query: str, k: int = 3) -> Lookup:
        terms = re.findall(r"\w+", query.lower())
        res = Lookup(query=query, retrieval=self.RETRIEVAL, candidates=0)
        if not terms:
            return res
        match = " OR ".join(f'"{t}"' for t in terms)
        res.candidates = self.db.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH ?",
                                         (match,)).fetchone()[0]
        rows = self.db.execute(
            "SELECT i.id, i.text, i.source, bm25(memory_fts) FROM memory_fts "
            "JOIN memory_items i ON i.id = memory_fts.rowid "
            "WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts) LIMIT ?", (match, k)).fetchall()
        redacted: dict[str, int] = {}
        for id_, text, source, bm in rows:
            clean, found = filter_pii(text)
            for kind, n in found.items():
                redacted[kind] = redacted.get(kind, 0) + n
            res.returned.append({"id": id_, "text": clean, "source": source, "score": round(-bm, 4)})
        if rows:
            res.top_score = res.returned[0]["score"]
        res.sources = sorted({r["source"] for r in res.returned})
        if redacted:
            res.pii_filter = "REDACTED " + ", ".join(f"{n} {kind}" for kind, n in redacted.items())
        res.tokens_injected_est = sum(len(r["text"]) for r in res.returned) // 4  # chars/4: an estimate
        return res
