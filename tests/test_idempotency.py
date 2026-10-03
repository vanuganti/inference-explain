from explain.payments import PaymentService


def test_same_key_twice_is_one_payment(tmp_path):
    p = PaymentService(str(tmp_path / "p.db"))
    first = p.charge(42.0, "USD", "pay_k_01", "t", "txn")
    second = p.charge(42.0, "USD", "pay_k_01", "t", "txn")
    assert p.count() == 1
    assert first["payment_id"] == second["payment_id"]
    assert first["deduplicated"] is False and second["deduplicated"] is True


def test_uniqueness_is_enforced_by_sqlite_not_python(tmp_path):
    path = str(tmp_path / "p.db")
    a, b = PaymentService(path), PaymentService(path)  # two instances, no shared Python state
    ra = a.charge(42.0, "USD", "k", "t", "x")
    rb = b.charge(42.0, "USD", "k", "t", "x")
    assert ra["payment_id"] == rb["payment_id"] and a.count() == 1
    assert a.db.execute("SELECT COUNT(*) FROM pragma_index_list('payments') WHERE \"unique\"=1").fetchone()[0] >= 1


def test_different_keys_make_different_payments(tmp_path):
    p = PaymentService(str(tmp_path / "p.db"))
    assert p.charge(1, "USD", "k1")["payment_id"] != p.charge(1, "USD", "k2")["payment_id"]
    assert p.count() == 2


def test_reissue_after_recovery_records_idempotency_check(stack):
    from explain.transaction import CommitUnknown
    try:
        stack.pay_once()
    except CommitUnknown:
        pass
    stack.restart()
    stack.tm.recover({"payment.charge": lambda k: stack.pay.get_status(idempotency_key=k)})
    info = stack.tm.assert_idempotent(stack.TXN, stack.CALL, stack.charge, stack.pay.count)
    assert info["records_before"] == info["records_after"] == 1
    assert info["first_payment_id"] == info["returned_payment_id"]
    assert info["duplicate_prevented"] and not info["new_side_effect"]
    assert "IDEMPOTENCY_CHECK" in stack.types()
