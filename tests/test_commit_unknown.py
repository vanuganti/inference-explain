import pytest

from explain.transaction import CommitUnknown, RetryBlocked


def test_ack_lost_after_commit_enters_commit_unknown(stack):
    with pytest.raises(CommitUnknown):
        stack.pay_once()
    # the side effect really happened, in the service's own database
    assert stack.pay.get_status(idempotency_key=stack.KEY)["status"] == "COMMITTED"
    # ...but the runtime cannot know, and says so
    t = stack.tm.txn(stack.TXN)
    assert (t["state"], t["retry_state"]) == ("COMMIT_UNKNOWN", "BLOCKED_PENDING_COMMIT_CHECK")
    assert stack.tm.side_effect(stack.TXN, stack.CALL)["status"] == "COMMIT_UNKNOWN"


def test_commit_happens_before_ack_loss_in_the_journal(stack):
    with pytest.raises(CommitUnknown):
        stack.pay_once()
    ts = stack.types()
    assert ts.index("DURABLE_INTENT") < ts.index("SIDE_EFFECT_COMMITTED") < ts.index("ACKNOWLEDGEMENT_LOST") \
        < ts.index("TRANSACTION_COMMIT_UNKNOWN")


def test_blind_retry_is_rejected_and_creates_nothing(stack):
    with pytest.raises(CommitUnknown):
        stack.pay_once()
    calls = []
    with pytest.raises(RetryBlocked):
        stack.tm.execute_side_effect(stack.TXN, stack.CALL, "payment.charge", stack.KEY,
                                     lambda: calls.append(1) or stack.charge(), "payment")
    assert calls == []  # the external call was never attempted
    assert stack.pay.count() == 1
    assert "RETRY_REQUESTED" in stack.types() and "RETRY_BLOCKED" in stack.types()


def test_cannot_commit_while_unresolved(stack):
    with pytest.raises(CommitUnknown):
        stack.pay_once()
    with pytest.raises(RetryBlocked):
        stack.tm.commit(stack.TXN)


def test_block_survives_restart(stack):
    with pytest.raises(CommitUnknown):
        stack.pay_once()
    stack.restart()
    with pytest.raises(RetryBlocked):
        stack.pay_once()
