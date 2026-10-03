import pytest

from explain.transaction import CommitUnknown


def test_reconcile_finds_existing_commit(stack):
    with pytest.raises(CommitUnknown):
        stack.pay_once()
    result = stack.tm.reconcile(stack.TXN, stack.CALL, lambda k: stack.pay.get_status(idempotency_key=k))
    assert result == "COMMITTED"
    assert stack.tm.txn(stack.TXN)["state"] == "COMMITTED"
    assert stack.pay.count() == 1  # no second payment
    ev = [e for e in stack.j.events(stack.task_id) if e["type"] == "TRANSACTION_COMMIT"][0]
    assert (ev["from_state"], ev["to_state"], ev["via"]) == ("COMMIT_UNKNOWN", "COMMITTED", "reconciliation")


def test_reconcile_not_found_allows_a_retry_per_policy(tmp_path):
    from tests.conftest import Stack
    s = Stack(tmp_path, inject=None)
    s.tm.begin(s.TXN)
    # an intent that never reached the service (e.g. crash before sending)
    s.j.db.execute("INSERT INTO side_effects VALUES (?,?,?,?,?,?,NULL,NULL,0)",
                   (s.TXN, s.CALL, s.task_id, "payment.charge", s.KEY, "COMMIT_UNKNOWN"))
    s.j.db.commit()
    s.tm._set_txn(s.TXN, "COMMIT_UNKNOWN", "BLOCKED_PENDING_COMMIT_CHECK")
    assert s.tm.reconcile(s.TXN, s.CALL, lambda k: s.pay.get_status(idempotency_key=k)) == "NOT_FOUND"
    assert s.tm.txn(s.TXN) == {"txn_id": s.TXN, "state": "ACTIVE", "retry_state": "ALLOWED"}
    res = s.pay_once()  # retry is now permitted, and uses the SAME idempotency key
    assert res["idempotency_key"] == s.KEY and s.pay.count() == 1


def test_recovery_with_fresh_runtime_reconciles(stack):
    with pytest.raises(CommitUnknown):
        stack.pay_once()
    stack.restart()
    out = stack.tm.recover({"payment.charge": lambda k: stack.pay.get_status(idempotency_key=k)})
    assert [r["result"] for r in out] == ["COMMITTED"]
    assert stack.tm.txn(stack.TXN)["state"] == "COMMITTED" and stack.pay.count() == 1
    assert {"RECOVERY_START", "RECONCILIATION_START", "RECONCILIATION_RESULT", "RECOVERY_END"} <= set(stack.types())


def test_crash_between_intent_and_outcome_is_treated_as_unknown(stack):
    # runtime died after DURABLE_INTENT, before recording any outcome
    stack.j.db.execute("INSERT INTO side_effects VALUES (?,?,?,?,?,?,NULL,NULL,0)",
                       (stack.TXN, stack.CALL, stack.task_id, "payment.charge", stack.KEY, "INTENT"))
    stack.j.db.commit()
    stack.pay.inject = None
    stack.charge()  # ...but the charge did reach the service
    stack.restart()
    out = stack.tm.recover({"payment.charge": lambda k: stack.pay.get_status(idempotency_key=k)})
    assert out[0]["result"] == "COMMITTED" and stack.pay.count() == 1
