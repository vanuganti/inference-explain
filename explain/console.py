"""Shared terminal output for examples: redacted tee to a plain-text report."""
from __future__ import annotations

import os
import re

from .redact import redact_text
from .render import pname
from .style import PROVIDER_COLOR, Panel, paint

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Out:
    """Print to the terminal and keep a plain-text, secret-redacted copy for the saved report."""

    def __init__(self):
        self.buf: list[str] = []

    def __call__(self, text: str = "") -> None:
        text = redact_text(text)
        print(text, flush=True)
        self.buf.append(ANSI.sub("", text))

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(self.buf) + "\n")


def progress_line(out: Out):
    n = [0]

    def cb(kind: str, step: str, info: dict) -> None:
        n[0] += 1
        head = paint(f"[{n[0]}]", "bold", "cyan") + f" {step:<10}"
        if kind == "inference":
            col = PROVIDER_COLOR.get(info["provider"], "cyan")
            out(head + paint(f"{pname(info['provider'])}/{info['model']}", col)
                + paint(f"  {info['latency_s']:.2f}s  ttft {info['ttft_s']:.2f}s", "dim"))
        elif kind == "tool":
            out(head + paint(f"tool: {info['tool']}", "yellow") + paint(f"  {info['latency_s']:.2f}s", "dim"))
        elif kind == "recovered":
            out(head + paint("recovered from journal (no re-execution)", "magenta"))
    return cb
