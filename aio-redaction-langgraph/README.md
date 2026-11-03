# aio-redaction-langgraph

A small command-line LangGraph app, integrated with AIO through the acceldata-aio-tracer
SDK. Pick this one to see how to keep personal and sensitive data out of AIO
while you still trace and debug every run. No LLM API key needed.

The app is traced with the
[`acceldata-aio-tracer`](https://pypi.org/project/acceldata-aio-tracer/) SDK. One
redaction policy covers the whole app, and each scenario shows one part of it:

- **Patterns:** email addresses, phone numbers, card numbers and street
  addresses, hidden wherever they appear, with an exception for the store's own
  email.
- **System prompts and tool definitions,** kept out of AIO.
- **A tool's arguments and results,** hidden whole.
- **A named attribute,** hidden on any span: here, the passages found in the
  staff handbook.
- **One agent's model inputs,** hidden as a whole.
- **A step that is never traced** at all.

Redaction happens as the telemetry leaves the app. The agents, the models and
your terminal always see the real values. Only what AIO receives changes, so
you can compare the terminal with the trace to see what was hidden.

The app plays the customer service desk of a small independent bookstore.
Customers write in, and their messages are full of personal data. Up to four
steps handle each message:

- **Support Triage** reads the message, works out what the customer wants, and
  looks them up with the `lookup_customer` tool.
- **Order Desk** looks up the order (`find_order`), changes the delivery address
  if asked (`update_address`), checks the staff handbook (`staff_handbook`, a
  retriever), and writes the reply.
- **Escalation**, for complaints only, writes a summary for the store manager.
- **internal_notes**, for complaints only, writes staff-only notes.

```text
START → support_triage → order_desk ──(not a complaint)──► END
                              │
                              ▼
                          escalation → internal_notes → END
```

Every agent uses `fixture-model`, a stand-in model that fills in a template, so
you need **no LLM API key**. All the customers, orders, emails, phone numbers
and card numbers are made up.

| File | What it is |
|---|---|
| `app.py` | The CLI: your own messages, the demo scenarios, and their guides |
| `graph.py` | Shared state, the agents, the tools, the handbook retriever, the graph |
| `models.py` | The fixture model every agent uses |
| `observability.py` | Everything AIO, including the redaction policy (`REDACTION`). The only file that imports the SDK |
| `data/` | The made-up customers and orders, and the staff handbook |
| `TOUR.md` | A guided walkthrough, one stop per scenario |

## How to run

### 1. Install

You need Python 3.10 or newer.

```bash
cd aio-redaction-langgraph
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
app it prints `AIO tracing is on, with redaction: traces go to project <your project>`.

### 3. Run it

**Write your own messages.** Pick one of the example messages by number, or
write as a customer. Mention an order number (#4471, #4488 or #4502) and
anything else you like: an email, a phone number, a card number, a new address.
The desk works out what you want from your words: a refund, a complaint, an
address change, or else a question about the order.

```bash
python app.py ask
python app.py ask "Where is order #4502? Call me on 555-310-4420."   # one message
```

Your messages go through the same policy as the scenarios. All messages in one
sitting form one session in AIO, with one trace per message.

**Run a demo scenario.** Each one sends a customer message that brings out one
part of the policy, then prints a guide: what happened, what to check in AIO,
and what to notice.

```bash
python app.py demo order-status
```

| Scenario | The customer message | What it shows |
|---|---|---|
| `order-status` | Priya asks about a late order and gives her email, phone and card number | Patterns, and the exception for the store's email |
| `address-change` | Marcus has moved and asks to change his delivery address | A tool's arguments and results, hidden whole |
| `refund-request` | Ana's book arrived damaged; the Order Desk checks the staff handbook and follows the internal refund rules in its system prompt | System prompts, tool definitions, and a named attribute |
| `complaint` | Priya complains a third time and shares something personal | One agent's model inputs hidden as a whole, and a step that is never traced |

**Run every scenario.** The tour runs them all in order and pauses after each
one so you can look at it in AIO. `TOUR.md` has the same walkthrough.

```bash
python app.py tour
```

## The redaction policy

The whole policy is `REDACTION` in `observability.py`. It is set once, when
tracing starts, and applies to every run:

```python
REDACTION = aio.redaction_policy(
    patterns=[EMAIL_ADDRESS, PHONE_NUMBER, CARD_NUMBER, STREET_ADDRESS],
    allow=is_store_address,
    hide_system_instructions=True,
    hide_tool_definitions=True,
    redact_tool_arguments=["lookup_customer"],
    redact_tool_results=["lookup_customer"],
    redact_attributes=["gen_ai.retrieval.documents"],
    suppress_spans=["internal_notes"],
)
```

| Setting | What it does, and what AIO shows instead |
|---|---|
| `patterns` | Regular expressions matched in messages, tool arguments and tool results. Each match becomes `__REDACTED__`, and the rest of the text stays |
| `allow` | Called with each match. Returning `True` keeps it. Here it keeps the store's own `support@bookstore.example` |
| `hide_system_instructions` | Every system prompt is withheld, and the system message is dropped from the model's input. AIO says "Only the prompt's fingerprint was exported — the text itself was not captured." |
| `hide_tool_definitions` | The tools sent to a model are withheld. The AIO UI doesn't show tool definitions, so nothing changes on screen |
| `redact_tool_arguments` | The named tools' arguments become a fingerprint, on the tool's span and inside the model's tool call |
| `redact_tool_results` | The named tools' results become a fingerprint |
| `redact_attributes` | The named attributes become a fingerprint, on any span. Patterns only scan messages and tool calls, so anything else has to be named here. `gen_ai.retrieval.documents` is what a retrieval found: here, internal handbook passages. The search query stays readable |
| `suppress_spans` | The named spans are never created, nor anything under them |

Two more ways to redact are written in the app's code, not in the policy:

- **`@hide_arguments` on a tool** (`graph.py`, `update_address`): the same as
  naming the tool in `redact_tool_arguments`, but on the tool itself.
- **`with private_model_inputs():` around a model call** (`graph.py`,
  `escalation`): everything sent to the model inside the block, its system
  prompt and its messages, is withheld, whatever the policy says. Only a
  fingerprint is sent, which AIO doesn't display: the span's input says "No input preview was ingested for this span."
  The model's reply is still traced.

A fingerprint (`sha256:...`) replaces a whole value. It is the same every time
the value is the same, so you can tell that two runs looked up the same
customer, or found the same handbook passage, without seeing it.

**Why the app sends no trace input preview.** `aio.root_span()` can take an
`input_preview`, which AIO shows as the trace's input. Here it would be the
customer's raw message, and patterns don't scan it. Hiding it with
`redact_attributes=["aio.input_preview"]` works, but then every trace's input in
AIO is a fingerprint. So the app leaves it out. AIO then shows the first agent's
input as the trace's input, and patterns already apply there.

**Which attributes are worth naming.** AIO shows a span's input and output from
a fixed set of attributes: model messages, tool arguments and results, and
retrieval queries and documents. Hiding one of those changes what you see.
Other attributes can be hidden too, but the AIO UI doesn't show custom span
attributes yet, and it fills a trace's user from the API key's owner, not from
the SDK's `user_id`.


## Troubleshooting

- **`AIO tracing is off`:** a setting is missing from your terminal. Run
  `set -a; source .env; set +a` again. If it prints `command not found`, a line
  in `.env` has a space after `=`.
- **`AIO has no project with ID ...`:** `AIO_PROJECT_ID` must be the project's
  ID, not its name.
- **No traces in AIO:** set `DEBUG_LOGS = True` at the top of `observability.py`
  and run again to see the SDK's logs. Check that `AIO_ENDPOINT` ends in `/aio`.
- **Real values in AIO:** open the newest trace. Redaction only applies to
  telemetry sent after the policy was set.
- **Something you expected to be hidden is readable:** check which setting
  should cover it. A pattern has to match the value exactly as it was written,
  and a tool or span has to be named exactly as it shows in the trace.
