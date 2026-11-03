# aio-base-langgraph

A small command-line LangGraph app, integrated with AIO through the acceldata-aio-tracer SDK. Start
here to explore AIO's main features: traces, spans and sessions, token cost,
and rules. No LLM API key needed.

The app is traced with the
[`acceldata-aio-tracer`](https://pypi.org/project/acceldata-aio-tracer/) SDK and
shows:

- **Traces, spans and sessions:** each run is a trace, each agent, model call
  and tool call is a span, and the runs in one sitting form a session.
- **Tokens and cost:** the token counts and cost of every model call.
- **Rules:** catching problems at three levels: one span, one trace, and a
  whole session.

The app plays a small independent bookstore. The manager asks a stock question
and two agents answer it together:

- **Inventory Analyst** reads sales and stock, finds books that are running low,
  and writes a short summary.
- **Supplier Coordinator** takes the analyst's findings, asks the supplier for
  stock and lead times, and writes the reorder recommendation.

The two agents are nodes in one LangGraph graph. They pass work to each other
only through the shared state (`findings`), so the handoff is easy to see in the
code and in the trace.

```text
START → inventory_analyst ──(nothing low)──────────────────────► report → END
              │
              ▼
        supplier_coordinator ──(supplier failed twice)──► failure_report → END
              │
              ▼
          validate ──(empty or invalid)─────────────────► failure_report
              │
              ▼
           report → END
```

Both agents use `fixture-model`, a stand-in model that fills in a template, so
you need **no LLM API key**.

| File | What it is |
|---|---|
| `app.py` | The CLI: your own questions, the demo scenarios, and their guides |
| `graph.py` | Shared state, both agents, the tools, the simulated problems (`FAULTS`), the graph |
| `models.py` | The fixture model both agents use |
| `observability.py` | Everything AIO: start-up and the demo model price. The only file that imports the SDK |
| `create_rules.py` | Creates the AIO rules the scenarios trigger |
| `TOUR.md` | A guided walkthrough, one stop per scenario |

## How to run

### 1. Install

You need Python 3.10 or newer.

```bash
cd aio-base-langgraph
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Add your AIO settings

In AIO, create a project (any name). Copy the example settings file:

```bash
cp .env.example .env
```

Open `.env` and fill it in, with no spaces around `=`:

| Setting | What to put |
|---|---|
| `AIO_ENDPOINT` | Your Acceldata URL followed by `/aio` |
| `AIO_TENANT_ID` | Your tenant ID |
| `AIO_ACCESS_KEY`, `AIO_SECRET_KEY` | Your Acceldata access key pair |
| `AIO_PROJECT_ID` | The ID of the project you created (the ID, not its name) |

Load the settings into your terminal. Do this again in every new terminal:

```bash
set -a; source .env; set +a
```

`.env` is git-ignored, so your keys stay out of the repository. When you run the
app it prints `AIO tracing is on: traces go to project <your project>`.

### 3. Run it

**Ask questions.** Pick one of the sample questions by number, or type your own.
The agents understand book titles (part of one is enough, like "Harry Potter")
and shelves: literary, sci-fi, mystery, history and fantasy.

```bash
python app.py ask
python app.py ask "Do we have enough Harry Potter?"   # one question
```

All questions in one sitting form one session in AIO, with one trace per
question. Each model call shows its input and output tokens and its cost.

**Run a demo scenario.** Each one sets up a problem for an AIO rule to catch,
then prints a guide: what happened, what to check in AIO, and how to debug it.

```bash
python app.py demo happy-path
```

| Scenario | What happens | Rule that should catch it |
|---|---|---|
| `happy-path` | Both agents finish the handoff and give a valid recommendation | None: the clean baseline |
| `banned-term` | The coordinator's reply recommends a competitor, "MegaBooks" | `banned_term` (span) |
| `empty-output` | The coordinator's model call returns only whitespace | `empty_output` (span) |
| `agent-retry` | The first supplier lookup times out, the retry works | `trace_error_spans` (trace) |
| `slow-supplier` | The supplier takes 6 seconds to answer | `trace_duration_bound` (trace) |
| `repeat-questions` | Five runs in one session, asking the same question again and again | `session_turn_count` (session) |

**Run every scenario.** The tour runs them all in order and pauses after each
one so you can look at it in AIO. `TOUR.md` has the same walkthrough.

```bash
python app.py tour
```

## Create the rules

The scenarios only set up the problems. To see AIO flag them, create the rules
in your project with one command (with your settings loaded):

```bash
python create_rules.py
```

It creates one rule per scenario, and skips any that already exist:

| Rule | Checks | Value |
|---|---|---|
| `banned_term` | A span's output matches a pattern | `(?i)megabooks` |
| `empty_output` | A span's output is empty | — |
| `trace_error_spans` | A trace has more failed spans than allowed | 0 |
| `trace_duration_bound` | A trace takes longer than allowed | 5 seconds |
| `session_turn_count` | A session has more turns than allowed | 4 turns |

Rules only check telemetry that arrives after they exist, so wait about a minute
before running the scenarios. To use other values, edit `RULES` in
`create_rules.py`. You can also create or edit rules in AIO, in your project's
**Rules** section.

## Change the cost

`fixture-model` reports token counts on every call, and AIO works out each
call's cost from them and the model's price. A model needs a price before AIO
can show its cost, so the app sets one for `fixture-model` in your tenant the
first time it runs: $3 per million input tokens and $15 per million output
tokens. You'll see the tokens and cost on each `chat fixture-model` span.

To change the price, or to set one for a model in your own app, call the
model-price API (prices are US dollars per million tokens, and all four are
required):

```bash
curl -X PUT "$AIO_ENDPOINT/api/aio/model-prices?model=fixture-model" \
  -H "accessKey: $AIO_ACCESS_KEY" -H "secretKey: $AIO_SECRET_KEY" \
  -H "Content-Type: application/json" \
  -d '{"inputUsdPerM": 3, "outputUsdPerM": 15, "cacheReadUsdPerM": 0, "cacheWriteUsdPerM": 0}'
```

Costs are worked out when a call arrives in AIO, so a new price applies to later
runs only. The app never overwrites a price that already exists.

## Troubleshooting

- **`AIO tracing is off`:** a setting is missing from your terminal. Run
  `set -a; source .env; set +a` again. If it prints `command not found`, a line
  in `.env` has a space after `=`.
- **`AIO has no project with ID ...`:** `AIO_PROJECT_ID` must be the project's
  ID, not its name.
- **No traces in AIO:** set `DEBUG_LOGS = True` at the top of `observability.py`
  and run again to see the SDK's logs. Check that `AIO_ENDPOINT` ends in `/aio`.
- **A trace but no rule flag:** the rule must exist before the run. Run
  `python create_rules.py`, wait a minute, and run the scenario again.
- **No cost on model spans:** if the app printed `Could not set a price`, set it
  with the curl command above.
