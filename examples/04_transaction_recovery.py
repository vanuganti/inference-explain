"""Flagship demo: an ambiguous commit, a blocked retry, reconciliation and recovery.

    python examples/04_transaction_recovery.py                       # fail, restart, recover, prove idempotency
    python examples/04_transaction_recovery.py --phase fail          # stop after the failure (process ends)
    python examples/04_transaction_recovery.py --recover <task-id>   # a NEW process recovers from SQLite
    python examples/04_transaction_recovery.py --compensate          # also show compensation
    python examples/04_transaction_recovery.py --fail none           # happy path, no failure injected

Everything below executes for real: a live model call for the plan, a SQLite journal, a separate
SQLite payment service (UNIQUE idempotency key), a deterministic failure injected AFTER the
payment commits, and recovery built from a fresh runtime. Nothing here moves real money.
"""
import _common  # noqa: F401
import argparse
import hashlib
import os
import random
import sys

from explain.agent import Runtime
from explain.config import ConfigError, Settings
from explain.console import Out
from explain.explain_task import explain_agent_task
from explain.journal import Journal
from explain.memory import AgentMemory
from explain.payments import ACK_TIMEOUT, PaymentService
from explain.providers import get_provider
from explain.render import pname
from explain.schema import InferenceRequest, ProviderError
from explain.style import PROVIDER_COLOR, Panel, paint
from explain.trace import render_trace
from explain.transaction import CommitUnknown, RetryBlocked, TransactionManager

GOAL = "Complete approved purchase"
ORDER = {"item": "Acme Cloud seat", "quantity": 3, "unit_price": 14.00, "currency": "USD"}
AMOUNT = ORDER["quantity"] * ORDER["unit_price"]
APPROVED = ["Acme Cloud", "Northwind Software", "Globex Compute"]
LIMIT = 100.00
MEMORY_SEED = [
    ("Preferred vendor for cloud licences is Acme Cloud; renewals go through procurement.", "user"),
    ("Approved vendors: Acme Cloud, Northwind Software, Globex Compute.", "system"),
    ("Purchases above 100 USD need a second approver; below that the requester may self-approve.", "system"),
    ("Contact the Acme Cloud account rep Jane Park at jane.park@acme-cloud.example for quotes.", "user"),
]
PAY_STEP = "payment"
CALL_ID = "payment-call-01"
IND = " " * 16


def lbl(n: int, name: str) -> str:
    return paint(f"[{n}] {name:<12}", "bold", "cyan")


def ids(task_id: str) -> dict:
    """Stable, persisted identities derived from the task id."""
    n = int(hashlib.sha256((task_id + "txn").encode()).hexdigest()[:6], 16) % 9000 + 1000
    return {"txn_id": f"txn-{n}", "idem_key": f"pay_{task_id}_01"}


def open_stack(st, task_id, goal, out, inject=None):
    """Build a runtime + transaction manager + payment service purely from SQLite files."""
    j = Journal(st.db_path)
    rt = Runtime(j, task_id, goal, st.budget_usd)
    tm = TransactionManager(rt)
    seen: dict = {}
    pay = PaymentService(st.payments_db, observer=lambda info: (tm.observe_commit(info), seen.update(info)),
                         inject=inject)
    tm.handlers["payment.refund"] = lambda idempotency_key: pay.compensate(idempotency_key=idempotency_key)
    return j, rt, tm, pay, seen


def phase_fail(st, out, args, task_id) -> None:
    ident = ids(task_id)
    j, rt, tm, pay, seen = open_stack(st, task_id, GOAL, out, None if args.fail == "none" else args.fail)
    provider = get_provider(st.provider)
    col = PROVIDER_COLOR.get(provider.name, "cyan")
    out(paint(f"{rt.attempt_id}", "dim") + paint("  runtime started", "dim"))
    out()

    # [1] plan: a live model call
    plan = rt.infer("plan", provider, InferenceRequest(
        system="You are a procurement agent. Plain text, no Markdown, under 60 words.",
        prompt=f"Order: {ORDER['quantity']} x {ORDER['item']} at {ORDER['unit_price']:.2f} {ORDER['currency']} "
               f"= {AMOUNT:.2f} {ORDER['currency']}. Write a short execution plan: check memory, check policy, "
               "pay, confirm."), role="planner")
    ev = [e for e in j.events(task_id) if e["type"] == "INFERENCE_END"][-1]
    out(lbl(1, "plan") + paint(f"{pname(ev['provider'])}/{ev['model']}", col)
        + paint(f"   {ev['latency_s']:.2f}s   in {ev['usage']['input_tokens']} out {ev['usage']['output_tokens']}", "dim"))

    # [2] memory: a real FTS5 lookup in the separate memory store
    mem = AgentMemory(st.memory_db)
    for text, src in MEMORY_SEED:
        mem.add(text, src)
    lk = rt.memory_lookup("memory", mem, "preferred vendor", k=3)
    top = lk["returned"][0]["text"] if lk["returned"] else ""
    out(lbl(2, "memory") + f"preferred vendor → {'HIT' if lk['returned'] else 'MISS'}"
        + paint(f"   ({len(lk['returned'])}/{lk['candidates']} candidates, top bm25 {lk['top_score']})", "dim"))
    vendor = next((v for v in APPROVED if any(v in r["text"] for r in lk["returned"])), None)

    # [3] policy: a real deterministic rule over the amount and the vendor found in memory
    pol = rt.policy_check("policy", "purchase policy", lambda: {
        "passed": bool(vendor) and AMOUNT <= LIMIT,
        "detail": f"vendor={vendor} amount={AMOUNT:.2f} limit={LIMIT:.2f}"})
    out(lbl(3, "policy") + "purchase policy → "
        + paint("PASSED" if pol["passed"] else "FAILED", "green" if pol["passed"] else "red")
        + paint(f"   ({pol['detail']})", "dim"))
    if not pol["passed"]:
        rt.finish("failed"); out(paint("policy rejected the purchase; no transaction started", "red")); return

    # [4] transaction
    tm.begin(ident["txn_id"], goal=GOAL, amount=AMOUNT, currency=ORDER["currency"])
    tm.register_compensation(ident["txn_id"], "payment.refund", {"idempotency_key": ident["idem_key"]})
    out(lbl(4, "transaction") + f"{ident['txn_id']} START" + paint("   compensation registered (not executed)", "dim"))

    # [5] checkpoint BEFORE the side effect
    rt.checkpoint("cp-05", txn_id=ident["txn_id"], idempotency_key=ident["idem_key"], amount=AMOUNT,
                  currency=ORDER["currency"], next_step=PAY_STEP, plan_chars=len(plan))
    out(lbl(5, "checkpoint") + "cp-05 persisted (before the side effect)")

    # [6] payment: durable intent, stable key, external commit, (injected) lost ack
    out(lbl(6, "payment") + f"charge ${AMOUNT:.2f} {ORDER['currency']}")
    out(f"{IND}idempotency key {ident['idem_key']}")
    charge = lambda: pay.charge(AMOUNT, ORDER["currency"], ident["idem_key"], task_id, ident["txn_id"])  # noqa: E731
    try:
        res = tm.execute_side_effect(ident["txn_id"], CALL_ID, "payment.charge", ident["idem_key"], charge,
                                     PAY_STEP, args={"amount": AMOUNT, "currency": ORDER["currency"]})
        out(f"{IND}side effect: " + paint("COMMITTED", "green") + f"   {res['payment_id']}")
        out(f"{IND}acknowledgement: " + paint("RECEIVED", "green"))
        tm.commit(ident["txn_id"])
        rt.checkpoint("cp-06", txn_id=ident["txn_id"], payment_id=res["payment_id"])
        rt.save_state("payment_id", res["payment_id"])
        return
    except CommitUnknown:
        out(f"{IND}side effect: " + paint("COMMITTED", "green") + f"   {seen.get('payment_id')}  "
            + paint("(seen by the payment service, not by the runtime)", "dim"))
        out(f"{IND}acknowledgement: " + paint("LOST", "red"))
    out()
    out(paint("⚠ COMMIT STATUS UNKNOWN", "bold", "yellow"))
    out()

    # [7] a blind retry is attempted and must be refused by the runtime
    out(lbl(7, "retry") + "requested")
    try:
        tm.execute_side_effect(ident["txn_id"], CALL_ID, "payment.charge", ident["idem_key"], charge, PAY_STEP)
        out(paint("BUG: retry was not blocked", "bold", "red"))
    except RetryBlocked:
        out()
        out(paint("✖ RETRY BLOCKED", "bold", "red") + "\n  pending commit reconciliation")
    out()
    out(paint(f"payment records for this task: {pay.count(task_id)}", "dim"))
    for c in (pay, j):
        c.close()  # the process "ends" here; only SQLite files remain


def phase_recover(st, out, args, task_id) -> None:
    ident = ids(task_id)
    out()
    out(paint("── process restart: fresh Journal, Runtime and PaymentService built from SQLite only ──", "dim"))
    out()
    j, rt, tm, pay, seen = open_stack(st, task_id, "", out)
    goal = next(e for e in j.events(task_id) if e["type"] == "TASK_START")["goal"]
    before = pay.count(task_id)
    cp = j.latest_checkpoint(task_id)
    out(lbl(8, "recovery") + f"{rt.attempt_id}   loaded checkpoint {cp[0] if cp else 'none'}")
    out(f"{IND}checking payment status...")
    results = tm.recover({"payment.charge": lambda key: pay.get_status(idempotency_key=key)})
    if not results:
        out(paint("nothing ambiguous to recover", "dim"))
    for r in results:
        found = pay.get_status(idempotency_key=ident["idem_key"])
        if r["result"] == "COMMITTED":
            out()
            out(paint("✔ EXISTING COMMIT FOUND", "bold", "green")
                + f"\n  payment {found['payment_id']}\n  status {found['status']}")
            out()
            t = tm.txn(r["txn_id"])
            out(paint("✔ TRANSACTION RECOVERED", "bold", "green")
                + f"\n  COMMIT_UNKNOWN → {t['state']}\n  duplicate charge prevented (records {before} → {pay.count(task_id)})")
        else:
            out(paint(f"reconciliation result: {r['result']}", "bold", "yellow"))

    # idempotency: re-issue the very same request, same key
    idem = tm.assert_idempotent(ident["txn_id"], CALL_ID, lambda: pay.charge(
        AMOUNT, ORDER["currency"], ident["idem_key"], task_id, ident["txn_id"]), lambda: pay.count(task_id))
    out()
    P = Panel("IDEMPOTENCY CHECK", "same key, re-issued after recovery")
    P.kv("Key", idem["idempotency_key"], 18).kv("First payment", idem["first_payment_id"], 18)
    P.kv("Re-issued request", "same key", 18).kv("Returned", idem["returned_payment_id"], 18)
    P.kv("Records", f"{idem['records_before']} → {idem['records_after']}", 18)
    P.kv("New side effect", paint("NO" if not idem["new_side_effect"] else "YES", "green" if not idem["new_side_effect"] else "red"), 18)
    P.kv("Duplicate", paint("PREVENTED" if idem["duplicate_prevented"] else "NOT PREVENTED",
                            "bold", "green" if idem["duplicate_prevented"] else "red"), 18)
    out(P.render(min_width=60))
    out()

    if args.compensate:
        # a LATER policy decision rejects the workflow after the payment committed
        aud = rt.policy_check("audit", "post-commit invoice audit",
                              lambda: {"passed": False, "detail": "invoice total does not match the order"})
        out(paint("post-commit audit → ", "bold") + paint("FAILED", "red") + paint(f"   ({aud['detail']})", "dim"))
        done = tm.compensate(ident["txn_id"], reason="post-commit audit failed")
        again = tm.compensate(ident["txn_id"], reason="repeat (must be a no-op)")
        out(paint("COMPENSATION", "bold", "yellow") + f"   executed {len(done)}: "
            + ", ".join(f"{d['name']} → {d['result'].get('status')}" for d in done)
            + f"\n{IND}second request executed {len(again)} (at most once)")
        out(paint("compensation records a reversal; it is not a database rollback", "dim"))
    else:
        out(paint("Compensation    not required", "bold") + paint("   (payment was found COMMITTED)", "dim"))
    rt.finish("recovered")
    for c in (pay, j):
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail", default=ACK_TIMEOUT, choices=[ACK_TIMEOUT, "none"])
    ap.add_argument("--phase", default="all", choices=["all", "fail"])
    ap.add_argument("--recover", metavar="TASK_ID")
    ap.add_argument("--compensate", action="store_true")
    args = ap.parse_args()
    st = Settings.load()
    out = Out()
    try:
        if args.recover:
            task_id = args.recover
            out(paint(f"EXPLAIN AGENT TASK · transaction recovery · {task_id}", "bold", "cyan"))
            phase_recover(st, out, args, task_id)
        else:
            task_id = f"task-{random.randint(1000, 9999)}"
            out(paint("EXPLAIN AGENT TASK · transaction recovery", "bold", "cyan") + paint(f"  {task_id}", "dim"))
            out(paint(f"goal: {GOAL}   failure injection: {args.fail}", "dim"))
            if args.fail == ACK_TIMEOUT:
                out(paint("the acknowledgement is dropped AFTER the payment service commits", "dim"))
            out()
            phase_fail(st, out, args, task_id)
            if args.phase == "fail":
                out(paint(f"process ends here. recover with:  python examples/04_transaction_recovery.py --recover {task_id}", "dim"))
                return 0
            if args.fail == ACK_TIMEOUT:
                phase_recover(st, out, args, task_id)
            else:
                j = Journal(st.db_path)
                j.close()
    except (ConfigError, ProviderError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    j = Journal(st.db_path)
    if args.fail == "none" and not args.recover:  # happy path never reopened a runtime: finish it now
        rt = Runtime(j, task_id, GOAL, st.budget_usd)
        rt.finish("completed")
    out()
    out(explain_agent_task(j, task_id))
    out()
    out(render_trace(j, task_id))
    path = os.path.join(os.path.dirname(os.path.abspath(st.db_path)), f"{task_id}.txt")
    out.save(path)
    print(paint(f"\nsaved plain-text report: {path}", "dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
