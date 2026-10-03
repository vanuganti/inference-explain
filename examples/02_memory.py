"""EXPLAIN MEMORY: real persistent agent memory (SQLite FTS5), separate from the task journal.

    python examples/02_memory.py
    python examples/02_memory.py "laptop refresh policy"

No model is called. Retrieval is lexical (bm25), so there are no embedding similarity scores.
"""
import _common  # noqa: F401
import sys
import time

from explain.agent import Runtime
from explain.config import Settings
from explain.journal import Journal
from explain.memory import AgentMemory
from explain.render import explain_memory
from explain.style import paint

SEED = [
    ("Preferred vendor for cloud licences is Acme Cloud; renewals go through procurement.", "user"),
    ("Approved vendors: Acme Cloud, Northwind Software, Globex Compute.", "system"),
    ("Contact the Acme Cloud account rep Jane Park at jane.park@acme-cloud.example or 512-555-0142 for quotes.", "user"),
    ("Purchases above 100 USD need a second approver; below that the requester may self-approve.", "system"),
    ("Laptop refresh happens every 36 months; exceptions need a manager ticket.", "user"),
    ("Team offsite budget is set each January and tracked by finance.", "derived"),
    ("Payment terms with approved vendors are net 30 unless the contract says otherwise.", "system"),
]


def main() -> int:
    st = Settings.load()
    mem = AgentMemory(st.memory_db)
    added = sum(mem.add(t, s) for t, s in SEED)
    print(paint(f"EXPLAIN MEMORY  {mem.count()} items in {st.memory_db} ({added} newly stored)", "bold", "cyan"), "\n")
    queries = sys.argv[1:] or ["preferred vendor", "approval limit for purchases", "contact Acme rep"]
    j = Journal(st.db_path)
    task_id = time.strftime("memory-%Y%m%d-%H%M%S")
    rt = Runtime(j, task_id, "memory lookups", st.budget_usd)
    for q in queries:
        rt.memory_lookup("memory", mem, q, k=3)
    rt.finish()
    # the view is rendered from the recorded events, not from the live lookup objects
    for e in j.events(task_id):
        if e["type"] == "MEMORY_LOOKUP":
            print(explain_memory(e), "\n")
    print(paint(f"journal events for this run: {len(j.events(task_id))}  (task {task_id})", "dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
