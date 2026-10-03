import os

from explain.agent import Runtime
from explain.journal import Journal
from explain.redact import redact, redact_text, summarize_args
from explain.explain_task import explain_agent_task

FAKE = "sk-test-0123456789abcdefghijkl"


def test_patterns_and_keys_are_redacted():
    s = f"Authorization: Bearer abcdefghijklmnop1234 key={FAKE} AIzaSyA1234567890abcdefghijk"
    out = redact_text(s)
    assert FAKE not in out and "abcdefghijklmnop1234" not in out and "AIzaSyA1234567890" not in out
    assert redact({"api_key": "x", "cookie": "y", "n": {"authorization": "z"}}) == \
        {"api_key": "[REDACTED]", "cookie": "[REDACTED]", "n": {"authorization": "[REDACTED]"}}


def test_configured_env_secrets_are_redacted_by_value(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_SECRET", "plain-looking-secret-value")
    assert "plain-looking-secret-value" not in redact_text("x plain-looking-secret-value y")


def test_secrets_never_reach_sqlite_or_rich_output(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE)
    path = str(tmp_path / "j.db")
    j = Journal(path)
    rt = Runtime(j, "t", f"goal using {FAKE}", 2.0, tools={"t": lambda **k: f"result {FAKE}"})
    rt.tool("fetch", "t", url="https://x", headers={"Authorization": f"Bearer {FAKE}"}, note=FAKE)
    rt.save_state("k", {"leak": FAKE})
    rt.checkpoint("cp", token=FAKE, other=f"value {FAKE}")
    rt.finish()
    rendered = explain_agent_task(j, "t")
    j.close()
    raw = b"".join(open(os.path.join(tmp_path, f), "rb").read() for f in os.listdir(tmp_path))
    assert FAKE.encode() not in raw and FAKE not in rendered


def test_tool_args_are_summarized_not_dumped():
    out = summarize_args({"body": "x" * 5000, "ids": list(range(100)), "n": 3})
    assert len(out["body"]) < 200 and out["ids"] == "<list len=100>" and out["n"] == 3


def test_task_and_idempotency_ids_are_not_mistaken_for_keys():
    ids = "pay_task-20261002-195449_01 task-20261002-195449-extra-long-suffix txn-3072"
    assert redact_text(ids) == ids
    assert "[REDACTED" in redact_text("key sk-abcdefghijklmnopqrstuv1234")
