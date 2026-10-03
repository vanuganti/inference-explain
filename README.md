# EXPLAIN for AI Agents

Databases gave us `EXPLAIN` and `EXPLAIN ANALYZE`.

LLM runtimes need **EXPLAIN INFERENCE**.

Agent runtimes need **EXPLAIN AGENT TASK**.

This repository is a live reference implementation that projects real LLM, tool, memory,
state and transaction execution into SQL-like EXPLAIN views. It makes real calls to
**OpenAI**, **Gemini** and **Anthropic**, and every box below is captured output from a real run.

```
EXPLAIN INFERENCE
    Why did the runtime choose this execution plan?

EXPLAIN ANALYZE
    What actually happened?

EXPLAIN AGENT TASK
    How did the complete task execute across models, memory, tools, state,
    transactions, retries, checkpoints, authority, cost and time?
```

## Database Kernels for AI

This project accompanies a two-part systems series:

Part 1 — LLM Inference Is Just an In-Memory Database
Mapping modern AI serving to database kernels.
https://anuganti.com/articles/llm-inference-is-inmemory-db/

Part 2 — Your AI Agent Needs a Transaction Manager
What agentic AI can learn from 40 years of database systems.
https://anuganti.com/articles/llm-inference-is-inmemory-db/

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in keys and models; .env is git-ignored
EXPLAIN_PROVIDER=openai python examples/01_inference.py
python examples/04_transaction_recovery.py
```

Configuration comes from `.env` **or** ordinary shell variables (the shell wins). Keys are only
ever read from the environment. Models are never hard-coded: set the one you want to observe.

| Variable | Meaning |
|---|---|
| `EXPLAIN_PROVIDER` | `openai`, `gemini` or `anthropic` |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | required for OpenAI |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | required for Gemini |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | required for Anthropic |
| `REASONING_EFFORT` | one setting for every provider: `minimal`/`low`/`medium`/`high`. OpenAI `reasoning.effort`, Gemini `thinking_level`, Anthropic `output_config.effort`. Blank = provider default |
| `EXPLAIN_AGENT_DB` | task/transaction journal, default `.explain-agent/runtime.db` |
| `EXPLAIN_PAYMENTS_DB`, `EXPLAIN_MEMORY_DB` | the local payment service and agent memory: separate SQLite files on purpose |
| `EXPLAIN_AGENT_BUDGET_USD` | per-task spend cap, default `2.00` |
| `*_PRICE_INPUT_PER_M`, `*_PRICE_OUTPUT_PER_M`, `*_PRICE_CACHED_PER_M` | your prices, USD per 1M tokens (see Cost) |

## The hero demo: an ambiguous commit

> A timeout does not mean an external action failed.
> The action may have committed while only the acknowledgement was lost.

```
payment committed
      ↓
acknowledgement lost
      ↓
COMMIT UNKNOWN
      ↓
retry blocked
      ↓
status reconciled
      ↓
original commit recovered
```

The runtime does not blindly retry an ambiguous side effect. It records durable intent, uses a
stable idempotency key, blocks retry while commit status is unknown, reconciles against the
external system, and resumes from durable execution state.

```bash
python examples/04_transaction_recovery.py
```

What really happens: a live model writes the plan; a local `PaymentService` (its **own SQLite file**,
`UNIQUE` idempotency key) commits the charge; the acknowledgement is dropped **after** that commit
by a deterministic failure injector; the runtime moves to `COMMIT_UNKNOWN` and **refuses** the
retry in code (not in the formatter); a fresh runtime rebuilt from SQLite alone reconciles with the
payment service and finds the original payment; re-issuing the same key creates nothing.

```
EXPLAIN AGENT TASK · transaction recovery  task-7422
goal: Complete approved purchase   failure injection: payment-ack-timeout
the acknowledgement is dropped AFTER the payment service commits

attempt-01  runtime started

[1] plan        OpenAI/gpt-5.2-2025-12-11   2.80s   in 65 out 126
[2] memory      preferred vendor → HIT   (2/2 candidates, top bm25 0.8682)
[3] policy      purchase policy → PASSED   (vendor=Acme Cloud amount=42.00 limit=100.00)
[4] transaction txn-2968 START   compensation registered (not executed)
[5] checkpoint  cp-05 persisted (before the side effect)
[6] payment     charge $42.00 USD
                idempotency key pay_task-7422_01
                side effect: COMMITTED   payment-0007  (seen by the payment service, not by the runtime)
                acknowledgement: LOST

⚠ COMMIT STATUS UNKNOWN

[7] retry       requested

✖ RETRY BLOCKED
  pending commit reconciliation

payment records for this task: 1

── process restart: fresh Journal, Runtime and PaymentService built from SQLite only ──

[8] recovery    attempt-02   loaded checkpoint cp-05
                checking payment status...

✔ EXISTING COMMIT FOUND
  payment payment-0007
  status COMMITTED

✔ TRANSACTION RECOVERED
  COMMIT_UNKNOWN → COMMITTED
  duplicate charge prevented (records 1 → 1)

┌─ IDEMPOTENCY CHECK · same key, re-issued after recovery ───┐
│ Key               pay_task-7422_01                         │
│ First payment     payment-0007                             │
│ Re-issued request same key                                 │
│ Returned          payment-0007                             │
│ Records           1 → 1                                    │
│ New side effect   NO                                       │
│ Duplicate         PREVENTED                                │
└────────────────────────────────────────────────────────────┘

Compensation    not required   (payment was found COMMITTED)
```

Then the projection of the journal. Every field below is read back from recorded events:

```
┌─ EXPLAIN AGENT TASK · task-7422 ─────────────────────────────────────────────────────────────────────────────────────┐
│ Goal            Complete approved purchase                                                                           │
│ Status          ✔ recovered                                                                                          │
├─ TRANSACTION ────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Transaction     txn-2968                                                                                             │
│ Payment         COMMIT_UNKNOWN → COMMITTED                                                                           │
│ Retry           BLOCKED pending commit check                                                                         │
│ Idempotency     pay_task-7422_01                                                                                     │
│ Checkpoint      cp-05                                                                                                │
│ Compensation    not required                                                                                         │
│ Duplicate       PREVENTED                                                                                            │
│ Policy          PASSED                                                                                               │
│ Attempts        2  (attempt-01, attempt-02)                                                                          │
├─ STEPS ──────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│   #  STEP       WHAT                                       IN  REASONING    OUT    TTFT  LATENCY  TIMELINE           │
│   1  plan       OpenAI/gpt-5.2-2025-12-11                  65         20    126   1.10s    2.80s  ██████████████     │
│   2  memory     memory: 'preferred vendor' → HIT  (2/2, top bm25 0.868)                                              │
│   3  policy     policy: purchase policy → PASSED                                                                     │
│   4  payment    tool: payment.charge  [no_acknowledgement]                                     0.01s  █░░░░░░░░░░░░░ │
├─ MODELS ─────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ OpenAI/gpt-5.2-2025-12-11   1 call                                                                                   │
├─ TOKENS  (as reported by each provider; not directly comparable) ────────────────────────────────────────────────────┤
│ OpenAI    in       65   reasoning          20   out      126                                                         │
├─ ESTIMATED COST  (calculated from a local price table, not a provider invoice) ──────────────────────────────────────┤
│ OpenAI    $0.001878                                                                                                  │
│ Total     $0.001878   of $2.00 budget                                                                                │
│           █░░░░░░░░░░░░░ 0.1% used                                                                                   │
├─ RUNTIME ────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Elapsed           3.37s                                                                                              │
│ Inference calls   1                                                                                                  │
│ Tool calls        1                                                                                                  │
│ Memory lookups    1                                                                                                  │
│ Journal events    24                                                                                                 │
│ State writes      0                                                                                                  │
│ Checkpoints       1                                                                                                  │
│ Recovered steps   0                                                                                                  │
│ Retries / errors  0 / 0                                                                                              │
│                                                                                                                      │
│ GPU Placement     UNAVAILABLE (hosted provider)                                                                      │
│ KV Occupancy      UNAVAILABLE (hosted provider)                                                                      │
│ Graph Breaks      UNAVAILABLE (hosted provider)                                                                      │
│ Kernel Fusion     UNAVAILABLE (hosted provider)                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

```
┌─ TRACE · task-7422 ────────────────────────────────────────┐
│ PLAN                                                       │
│   ↓                                                        │
│ MEMORY                                                     │
│   ↓                                                        │
│ POLICY                                                     │
│   ↓                                                        │
│ CHECKPOINT cp-05                                           │
│   ↓                                                        │
│ PAYMENT                                                    │
│   ├─ durable intent recorded                               │
│   ├─ side effect COMMITTED                                 │
│   └─ acknowledgement LOST                                  │
│   ↓                                                        │
│ COMMIT UNKNOWN                                             │
│   ↓                                                        │
│ RETRY REQUESTED                                            │
│   ↓                                                        │
│ RETRY BLOCKED                                              │
│   ↓                                                        │
│ RECOVERY from cp-05  [attempt-02]                          │
│   ↓                                                        │
│ STATUS RECONCILE                                           │
│   ↓                                                        │
│ COMMITTED                                                  │
│   ↓                                                        │
│ IDEMPOTENCY CHECK                                          │
│   ├─ same key re-issued → payment-0007                     │
│   └─ duplicate PREVENTED                                   │
└────────────────────────────────────────────────────────────┘
```

Other scenarios: `--phase fail` ends the process after the failure and `--recover <task-id>` finishes it
from a **new process**; `--compensate` adds a later policy rejection that triggers a recorded,
at-most-once compensation; `--fail none` runs the happy path.

## Architecture

```
REAL EXECUTION
      ↓
APPEND-ONLY EVENTS
      ↓
DURABLE TASK / TRANSACTION JOURNAL
      ↓
EXPLAIN PROJECTION
      ↓
HUMAN-READABLE PLAN / RUNTIME / TRAJECTORY
```

EXPLAIN is not a logging formatter. The boxes are projections over recorded execution state:
`project_task()` turns journal events into a plain dict, and the terminal view only renders that dict.
The event vocabulary is closed (`explain/events.py`); the journal rejects unknown event types. Tests
rebuild the view from a fresh SQLite connection to prove nothing comes from Python memory.

```
               EXPLAIN AGENT TASK
                       │
     ┌──────────┬──────┴─────┬───────────────┐
  Memory      Tools      Inference      Transactions
 (FTS5)     (events)        │            (journal +
                      ┌─────┼──────┐     payment svc)
                   OpenAI Gemini Anthropic
```

Agent **memory** (semantic lookups, `memory.db`) is not the **journal** (execution events, `runtime.db`).
Execution persistence is reported as *journal events* and *state writes*; "memory" is only ever a real memory lookup.

## Examples

### 1. EXPLAIN INFERENCE: `examples/01_inference.py`

```bash
EXPLAIN_PROVIDER=gemini python examples/01_inference.py     # or openai, anthropic
```

```
┌─ EXPLAIN ANALYZE ──────────────────────────────────────────────────┐
│ Input Tokens      74  OBSERVED                                     │
│ Output Tokens     162  OBSERVED                                    │
│ Cached Tokens     UNAVAILABLE                                      │
│ Reasoning Tokens  572  OBSERVED                                    │
│ Tool Tokens       UNAVAILABLE                                      │
│ Total Tokens      808  OBSERVED                                    │
│ TTFT              2.54s  OBSERVED                                  │
│ Total Latency     2.87s  OBSERVED                                  │
│ Retries           0                                                │
│ Estimated Cost    $0.002808  DERIVED                               │
└────────────────────────────────────────────────────────────────────┘
```

Every value is tagged by how it is known: `OBSERVED` (reported by the provider or measured by the client),
`DERIVED` (calculated from observed values) or `ESTIMATED`. GPU placement, KV occupancy, graph breaks and
kernel fusion stay `UNAVAILABLE` for hosted providers; they are never invented.

### 2. EXPLAIN MEMORY: `examples/02_memory.py`

Real persistent memory in SQLite FTS5. Retrieval is lexical (bm25), so there are no embedding similarity
scores to show, and none are shown. No model is called.

```
┌─ EXPLAIN MEMORY ──────────────────────────────────────────────────────────────────────────────────────┐
│ Query             preferred vendor                                                                    │
│ Retrieval         FTS5 bm25 (porter stemming; no embeddings)                                          │
│ Candidates        3                                                                                   │
│ Returned          3                                                                                   │
│ Top Score         2.1442  (bm25, higher is better)                                                    │
│ Source            system, user                                                                        │
│ PII Filter        PASSED                                                                              │
│ Tokens Injected   ~57  ESTIMATED (chars/4)                                                            │
│                                                                                                       │
│   #1  2.1442      Preferred vendor for cloud licences is Acme Cloud; renewals go through procurement. │
│                                                                                                       │
│   #2  0.5394      Approved vendors: Acme Cloud, Northwind Software, Globex Compute.                   │
│                                                                                                       │
│   #8  0.4555      Payment terms with approved vendors are net 30 unless the contract says otherwise.  │
└───────────────────────────────────────────────────────────────────────────────────────────────────────┘ 
```

### 3. EXPLAIN AGENT TASK, a normal live task: `examples/03_agent_task.py`

Plan, fetch live Hacker News, research, summarize, on any provider (or mixed with
`--multi-provider` / `--roles planner=gemini,research=openai,summarizer=anthropic`). It then **automatically
re-runs the same task id** to show recovery; completed steps are restored from the journal, not re-executed.

```
RUN 1  live  plan → fetch live Hacker News → research → summarize
[1] plan      Gemini/gemini-3.8-flash  2.92s  ttft 2.78s
[2] fetch     tool: hn_top  2.01s
[3] research  Gemini/gemini-3.8-flash  6.10s  ttft 5.02s
[4] summarize Gemini/gemini-3.8-flash  4.72s  ttft 4.22s

┌─ RECOVERY CHECK · resume with same task id ────────────────┐
│ New model calls       0  ✔ yes                             │
│ New tool calls        0  ✔ yes                             │
│ Spend unchanged       $0.010402 → $0.010402  ✔ yes         │
│ Result identical      ✔ yes                                │
└────────────────────────────────────────────────────────────┘
```

### 4. Transaction failure and recovery: `examples/04_transaction_recovery.py`

The hero demo above.

### 5. Multi-provider observability: `examples/05_compare_providers.py`

The same request against every configured provider. An advanced example showing the EXPLAIN layer is
provider-neutral, not a benchmark.

```
┌─ EXPLAIN COMPARE · same request, each provider ──────────────────────────────────────────────────┐
│                   OPENAI                    GEMINI                    ANTHROPIC                  │
├─ OBSERVED ───────────────────────────────────────────────────────────────────────────────────────┤
│ Model             gpt-5.2-2025-12-11        gemini-3.8-flash          claude-sonnet-5            │
│ Input Tokens      80                        74                        104                        │
│ Output Tokens     158                       162                       201                        │
│ Reasoning Tokens  45                        638                       UNAVAILABLE                │
│ Cached Tokens     0                         UNAVAILABLE               0                          │
│ Total Tokens      238                       874                       305                        │
│ TTFT              2.42s                     2.56s                     1.26s                      │
│ Latency           4.47s                     2.91s                     2.14s                      │
│ Estimated Cost    $0.002352                 $0.003056                 $0.002218                  │
├─ READ THIS FIRST ────────────────────────────────────────────────────────────────────────────────┤
│ Observability comparison, not a benchmark: no ranking, no winner.                                │
│ Providers tokenize and execute differently, so token counts are not                              │
│ equivalent work. Output: Gemini excludes thinking tokens, OpenAI includes                        │
│ them, Anthropic includes them and reports no separate reasoning count.                           │
│ Input includes cached tokens for all three. Cost is estimated from a local                       │
│ price table, not a provider invoice.                                                             │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘ 
```

**Observability comparison, not a benchmark. No ranking, no winner.** Providers tokenize and execute
differently, so token counts are not equivalent units of work. Output: Gemini excludes thinking tokens,
OpenAI includes them, Anthropic includes them and reports no separate reasoning count (so it shows
`UNAVAILABLE`). Input includes cached tokens for all three.

## Inspecting a task

```bash
python -m explain list
python -m explain explain <task-id>     # EXPLAIN AGENT TASK
python -m explain trace   <task-id>     # trajectory
python -m explain events  <task-id>     # event journal as a table (add --json for developers)
```

## Cost

Cost is **estimated**: calculated from a local price table, not a provider invoice. Lookup order:
`*_PRICE_*` env override, then `data/cost.json`, then auto-fetch. No provider API exposes prices, so
auto-fetch reads the community-maintained LiteLLM price table and saves the entry with its source and
timestamp (check the provider's pricing page before quoting). Disable with `EXPLAIN_PRICE_AUTOFETCH=0`.
Unknown pricing renders `UNAVAILABLE`, never zero. Cache-write tokens are billed at the input rate here;
tiered or long-context pricing is not modelled.

## Safety

Before anything is persisted or printed, API keys, bearer tokens, `Authorization`/cookie values and the
values of any configured `*KEY/*TOKEN/*SECRET` environment variable are redacted. Tool arguments are stored
as summaries (long strings shortened, collections reduced to their size), and the environment is never
stored. A test writes a fake key through every persistence path and scans the raw SQLite bytes.

## Limitations

- This is a **reference implementation**, not a production transaction manager.
- Arbitrary external tools do **not** participate in ACID transactions.
- **Compensation is not rollback**: it is a new, recorded action that reverses an effect. Handlers must be idempotent.
- Hosted model APIs do not expose all physical inference internals. GPU placement, KV occupancy, graph breaks
  and kernel fusion therefore remain `UNAVAILABLE` where the provider does not expose them.
- Cost is calculated from a local pricing table; treat it as estimated, not provider-billed.
- Provider token accounting differs; see the comparison note.
- The payment service is local and demonstrates external side-effect semantics. It does not process real money.
- `SIDE_EFFECT_COMMITTED` is reported out-of-band by the payment service's observer after its own commit.
  The runtime never uses it for decisions; its state follows only what its acknowledgement says.
- Refusal fallbacks for Anthropic models are not enabled in this demo; a refusal surfaces as the finish reason.

## Layout

```
explain/
  schema.py  events.py  redact.py  config.py  pricing.py
  providers/{base,openai,gemini,anthropic}.py    one InferenceProvider interface
  journal.py        append-only events + durable tables (SQLite)
  agent.py          provider-independent runtime: durable steps, tools, memory lookups, policy
  transaction.py    durable intent, COMMIT_UNKNOWN, retry block, reconciliation, compensation
  payments.py       local external payment service (separate SQLite, UNIQUE idempotency key)
  memory.py         agent memory (FTS5), separate from the journal
  explain_task.py   EXPLAIN AGENT TASK projection      trace.py   trajectory projection
  render.py         EXPLAIN INFERENCE / ANALYZE / MEMORY / COMPARE    cli.py   explain|trace|events
examples/  01_inference  02_memory  03_agent_task  04_transaction_recovery  05_compare_providers
tests/     behavior tests (no keys needed): idempotency, commit-unknown, reconciliation,
           checkpoint recovery, compensation, journal projection, capabilities, redaction
```

`python -m pytest -q` needs no API keys. The live behaviour is exercised by the examples.
