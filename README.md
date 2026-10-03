# EXPLAIN for AI Agents

> **Proof of concept, for demonstration only.** This repository is a demo built to illustrate the ideas in
> the articles below. It is **not** a real or production implementation: the payment service is a local
> stand-in that moves no money, the transaction manager is a teaching sketch, and nothing here should be
> used to run real workloads. See [Limitations](#limitations).

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

## Companion code for *Database Kernels for AI*

This code accompanies a two-part systems series. The articles make the argument; this repository lets you
watch it happen against live models.

- **[Part 1: LLM Inference Is Just an In-Memory Database](https://anuganti.com/articles/llm-inference-is-inmemory-db/)**
  maps modern AI serving to database kernels. The matching examples are 01, 05, 06 and 07: EXPLAIN INFERENCE,
  plans, estimates versus actuals, and cache reuse.
- **[Part 2: Your AI Agent Needs a Transaction Manager](https://anuganti.com/articles/ai-agent-needs-transaction-manager/)**
  asks what agentic AI can learn from 40 years of database systems. The matching examples are 02, 03 and 04:
  memory, agent tasks, and the ambiguous-commit recovery demo.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in keys and models; .env is git-ignored
EXPLAIN_PROVIDER=openai python examples/01_inference.py
python examples/04_transaction_recovery.py
```

**No API keys?** Replay a recorded run. The files in `samples/` are real journals captured from live runs;
the views are rendered from their events, with no keys and no network:

```bash
python -m explain replay samples/04_transaction_recovery.json    # the hero demo below
python -m explain replay samples/03_agent_task.json
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

Then the projection of the journal. Every field below is read back from recorded events, and the counters
agree with the trace: the retry was requested and **blocked** (none executed), and one recovery ran.
You can regenerate it without keys: `python -m explain replay samples/04_transaction_recovery.json`.

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
│ Retries           0 executed · 1 blocked                                                                             │
│ Recoveries        1 (reconciliations: 1)                                                                             │
│ Steps restored    0 from the step ledger (no re-execution)                                                           │
│ Inference errors  0                                                                                                  │
│                                                                                                                      │
│ Physical internalsUNAVAILABLE  (hosted provider)                                                                     │
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
┌─ EXPLAIN INFERENCE ────────────────────────────────────────────────┐
│ Provider            Gemini                                         │
│ Model               gemini-3.8-flash                               │
│ Request             wXvAaqP-Jvy639IP19iJ0Q8  OBSERVED              │
│ Router Decision     configured provider/model                      │
│ Finish              STOP                                           │
│                                                                    │
│ Physical internals  UNAVAILABLE  (hosted provider)                 │
└────────────────────────────────────────────────────────────────────┘ 

┌─ EXPLAIN ANALYZE ──────────────────────────────────────────────────┐
│ Input Tokens      74  OBSERVED                                     │
│ Output Tokens     197  OBSERVED                                    │
│ Cached Tokens     UNAVAILABLE                                      │
│ Reasoning Tokens  642  OBSERVED                                    │
│ Tool Tokens       UNAVAILABLE                                      │
│ Total Tokens      913  OBSERVED                                    │
│ TTFT              2.61s  OBSERVED                                  │
│ Total Latency     3.06s  OBSERVED                                  │
│ Retries           0                                                │
│ Estimated Cost    $0.003202  DERIVED                               │
└────────────────────────────────────────────────────────────────────┘
```

Every value is tagged by how it is known: `OBSERVED` (reported by the provider or measured by the client),
`DERIVED` (calculated from observed values) or `ESTIMATED`. Hosted APIs do not expose GPU placement, KV
occupancy, graph breaks or kernel fusion, so they collapse to one `Physical internals UNAVAILABLE` line; they
are never invented, and they expand into individual rows for any adapter that really exposes them.

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

### 6. The plan, then the actuals: `examples/06_explain_plan.py`

Hosted APIs hide physical placement, but the decisions the *runtime* makes are real: answer from memory instead
of inferring, which configured model to call, and what that should cost. The plan is a real decision over
observed inputs (memory coverage, each provider's own pre-flight token count, your price table), journaled as a
`PLAN_CHOSEN` event; the view is rendered from that event, then compared with what actually happened.

```bash
python examples/06_explain_plan.py
```

```
── Question 1 ──
┌─ EXPLAIN INFERENCE · plan ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Question        What is the preferred vendor for cloud licences?                                                                                             │
│ Router Decision answer from memory (no inference)                                                                                                            │
│ Rule            memory if its top item covers ≥75% of the question's words, else the cheapest worst-case model                                               │
├─ CANDIDATES  (chosen first; est. cost = worst case, at the output cap) ──────────────────────────────────────────────────────────────────────────────────────┤
│     PLAN                                     EST IN  EST OUT ≤   EST COST ≤  WHY                                                                             │
│ ▶  answer from memory (no inference)             0          0    $0.000000  memory item #1 covers 100% of the question (>= 75%)                              │
│ ✗  openai/gpt-5.2                               37      2,000    $0.028065  not needed: answered from memory                                                 │
│ ✗  gemini/gemini-flash-latest                   29      2,000    $0.007522  not needed: answered from memory                                                 │
│ ✗  anthropic/claude-sonnet-5                    53      2,000    $0.020106  not needed: answered from memory                                                 │
├─ PROVENANCE ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ EST IN is OBSERVED (provider pre-flight count). EST OUT is the ESTIMATED upper bound (the output cap). EST COST is ESTIMATED, derived from your price table. │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘ 

┌─ EXPLAIN ANALYZE · plan vs actual ─────────────────────────────────────────────────┐
│ Executed              memory lookup (no model call)                                │
│ Inference calls       0                                                            │
│ Tokens                0 in / 0 out                                                 │
│ Estimated Cost        $0.000000  (nothing to bill)                                 │
│ Lookup latency        2.06 ms  OBSERVED                                            │
└────────────────────────────────────────────────────────────────────────────────────┘ 

answer: Preferred vendor for cloud licences is Acme Cloud; renewals go through procurement.

── Question 2 ──
┌─ EXPLAIN INFERENCE · plan ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Question        Draft a polite two-sentence email asking Acme Cloud for a ten percent volume discount on three seats.                                        │
│ Router Decision gemini/gemini-flash-latest                                                                                                                   │
│ Rule            memory if its top item covers ≥75% of the question's words, else the cheapest worst-case model                                               │
├─ CANDIDATES  (chosen first; est. cost = worst case, at the output cap) ──────────────────────────────────────────────────────────────────────────────────────┤
│     PLAN                                     EST IN  EST OUT ≤   EST COST ≤  WHY                                                                             │
│ ▶  gemini/gemini-flash-latest                   40      2,000    $0.007530  lowest worst-case estimated cost among priced candidates                         │
│ ✗  answer from memory (no inference)             0          0    $0.000000  top item covers only 0% (< 75%)                                                  │
│ ✗  openai/gpt-5.2                               49      2,000    $0.028086  rejected: worst-case $0.0281 vs $0.0075 chosen                                   │
│ ✗  anthropic/claude-sonnet-5                    73      2,000    $0.020146  rejected: worst-case $0.0201 vs $0.0075 chosen                                   │
├─ PROVENANCE ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ EST IN is OBSERVED (provider pre-flight count). EST OUT is the ESTIMATED upper bound (the output cap). EST COST is ESTIMATED, derived from your price table. │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘ 

┌─ EXPLAIN ANALYZE · plan vs actual ─────────────────────────────────────────────────┐
│ Executed              gemini/gemini-3.8-flash                                      │
│ Input tokens          est 40 → actual 40   Δ +0                                    │
│ Output tokens         ≤ 2,000 → actual 43  (reasoning 417, reported separately)    │
│ Estimated Cost        ≤ $0.007530 → actual $0.001755  (23.3% of the bound)         │
│ TTFT                  1.96s                                                        │
│ Total latency         2.06s                                                        │
└────────────────────────────────────────────────────────────────────────────────────┘ 

answer: We are excited to move forward with purchasing three seats for our team and are eager to finalize
        our agreement with Acme Cloud. Given our commitment, would you be open to extending a ten
        percent volume discount on these licenses?

```

The first question is answered from memory, so no model is called. The second picks the candidate with the
lowest *worst-case* estimated cost and shows each rejected alternative with the numbers that rejected it. The
estimate is an upper bound, not a prediction: Gemini's reasoning tokens are reported separately from its output.

### 7. Prompt layout and cache reuse: `examples/07_prompt_layout_cache.py`

Providers only reuse a cached prefix when the prompt is large enough **and** its start is byte-identical between
calls. This sends the same ~5-7k token handbook two ways, twice each: stable text first (only the question
varies) versus a per-request header first. Each run's prefix is unique, so call 1 is genuinely cold.

```bash
python examples/07_prompt_layout_cache.py
```

```
┌─ EXPLAIN CACHE REUSE · what each provider reported for the SAME shared prompt ──────────────────────────────────────────────┐
│   PROVIDER   LAYOUT          CALL    INPUT       CACHED   CACHE WRITE     CACHED %        TTFT        COST                  │
│   OpenAI     stable-first    1       4,919            0   UNAVAILABLE           0%       3.53s   $0.011520                  │
│   OpenAI     stable-first    2       4,919        4,736   UNAVAILABLE          96%       3.20s   $0.004229                  │
│   OpenAI     volatile-first  1       4,932            0   UNAVAILABLE           0%       3.67s   $0.012061                  │
│   OpenAI     volatile-first  2       4,932            0   UNAVAILABLE           0%       1.97s   $0.010913                  │
├─ Gemini ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│   Gemini     stable-first    1       5,670  UNAVAILABLE   UNAVAILABLE  UNAVAILABLE       8.22s   $0.011647                  │
│   Gemini     stable-first    2       5,668  UNAVAILABLE   UNAVAILABLE  UNAVAILABLE       3.87s   $0.008320                  │
│   Gemini     volatile-first  1       5,691  UNAVAILABLE   UNAVAILABLE  UNAVAILABLE       7.70s   $0.011577                  │
│   Gemini     volatile-first  2       5,689  UNAVAILABLE   UNAVAILABLE  UNAVAILABLE       3.06s   $0.007462                  │
├─ Anthropic ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│   Anthropic  stable-first    1       7,431            0         7,410           0%       3.73s   $0.018162                  │
│   Anthropic  stable-first    2       7,429        7,410             0         100%       1.62s   $0.003390                  │
│   Anthropic  volatile-first  1       7,445            0         7,424           0%       2.16s   $0.016840                  │
│   Anthropic  volatile-first  2       7,443            0         7,424           0%       1.59s   $0.017036                  │
├─ HOW TO READ THIS ──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Call 1 is cold (the prefix is unique to this run), call 2 repeats it. CACHED is the provider-reported count of input tokens │
│ served from cache; UNAVAILABLE means the provider reported no figure. Gemini omits the field when it                        │
│ reports no cache use, so UNAVAILABLE there means no hit was reported, not a measured zero.                                  │
│ Only the placement of the volatile text differs between layouts. Cost is estimated from your price table.                   │
│ TTFT is time to the first visible text token. Caching is best-effort and provider-specific:                                 │
│ this is an observation, not a guarantee or a ranking.                                                                       │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

In this run, with the stable-first layout OpenAI reported 96% of the input as cached on the warm repeat, and
Anthropic read back everything it had written on call 1. With the volatile header first, neither reported any reuse.
Gemini reported no cached-token figure in either layout (the field is absent when no cache use is reported, so
that is not a measured zero). The same prompt is 4.9k, 5.7k and 7.4k tokens depending on the provider's
tokenizer. Caching is best-effort and provider-specific; this is one run's observation, not a guarantee.

## Inspecting a task

```bash
python -m explain list
python -m explain explain <task-id>     # EXPLAIN AGENT TASK
python -m explain trace   <task-id>     # trajectory
python -m explain events  <task-id>     # event journal as a table (add --json for developers)
python -m explain export  <task-id> --out samples/mine.json   # record a run (secrets already redacted)
python -m explain replay  samples/mine.json                   # render it again: no keys, no network
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

- This is a **proof of concept for demonstration**, not a real or production implementation, and not a production transaction manager.
- Arbitrary external tools do **not** participate in ACID transactions.
- **Compensation is not rollback**: it is a new, recorded action that reverses an effect. Handlers must be idempotent.
- Hosted model APIs do not expose all physical inference internals. GPU placement, KV occupancy, graph breaks
  and kernel fusion therefore remain `UNAVAILABLE` where the provider does not expose them.
- Cost is calculated from a local pricing table; treat it as estimated, not provider-billed.
- Provider token accounting differs; see the comparison note.
- Cache results (example 07) are single-run observations; providers cache on a best-effort basis and report it differently.
- The plan (example 06) is a simple, explicit rule over observed inputs, not a cost-based optimizer; its cost figures are upper bounds.
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
  plan.py           inference plan: chosen path + rejected alternatives (journaled as PLAN_CHOSEN)
  payments.py       local external payment service (separate SQLite, UNIQUE idempotency key)
  memory.py         agent memory (FTS5), separate from the journal
  explain_task.py   EXPLAIN AGENT TASK projection      trace.py   trajectory projection
  render.py         EXPLAIN INFERENCE / ANALYZE / MEMORY / COMPARE    cli.py   explain|trace|events
examples/  01_inference  02_memory  03_agent_task  04_transaction_recovery  05_compare_providers
           06_explain_plan  07_prompt_layout_cache
samples/   recorded real journals for keyless replay
tests/     behavior tests (no keys needed): idempotency, commit-unknown, reconciliation,
           checkpoint recovery, compensation, journal projection, capabilities, redaction,
           plan choice, prompt-layout prefix stability, replay
```

`python -m pytest -q` needs no API keys. The live behaviour is exercised by the examples.
