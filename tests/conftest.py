import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from explain.agent import Runtime  # noqa: E402
from explain.journal import Journal  # noqa: E402
from explain.payments import ACK_TIMEOUT, PaymentService  # noqa: E402
from explain.transaction import TransactionManager  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_pricing(monkeypatch, tmp_path):
    """Tests never touch data/cost.json or the network."""
    monkeypatch.setenv("EXPLAIN_COST_FILE", str(tmp_path / "cost.json"))
    monkeypatch.setenv("EXPLAIN_PRICE_AUTOFETCH", "0")


class Stack:
    """A runtime + transaction manager + payment service over real SQLite files.
    restart() drops every Python object and rebuilds from the files (a process restart)."""
    TXN, CALL, KEY = "txn-1", "payment-call-01", "pay_task-1_01"

    def __init__(self, tmp_path, inject=ACK_TIMEOUT, task_id="task-1"):
        self.jpath, self.ppath, self.task_id = str(tmp_path / "j.db"), str(tmp_path / "p.db"), task_id
        self.open(inject)

    def open(self, inject=None):
        self.j = Journal(self.jpath)
        self.rt = Runtime(self.j, self.task_id, "goal", 2.0)
        self.tm = TransactionManager(self.rt)
        self.pay = PaymentService(self.ppath, observer=self.tm.observe_commit, inject=inject)
        self.tm.handlers["payment.refund"] = lambda idempotency_key: self.pay.compensate(idempotency_key=idempotency_key)

    def restart(self, inject=None):
        self.pay.close(); self.j.close()
        self.open(inject)

    def charge(self):
        return self.pay.charge(42.0, "USD", self.KEY, self.task_id, self.TXN)

    def pay_once(self):
        return self.tm.execute_side_effect(self.TXN, self.CALL, "payment.charge", self.KEY, self.charge, "payment")

    def types(self):
        return [e["type"] for e in self.j.events(self.task_id)]


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    s.tm.begin(s.TXN)
    return s
