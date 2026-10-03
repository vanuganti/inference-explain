from explain.agent import Runtime
from explain.journal import Journal


def test_checkpoint_persists_and_loads_in_a_fresh_context(tmp_path):
    path = str(tmp_path / "j.db")
    j = Journal(path)
    rt = Runtime(j, "t1", "g", 2.0)
    rt.checkpoint("cp-05", txn_id="txn-1", next_step="payment")
    j.close()
    fresh = Journal(path)  # new connection, no Python state carried over
    assert fresh.latest_checkpoint("t1") == ("cp-05", {"txn_id": "txn-1", "next_step": "payment"})


def test_completed_steps_are_not_executed_again_after_restart(tmp_path):
    path, calls = str(tmp_path / "j.db"), {"a": 0, "b": 0}

    def pipeline(rt):
        rt.step("a", lambda: calls.__setitem__("a", calls["a"] + 1) or "A")
        rt.checkpoint("cp-1")
        rt.step("b", lambda: calls.__setitem__("b", calls["b"] + 1) or "B")

    rt1 = Runtime(Journal(path), "t", "g", 2.0)
    rt1.step("a", lambda: calls.__setitem__("a", calls["a"] + 1) or "A")  # crash after step a
    rt1.checkpoint("cp-1")
    rt1.j.close()

    rt2 = Runtime(Journal(path), "t", "g", 2.0)  # fresh runtime object, same SQLite file
    assert rt2.attempt_id == "attempt-02"
    pipeline(rt2)
    assert calls == {"a": 1, "b": 1}  # a recovered, not re-run; b ran once
    types = [e["type"] for e in rt2.j.events("t")]
    assert types.count("STEP_RECOVERED") == 1 and "TASK_RESUME" in types


def test_recovered_result_is_the_original_value(tmp_path):
    path = str(tmp_path / "j.db")
    Runtime(Journal(path), "t", "g", 2.0).step("x", lambda: {"v": 7})
    assert Runtime(Journal(path), "t", "g", 2.0).step("x", lambda: {"v": 999}) == {"v": 7}
