# Guided tour

Seven stops. Each one pairs a command with what to look for in AIO and how to
debug it. You can also run
`python app.py tour`, which runs every scenario and prints the same guides.

Before you start:

1. Follow the README to install, and load your AIO settings (`set -a; source .env; set +a`).
2. Create the rules with `python create_rules.py`, then wait about a minute.
   Rules only check telemetry that arrives after they exist.

The guides tell you what evidence to look for. Whether a rule actually flagged
something is for you to check in AIO. A scenario that fails doesn't prove a
rule fired.

---

## Stop 1: the baseline and the handoff (`happy-path`)

```bash
python app.py demo happy-path
```

**What happened.** The analyst found 3 low-stock books and handed them to the
coordinator through `findings`. The supplier answered first time, and the
recommendation passed validation. Only the model is simulated (`fixture-model`).

**Check in AIO**

1. Open your project → latest trace named `happy-path`.
2. Expand `invoke_workflow bookstore`: `inventory_analyst` → `supplier_coordinator` → `validate` → `report`.
3. Open `invoke_agent supplier_coordinator`: its input is the analyst's findings. That is the handoff.
4. Go to the Traces section and the Spans section: none of the rules from `create_rules.py` should flag this trace or any of its spans.

**Debug it**

- Open a `chat` span: the system prompt is the agent's instructions, and the user message is the facts as JSON.
- Open `execute_tool check_supplier`: its arguments (`attempt=1`) and its result.
- Keep this trace as the baseline to compare every other scenario against.

**Not seeing it?** No trace → check the `AIO_*` settings, then turn on
`DEBUG_LOGS` in `observability.py` and run again. A rule flagged it → that rule is set too strictly.

---

## Stop 2: span-level rules (`banned-term` and `empty-output`)

Both scenarios replace the coordinator's model reply with a fixed bad answer.

### `banned-term`

```bash
python app.py demo banned-term
```

**What happened.** The coordinator's reply (a fixture, simulated) recommends a
competitor, "MegaBooks". The app's own checks don't look for that, so the run
ends normally (outcome: ordered).

**Check in AIO**

1. Open your project → latest trace named `banned-term`.
2. Expand `supplier_coordinator` → `chat fixture-model`: the output mentions MegaBooks.
3. Go to the Spans section and find this trace's `chat` span under `supplier_coordinator`. If `banned_term` is configured with the term `MegaBooks`, check whether the rule flagged that span.

**Debug it**

- Read the span's output and find the banned term.
- Compare it with the system prompt on the same span, which says not to mention other bookstores.
- Ask: would the app have caught this on its own? No. `validate` passed it.

**Not seeing it?** No rule flag → check the term is exactly `MegaBooks`, the rule
is enabled for your project, and it was created before the run.

### `empty-output`

```bash
python app.py demo empty-output
```

**What happened.** The coordinator's model call succeeded but returned only
whitespace (a fixture, simulated). `validate` saw the empty recommendation and
sent the run to `failure_report` (outcome: failed).

**Check in AIO**

1. Open your project → latest trace named `empty-output`.
2. Expand `supplier_coordinator` → `chat fixture-model`: the output is blank, yet the span is not an error.
3. The run ends at `failure_report` instead of `report`.
4. Go to the Spans section and find this trace's `chat` span under `supplier_coordinator` (the blank one). If `empty_output` is configured, check whether the rule flagged that span.

**Debug it**

- The model span looks healthy: OK status, finish reason `stop`. Only the content is missing.
- Open `validate`: its input shows the empty recommendation. `failure_report`'s input shows the error.
- Ask: why would a model return nothing? With a real model, check its token limit and content filters.

**Not seeing it?** No rule flag → check the rule is enabled and was created before the run.

---

## Stop 3: the hidden failure (`agent-retry`)

```bash
python app.py demo agent-retry
```

**What happened.** The supplier lookup failed on attempt 1 (simulated timeout),
then succeeded on attempt 2 after a 1s backoff. Each attempt is its own tool
call, so each attempt gets its own span. The user got a normal
recommendation.

**Check in AIO**

1. Open your project → latest trace named `agent-retry`.
2. Expand `supplier_coordinator`: there are two `check_supplier` spans, and the first one is marked as an error.
3. Go to the Traces section and find this `agent-retry` trace. If `trace_error_spans` is configured, check whether the rule flagged it.

**Debug it**

- Open the failed span: read the error message and the attempt number in its arguments.
- Compare its end time with the second span's start: the ~1s gap is the backoff.
- Ask: is the retry hiding a real upstream problem? The final answer looks fine,
  which is exactly why this rule exists.

**Not seeing it?** No rule flag → check the rule is enabled and was created before the run.

---

## Stop 4: the slow span (`slow-supplier`)

```bash
python app.py demo slow-supplier
```

**What happened.** The supplier tool waited 6s before answering (simulated, set
by `SLOW_SUPPLIER_SECONDS` in `graph.py`). The run succeeded, but took longer than a 5s budget.

**Check in AIO**

1. Open your project → latest trace named `slow-supplier`.
2. Expand `supplier_coordinator`: `execute_tool check_supplier` takes about 6s, almost the whole trace.
3. Go to the Traces section and find this `slow-supplier` trace. If `trace_duration_bound` is configured (5s budget), check whether the rule flagged it.

**Debug it**

- Compare span durations: the tool span takes seconds, while the model spans take milliseconds.
- Compare with the happy-path trace: the same spans, only the tool span is slower.
- Ask: is the supplier slow, or are we calling it badly? Should this call have a timeout?

**Not seeing it?** No rule flag → the budget must be below the run time. If you
lowered `SLOW_SUPPLIER_SECONDS` in `graph.py`, lower the budget too.

---

## Stop 5: the session view (`repeat-questions`)

```bash
python app.py demo repeat-questions
```

**AIO code:** `observability.py` → `traced_run()`. All five runs pass the same
session ID to `aio.root_span()` as `conversation_id`, which is what makes them
one session.

**What happened.** Five runs under one session ID. Run 1 asked about the sci-fi
shelf, which is healthy, so the supplier step was skipped. Runs 2–5 asked about
*The Midnight Library* in different words. Every run succeeded.

**Check in AIO**

1. Open your project → the session with the ID printed at the top of the output.
2. It holds five traces named `repeat-questions`. Run 1 has no `supplier_coordinator` span (the skip branch).
3. Go to the Sessions section and find this session. If `session_turn_count` is configured (maximum 4 turns), check whether the rule flagged it.

**Debug it**

- Read the inputs of runs 2–5 side by side: the same question in different words.
- Every trace is green. The problem only shows when you look at the whole session.
- Ask: why does the manager keep asking? The answer says what to order, not when
  the book will be back.

**Not seeing it?** No session → the runs must share one session ID. No rule flag
→ the maximum must be below 5 turns.

---

## Stop 6: how the app is instrumented

All the AIO code is in `observability.py`, about 180 lines. Nothing else in the
app imports the SDK.

| AIO code in `observability.py` | SDK call | What it gives you in AIO |
|---|---|---|
| `start_tracing()`, called once at start-up | `aio.init(service=..., tenant_id=..., project_id=..., endpoint=...)` | Automatic tracing of every graph, node, model call and tool call |
| `traced_run()`, around each graph run | `aio.root_span(conversation_id=..., name=..., input_preview=...)` | One trace per run, named after the scenario, with the session ID on every span |
| `record_node()`, used as `@record_node(...)` on each graph node | `aio.langgraph_node(input=..., output=...)` | Each node's input and output on its `invoke_agent` span |
| `flush()`, before the app exits | OpenTelemetry `force_flush()` | Nothing lost when the CLI exits right after a run |

Things to notice:

- There are no manual spans. Model calls, tool calls, and the graph and node
  spans all come from automatic instrumentation once `aio.init()` has run.
- `aio.init()` must run before any model client is created, or those calls
  aren't traced.
- The graph is built inside the root span (`run_question()` in `app.py`).
  Building it creates a `create_agent` span, which would otherwise become a
  trace of its own.

---

## Stop 7: tokens and cost

```bash
python app.py demo happy-path
```

**AIO code:** `observability.py`: `FIXTURE_MODEL_PRICE` (the demo price set at
start-up).

**What happened.** `fixture-model` reported input and output token counts on
every call. At start-up the app made sure `fixture-model` has a price in your
tenant, so AIO works out a cost for each model call from its tokens and that
price.

**Check in AIO**

1. Open your project → latest trace named `happy-path`.
2. Open `chat fixture-model` under `inventory_analyst`, then under `supplier_coordinator`: find the token counts and the cost on each span.
3. Compare the two calls: the one with more tokens costs more.

**Debug it**

- Cost = (input tokens × $3 + output tokens × $15) ÷ 1,000,000. Check it against the span's numbers.
- Costs are worked out when a call arrives in AIO, so a new price applies to later runs only.

**Not seeing it?** No cost → see README → Change the cost: the start-up
output says whether the price was set.
