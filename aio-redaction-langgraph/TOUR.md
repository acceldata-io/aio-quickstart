# Guided tour

Five stops. The first four each pair a command with what to look for in AIO
and what to notice. The last one shows how the redaction is wired into the
app. You can also run `python app.py tour`, which runs every scenario and
prints the same guides.

Before you start, follow the README to install, and load your AIO settings
(`set -a; source .env; set +a`).

Every run uses the same policy, `REDACTION` in `observability.py`. Each scenario
sends a customer message that brings out one part of it. Your terminal always
shows the real values: compare it with the trace in AIO to see what was
hidden.

Two kinds of hidden value:

- `__REDACTED__` replaces the part of a text that a pattern matched. The rest of
  the text stays.
- `sha256:...` is a fingerprint that replaces a whole value. It is the same
  every time the value is the same.

---

## Stop 1: patterns (`order-status`)

```bash
python app.py demo order-status
```

**AIO code:** `observability.py` → `REDACTION`: `patterns` and `allow`.

**What happened.** Priya asked about a late order and gave her email, phone and
card number. Support Triage looked her up, and the Order Desk found the order
and added store credit. Your terminal shows every real value. AIO got a
redacted copy.

**Check in AIO**

1. Open your project → latest trace named `order-status`.
2. Open `chat fixture-model` under `support_triage`: in the customer's message the email, phone and card number each read `__REDACTED__`. The rest of the sentence is still there.
3. Open `chat fixture-model` under `order_desk` (the second one): the reply ends with `support@bookstore.example`, unredacted.
4. Look at the trace's input in the trace list: it is the same message, with the same values hidden.

**What to notice**

- Patterns hide only the part that matches, so the message still makes sense in AIO.
- The store's own email survives because of `allow=is_store_address`: an email pattern on its own would have hidden it too.
- Priya's name is still readable: no pattern matches names. To hide a whole message, see Stop 4.
- The trace's input is the first agent's input, so patterns already apply to it. The app sends no separate input preview: that would be the raw message, in an attribute patterns don't scan (README → Why the app sends no trace input preview).

**Try it yourself.** Run `python app.py ask` and write a message with your own
made-up email or phone number, then find it in the trace.

---

## Stop 2: tool arguments and results (`address-change`)

```bash
python app.py demo address-change
```

**AIO code:** `observability.py` → `REDACTION`: `redact_tool_arguments` and
`redact_tool_results`. `graph.py` → `@hide_arguments` on `update_address`.

**What happened.** Marcus moved house. Support Triage looked him up, and the
Order Desk changed the delivery address of his order, which hadn't shipped yet.

**Check in AIO**

1. Open your project → latest trace named `address-change`.
2. Open `execute_tool lookup_customer`: its arguments and its result are both `sha256:...` fingerprints. The call itself, its timing and its status are all there.
3. Open `execute_tool update_address`: its arguments are a fingerprint, its result (`{"updated": true}`) is not.
4. Open `execute_tool find_order`: nothing is hidden. Its arguments and result have no personal data.

**What to notice**

- Two ways to hide a tool's arguments: `lookup_customer` is named in the policy (`redact_tool_arguments`); `update_address` is marked in its own code (`@hide_arguments` in `graph.py`).
- Hiding a tool's arguments also hides them inside the model's tool call, one span away, on the chat span under `support_triage`. The AIO UI shows only a model's reply text, not its tool calls, but the arguments are stored and would leak if only the tool span were covered.
- A tool's results are hidden separately (`redact_tool_results`): often they are your own data, not secret.
- Support Triage hands on only the customer ID and first name, not the email, phone and address `lookup_customer` returned. Redaction can't help if the next prompt repeats them.
- The new address in the customer's own message reads `__REDACTED__`: that is the `STREET_ADDRESS` pattern, not the tool rule.

---

## Stop 3: system prompts, tool definitions and a named attribute (`refund-request`)

```bash
python app.py demo refund-request
```

**AIO code:** `observability.py` → `REDACTION`: `hide_system_instructions`,
`hide_tool_definitions` and `redact_attributes`. `graph.py` → `StaffHandbook`.

**What happened.** Ana's book arrived damaged. The Order Desk checked the staff
handbook and refunded the order in full, following the store's internal rules in
its system prompt.

**Check in AIO**

1. Open your project → latest trace named `refund-request`.
2. Open `chat fixture-model` under `order_desk`: where the system prompt would be, AIO says "Only the prompt's fingerprint was exported — the text itself was not captured." The system message is gone from the input messages too.
3. Open `retrieval staff_handbook` under `order_desk`: its input, the search query (`refund damaged order`), is readable. Its output, the handbook passage found, is a `sha256:...` fingerprint.

**What to notice**

- The reply still makes sense: hiding the prompt doesn't hide what the model did with it.
- Hiding system prompts covers every model call, not only this agent's.
- The tool definitions (`find_order`, `update_address`) are hidden too, though the AIO UI doesn't show tool definitions, so there is nothing to see there.
- Patterns don't scan a retrieval's results, so the policy names that attribute (`gen_ai.retrieval.documents`) in `redact_attributes`. Any span attribute can be hidden this way.
- The Order Desk passes the model only the passage's ID (`HB-01`), not its text, so the text reaches AIO only on the retrieval span.

---

## Stop 4: one agent's inputs, and a step never traced (`complaint`)

```bash
python app.py demo complaint
```

**AIO code:** `graph.py` → `escalation()` uses `private_model_inputs()`.
`observability.py` → `REDACTION`: `suppress_spans`.

**What happened.** Priya complained a third time and shared something personal.
The Order Desk replied, Escalation wrote a summary for the store manager, and
`internal_notes` wrote staff-only notes with her phone and home address
(printed in your terminal).

**Check in AIO**

1. Open your project → latest trace named `complaint`.
2. Open `chat fixture-model` under `escalation`: its input says "No input preview was ingested for this span." and its system prompt says "Only the prompt's fingerprint was exported — the text itself was not captured." Its output, the manager summary, is readable.
3. Compare `chat fixture-model` under `support_triage`: the same complaint is readable there, with only the phone number hidden.
4. Look for `internal_notes` after `escalation`: there is no such span. The graph ran it, AIO never got it.

**What to notice**

- `private_model_inputs()` hides everything sent to a model inside one block of code. It is for an agent whose prompts are private as a whole, not just in places.
- It works without a policy, and only on model calls: `escalation`'s node span is still traced.
- Patterns can't recognise a personal story. Where a whole message must stay private, hide the model's inputs as a whole.
- `suppress_spans` stops a span being created at all, so there is nothing to redact. It also drops everything under it, so only use it on steps with nothing worth seeing inside.
- A node's saved input and output (`@record_node`) are sent separately from its span, so suppressing the span doesn't stop them. That is why `internal_notes` has no `@record_node`.

---

## Stop 5: how the redaction is wired in

All the AIO code is in `observability.py`. Nothing else in the app imports the
SDK.

| AIO code in `observability.py` | SDK call | What it does |
|---|---|---|
| `REDACTION`, the policy | `aio.redaction_policy(...)` | Passed to `aio.init(redaction=REDACTION)` once, at start-up, and applied to every span and log record as it leaves the app |
| `hide_arguments()`, used as `@hide_arguments` on a tool | `aio.redact_arguments(tool)` | Adds the tool to the ones whose arguments are hidden |
| `private_model_inputs()`, used as a `with` block around model calls | `aio.redact_model_inputs()` | Marks every model call started inside the block so its inputs are hidden |

Things to notice:

- The policy is fixed when tracing starts. To change it, change `REDACTION` and
  start the app again.
- Redaction runs on the way out, so it covers every span the SDK traces: model
  calls, tool calls, graph nodes, and libraries the app doesn't know about.
- If redaction fails on a span, the SDK hides that span's content rather than
  sending it unredacted.

**Not seeing it?** No trace → check the `AIO_*` settings, then turn on
`DEBUG_LOGS` in `observability.py` and run again (README → Troubleshooting).
Real values in AIO → check you opened the newest trace. Redaction only applies
to telemetry sent after it is set.
