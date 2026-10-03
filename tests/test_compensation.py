import pytest

from explain.transaction import CommitUnknown


def committed(stack):
    stack.tm.register_compensation(stack.TXN, "payment.refund", {"idempotency_key": stack.KEY})
    with pytest.raises(CommitUnknown):
        stack.pay_once()
    stack.tm.recover({"payment.charge": lambda k: stack.pay.get_status(idempotency_key=k)})


def test_compensation_is_not_run_unless_required(stack):
    committed(stack)
    assert stack.pay.get_status(idempotency_key=stack.KEY)["status"] == "COMMITTED"
    assert "TRANSACTION_COMPENSATE" not in stack.types()
    assert "COMPENSATION_REGISTERED" in stack.types()


def test_compensation_executes_when_required_and_is_distinct_from_rollback(stack):
    committed(stack)
    out = stack.tm.compensate(stack.TXN, reason="audit failed")
    assert len(out) == 1
    pay = stack.pay.get_status(idempotency_key=stack.KEY)
    assert pay["status"] == "COMPENSATED" and pay["compensated_at"] and pay["committed_at"]  # history kept
    assert stack.pay.count() == 1  # a reversal is recorded; the original row is not deleted
    assert stack.tm.txn(stack.TXN)["state"] == "COMPENSATED"
    assert stack.types().count("TRANSACTION_COMPENSATE") == 1


def test_repeated_compensation_and_recovery_execute_at_most_once(stack):
    committed(stack)
    calls = []
    real = stack.tm.handlers["payment.refund"]
    stack.tm.handlers["payment.refund"] = lambda **kw: calls.append(1) or real(**kw)
    assert len(stack.tm.compensate(stack.TXN, "r1")) == 1
    assert stack.tm.compensate(stack.TXN, "r2") == []
    stack.restart()
    stack.tm.handlers["payment.refund"] = lambda **kw: calls.append(1) or real(**kw)
    assert stack.tm.compensate(stack.TXN, "after restart") == []
    stack.tm.recover({"payment.charge": lambda k: stack.pay.get_status(idempotency_key=k)})
    assert len(calls) == 1 and stack.types().count("TRANSACTION_COMPENSATE") == 1


def test_payment_compensate_is_itself_idempotent(stack):
    committed(stack)
    a = stack.pay.compensate(idempotency_key=stack.KEY)
    b = stack.pay.compensate(idempotency_key=stack.KEY)
    assert a["status"] == b["status"] == "COMPENSATED" and b["already_compensated"] is True
