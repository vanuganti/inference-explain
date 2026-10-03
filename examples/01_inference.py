"""Live inference through the configured provider, then EXPLAIN INFERENCE/ANALYZE.

    EXPLAIN_PROVIDER=openai python examples/01_inference.py
    EXPLAIN_PROVIDER=gemini python examples/01_inference.py
"""
import _common  # noqa: F401
import sys

from explain.config import ConfigError, Settings
from explain.providers import get_provider
from explain.render import explain_analyze, explain_inference, pname, step
from explain.schema import InferenceRequest
from explain.style import PROVIDER_COLOR, Panel, paint

PROMPT = ("A bat and a ball cost $1.10 in total; the bat costs $1.00 more than the ball. "
          "A train leaves at 3:40pm and arrives 2h 35m later. "
          "Answer both questions with short working.")
SYSTEM = "Reply in plain text for a terminal: no Markdown, no LaTeX, no bold."


def main() -> int:
    try:
        provider = get_provider(Settings.load().provider)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    req = InferenceRequest(prompt=PROMPT, system=SYSTEM)
    col = PROVIDER_COLOR.get(provider.name, "cyan")
    print(paint(f"EXPLAIN INFERENCE  live call to {pname(provider.name)}/{provider.model}", "bold", col), "\n")

    print(step(1, "count_tokens", f"pre-flight input tokens: {provider.count_tokens(req) or 'UNAVAILABLE'}"))
    print(step(2, "stream", "response below"))
    print(paint("  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄", "dim"))
    final = None
    for ev in provider.stream(req):
        if ev.kind == "delta":
            print(ev.text, end="", flush=True)
        else:
            final = ev.result
    print(paint("\n  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄", "dim"))
    ttft = f"first token {final.ttft_s:.2f}s, " if final.ttft_s is not None else ""
    print(step(3, "normalize", f"{ttft}total {final.latency_s:.2f}s, {final.retries} retries"), "\n")

    print(explain_inference(final, provider.capabilities()), "\n")
    print(explain_analyze(final, provider.capabilities()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
