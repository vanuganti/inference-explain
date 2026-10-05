"""Run every example live and write their real console output to docs/EXAMPLES.md.

    pip install rich                              # only this script needs it (renders ANSI to SVG)
    python scripts/capture_examples.py            # all examples (live calls, costs a few cents)
    python scripts/capture_examples.py 06 08      # only these

GitHub cannot show terminal colours, so each run is kept twice: a colour SVG of the terminal in docs/img/
(embedded in the page) and the plain text in a collapsed block, for copying. The home directory is masked.
A failing example is recorded with its exit code, never hidden.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from explain.config import configured_providers  # noqa: E402

EXAMPLES = [
    ("01", "EXPLAIN INFERENCE", "01_inference.py", "per-provider"),
    ("02", "EXPLAIN MEMORY", "02_memory.py", []),
    ("03", "EXPLAIN AGENT TASK, a normal live task", "03_agent_task.py", []),
    ("04", "Transaction failure and recovery", "04_transaction_recovery.py", []),
    ("05", "Multi-provider observability", "05_compare_providers.py", []),
    ("06", "The plan, then the actuals", "06_explain_plan.py", []),
    ("07", "Prompt layout and cache reuse", "07_prompt_layout_cache.py", []),
    ("08", "Agent task trajectory", "08_agent_task_trajectory.py", []),
]
ANSI = re.compile(r"\x1b\[[0-9;]*m")


ID = re.compile(r"\b(resp_|msg_)[0-9A-Za-z]{12,}|(?<=Request {13})[0-9A-Za-z_-]{15,}")


def mask_ids(text: str) -> str:
    """Provider request/response ids are account-linked, not secrets; mask them, keeping the length so boxes stay aligned."""
    return ID.sub(lambda m: (m.group(1) or "") + "x" * (len(m.group(0)) - len(m.group(1) or "")), text)


def to_svg(ansi_text: str, title: str) -> str:
    import io
    from rich.console import Console
    from rich.text import Text
    width = max((len(ANSI.sub("", ln)) for ln in ansi_text.splitlines()), default=80) + 1
    c = Console(record=True, width=width, file=io.StringIO(), force_terminal=True, color_system="truecolor")
    c.print(Text.from_ansi(ansi_text), soft_wrap=False)
    return c.export_svg(title=title)


def run(script: str, args: list[str], extra_env: dict[str, str]) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
    env.update({"FORCE_COLOR": "1", **extra_env})
    p = subprocess.run([sys.executable, str(ROOT / "examples" / script), *args], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=900)
    out = p.stdout + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
    return p.returncode, mask_ids(out.replace(str(Path.home()), "~")).rstrip() + "\n"


def main() -> int:
    only = set(sys.argv[1:])
    header = ["# Example output\n",
              "Real output from live runs of every example, captured by `scripts/capture_examples.py`. "
              "Nothing is edited except masking the home directory. Each run is a screenshot of the coloured "
              "terminal output (SVG) with the plain text underneath it, for copying. "
              "Models, token counts, latencies and costs vary run to run.\n",
              "Part of the [README](../README.md). To regenerate: `python scripts/capture_examples.py` "
              "(`python scripts/capture_examples.py 06 08` re-captures only those).\n"]
    target = ROOT / "docs" / "EXAMPLES.md"
    old: dict[str, str] = {}  # a partial run keeps the other sections as they are
    if only and target.exists():
        for chunk in re.split(r"\n(?=## \d+\. )", target.read_text())[1:]:
            old[f"{int(chunk[3:chunk.index('.')]):02d}"] = "\n" + chunk.rstrip("\n") + "\n"
    status = 0
    sections: list[str] = []
    for num, title, script, mode in EXAMPLES:
        if only and num not in only:
            if num in old:
                sections.append(old[num])
            continue
        runs = [(f" ({p})", [], {"EXPLAIN_PROVIDER": p}) for p in configured_providers()] if mode == "per-provider" \
            else [("", mode, {})]
        parts = [f"\n## {int(num)}. {title}\n\n`python examples/{script}`\n"]
        for label, args, env in runs:
            print(f"running {script}{label} ...", flush=True)
            code, out = run(script, args, env)
            status |= code != 0
            if label:
                parts.append(f"\n**{label.strip(' ()')}**\n")
            if code != 0:
                parts.append(f"\n> exit code {code}\n")
            name = f"{num}{'-' + label.strip(' ()') if label else ''}.svg"
            (ROOT / "docs" / "img").mkdir(parents=True, exist_ok=True)
            (ROOT / "docs" / "img" / name).write_text(to_svg(out, f"python examples/{script}"))
            parts.append(f"\n![{script}{label}](img/{name})\n")
            parts.append(f"\n<details><summary>plain text</summary>\n\n```text\n{ANSI.sub('', out)}```\n\n</details>\n")
        sections.append("".join(parts))
    target.write_text("\n".join(header) + "".join(sections))
    print(f"wrote {target}")
    return status


if __name__ == "__main__":
    sys.exit(main())
