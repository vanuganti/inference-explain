"""Terminal styling: ANSI colors (TTY only) and content-sized panels."""
from __future__ import annotations

import os
import re
import sys

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
         "blue": "34", "magenta": "35", "cyan": "36"}
PROVIDER_COLOR = {"openai": "green", "gemini": "blue"}


def enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(os.environ.get("FORCE_COLOR")) or sys.stdout.isatty()


def paint(text: str, *styles: str) -> str:
    styles = tuple(x for x in styles if x)  # callers pass "" for "no style"
    if not enabled() or not styles:
        return text
    return f"\x1b[{';'.join(CODES[s] for s in styles)}m{text}\x1b[0m"


def vlen(s: str) -> int:
    return len(_ANSI.sub("", s))


def pad(s: str, width: int, right: bool = False) -> str:
    gap = " " * max(width - vlen(s), 0)
    return gap + s if right else s + gap


def cell(text: str, width: int, right: bool = False, *styles: str) -> str:
    """Pad first (on plain text), then paint, so ANSI codes never skew alignment."""
    text = str(text)
    if text.startswith("UNAVAILABLE"):
        styles = ("dim", "yellow")
    return paint(pad(text, width, right), *styles)


class Panel:
    """A box that grows to fit its content; never clips a value."""

    def __init__(self, title: str, subtitle: str = ""):
        self.title, self.subtitle = title, subtitle
        self.items: list[tuple[str, str]] = []  # (kind, text)

    def row(self, text: str = "") -> "Panel":
        self.items.append(("row", text)); return self

    def kv(self, label: str, value: str, label_w: int = 18) -> "Panel":
        v = paint(value, "dim", "yellow") if value.startswith("UNAVAILABLE") else value
        return self.row(paint(f"{label:<{label_w}}", "dim") + v)

    def sep(self, title: str) -> "Panel":
        self.items.append(("sep", title)); return self

    def render(self, min_width: int = 60) -> str:
        head = " " + paint(self.title, "bold", "cyan") + (
            " " + paint("· " + self.subtitle, "dim") if self.subtitle else "") + " "
        inner = max(min_width, vlen(head) + 3,
                    *(vlen(t) + 2 for k, t in self.items if k == "row"),
                    *(vlen(t) + 5 for k, t in self.items if k == "sep"))
        b = lambda s: paint(s, "dim")  # noqa: E731 - dim borders keep content forward
        out = [b("┌─") + head + b("─" * (inner - vlen(head) - 1) + "┐")]
        for kind, text in self.items:
            if kind == "row":
                out.append(b("│") + pad(" " + text, inner) + b("│"))
            else:
                h = " " + paint(text, "bold") + " "
                out.append(b("├─") + h + b("─" * (inner - vlen(h) - 1) + "┤"))
        out.append(b("└" + "─" * inner + "┘"))
        return "\n".join(out)
