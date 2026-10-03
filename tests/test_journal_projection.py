import pytest

from explain.explain_task import explain_agent_task, project_task
from explain.trace import trace_nodes
from explain.transaction import CommitUnknown, RetryBlocked


def run_scenario(stack):
    stack.tm.register_compensation(stack.TXN, "payment.refund", {"idempotency_key": stack.KEY})
    stack.rt.checkpoint("cp-05", txn_id=stack.TXN)
    with pytest.raises(CommitUnknown):
        stack.pay_once()
    with pytest.raises(RetryBlocked):
        stack.pay_once()
    stack.restart()
    stack.tm.recover({"payment.charge": lambda k: stack.pay.get_status(idempotency_key=k)})
    stack.tm.assert_idempotent(stack.TXN, stack.CALL, stack.charge, stack.pay.count)
    stack.rt.finish("recovered")


def test_explain_is_reconstructed_from_events_alone(stack):
    run_scenario(stack)
    stack.restart()  # read through a brand-new connection: only SQLite remains
    p = project_task(stack.j, stack.task_id)
    t = p["txn"]
    assert t["state_path"] == ["COMMIT_UNKNOWN", "COMMITTED"]
    assert t["retry"].startswith("BLOCKED")
    assert t["idempotency_key"] == stack.KEY and t["checkpoints"] == ["cp-05"]
    assert t["compensation"] == "not required" and t["duplicate"] == "PREVENTED"
    assert t["attempts"] == ["attempt-01", "attempt-02", "attempt-03"][:len(t["attempts"])]
    assert p["status"] == "recovered"
    text = explain_agent_task(stack.j, stack.task_id)
    for needle in ("COMMIT_UNKNOWN → COMMITTED", "BLOCKED pending commit check", stack.KEY, "cp-05",
                   "not required", "PREVENTED", "Journal events"):
        assert needle in text


def test_projection_changes_when_events_change(stack):
    """Not hard-coded: a run without the failure projects a different transaction."""
    stack.pay.inject = None
    stack.pay_once()
    stack.tm.commit(stack.TXN)
    stack.rt.finish("completed")
    t = project_task(stack.j, stack.task_id)["txn"]
    assert t["state_path"] == ["COMMITTED"] and t["retry"] == "not requested" and t["duplicate"] == "not tested"


def test_trace_nodes_follow_recorded_events(stack):
    run_scenario(stack)
    labels = [n["label"] for n in trace_nodes(stack.j.events(stack.task_id))]
    order = ["PAYMENT", "COMMIT UNKNOWN", "RETRY REQUESTED", "RETRY BLOCKED", "STATUS RECONCILE", "COMMITTED"]
    pos = [labels.index(x) for x in order]
    assert pos == sorted(pos)
    pay = next(n for n in trace_nodes(stack.j.events(stack.task_id)) if n["label"] == "PAYMENT")
    assert pay["children"] == ["durable intent recorded", "side effect COMMITTED", "acknowledgement LOST"]


def test_unknown_event_types_are_rejected(stack):
    with pytest.raises(ValueError):
        stack.j.emit(stack.task_id, "MADE_UP_EVENT")


def test_counters_agree_with_the_trace(stack):
    """The summary must not contradict the story: a blocked retry and a recovery are both counted."""
    run_scenario(stack)
    c = project_task(stack.j, stack.task_id)["counts"]
    assert c["retries_blocked"] == 1 and c["retries_executed"] == 0
    assert c["recoveries"] == 1 and c["reconciliations"] == 1
    text = explain_agent_task(stack.j, stack.task_id)
    assert "0 executed · 1 blocked" in text and "Recoveries" in text
