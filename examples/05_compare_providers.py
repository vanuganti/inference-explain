"""Run the SAME request against every configured provider. Observability, not a benchmark."""
import _common  # noqa: F401
import sys

from explain.config import configured_providers
from explain.providers import get_provider
from explain.render import compare, explain_analyze, pname, step
from explain.schema import InferenceRequest, ProviderError
from explain.style import PROVIDER_COLOR, paint

PROMPT = ("A bat and a ball cost $1.10 in total; the bat costs $1.00 more than the ball. "
          "A train leaves at 3:40pm and arrives 2h 35m later. "
          "Answer both questions with short working.")
SYSTEM = "Reply in plain text for a terminal: no Markdown, no LaTeX, no bold."


def main() -> int:
    names = configured_providers()
    if len(names) < 2:
        print(f"need both OPENAI_* and GEMINI_* (API_KEY + MODEL) set; found: {names or 'none'}",
              file=sys.stderr)
        return 2
    req = InferenceRequest(prompt=PROMPT, system=SYSTEM)
    print(paint("EXPLAIN COMPARE  same request, live calls", "bold", "cyan"), "\n")
    results = []
    for i, name in enumerate(names, 1):
        p = get_provider(name)
        print(step(i, f"call {pname(name)}/{p.model}", "streaming..."), flush=True)
        try:
            final = [e.result for e in p.stream(req) if e.kind == "final"][0]
        except ProviderError as e:
            print(f"{name}: failed after {e.attempts} attempt(s): {e}", file=sys.stderr)
            return 1
        print(paint(f"    done in {final.latency_s:.2f}s", PROVIDER_COLOR.get(name, "cyan")))
        results.append(final)
    print()
    print(compare(results), "\n")
    for r in results:
        print(explain_analyze(r), "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
