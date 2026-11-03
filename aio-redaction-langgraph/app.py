"""Command line for the customer service desk demo.

    python app.py ask                 write your own customer messages (shows examples)
    python app.py ask "<message>"     send one message
    python app.py demo <scenario>     run one scenario, then print its guide
    python app.py list                show the scenarios and what each one redacts
    python app.py tour                run every scenario in order, with a guide after each

Every run uses the same redaction policy (REDACTION in observability.py).
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass

import observability
from graph import build_graph


@dataclass(frozen=True)
class Scenario:
    message: str   # what the customer writes
    shows: str     # the part of the policy this scenario is about


SCENARIOS: dict[str, Scenario] = {
    "order-status": Scenario(
        "Hi, this is Priya Nair. My order #4471 still hasn't arrived. You can reach me at "
        "priya.nair@example.com or 555-201-7788. I paid with card 4242 4242 4242 4242.",
        shows="patterns, and an exception for the store's email",
    ),
    "address-change": Scenario(
        "Hello, it's Marcus Lee (marcus.lee@example.com). I've moved, so please send order #4502 "
        "to 48 Harbour Road, Apt 3, Portland instead.",
        shows="tool arguments and results",
    ),
    "refund-request": Scenario(
        "Hi, order #4488 arrived with a torn cover. Can I get a refund? My email is ana.silva@example.com.",
        shows="system prompts, tool definitions, a named attribute",
    ),
    "complaint": Scenario(
        "This is the third time order #4471 is late! I'm Priya Nair, 555-201-7788. I needed it for my "
        "daughter's hospital stay. I want to speak to a manager.",
        shows="one agent's model inputs, a span never traced",
    ),
}

# Shown by `python app.py ask`. Type the number to use one.
EXAMPLE_MESSAGES = [scenario.message for scenario in SCENARIOS.values()]


# --- Scenario guides ---------------------------------------------------------------
# The one place the guide text lives. `demo` and `tour` print it, and TOUR.md
# has a section for every entry. Keep the two in step when you edit either.


@dataclass(frozen=True)
class Guide:
    what_happened: str
    check_in_aio: list[str]
    notice: list[str]
    not_seeing_it: list[str]


NO_TRACE = "No trace -> check the AIO_* settings, then turn on DEBUG_LOGS in observability.py and run again (README -> Troubleshooting)."
OLD_TRACE = "Real values in AIO -> check you opened the newest trace. Redaction only applies to telemetry sent after it is set."

GUIDES: dict[str, Guide] = {
    "order-status": Guide(
        what_happened=(
            "Priya asked about a late order and gave her email, phone and card number.\n"
            "Support Triage looked her up, the Order Desk found the order and added store credit.\n"
            "Your terminal shows every real value. AIO got a redacted copy."
        ),
        check_in_aio=[
            "Open project {project} -> latest trace named order-status.",
            "Open chat fixture-model under support_triage: in the customer's message the email, phone and card number each read __REDACTED__. The rest of the sentence is still there.",
            "Open chat fixture-model under order_desk (the second one): the reply ends with support@bookstore.example, unredacted.",
            "Look at the trace's input in the trace list: it is the same message, with the same values hidden.",
        ],
        notice=[
            "Patterns hide only the part that matches, so the message still makes sense in AIO.",
            "The store's own email survives because of allow=is_store_address: an email pattern on its own would have hidden it too.",
            "Priya's name is still readable: no pattern matches names. To hide a whole message, see the complaint scenario.",
            "The trace's input is the first agent's input, so patterns already apply to it. The app sends no separate input preview: that would be the raw message, in an attribute patterns don't scan.",
            "AIO code: observability.py -> REDACTION: patterns and allow."
        ],
        not_seeing_it=[NO_TRACE, OLD_TRACE],
    ),
    "address-change": Guide(
        what_happened=(
            "Marcus moved house. Support Triage looked him up, and the Order Desk changed the\n"
            "delivery address of his order, which hadn't shipped yet."
        ),
        check_in_aio=[
            "Open project {project} -> latest trace named address-change.",
            "Open execute_tool lookup_customer: its arguments and its result are both sha256:... fingerprints. The call itself, its timing and its status are all there.",
            "Open execute_tool update_address: its arguments are a fingerprint, its result ({\"updated\": true}) is not.",
            "Open execute_tool find_order: nothing is hidden. Its arguments and result have no personal data.",
        ],
        notice=[
            "Two ways to hide a tool's arguments: lookup_customer is named in the policy (redact_tool_arguments); update_address is marked in its own code (@hide_arguments in graph.py).",
            "Hiding a tool's arguments also hides them inside the model's tool call, one span away, on the chat span under support_triage. The AIO UI shows only a model's reply text, not its tool calls, but the arguments are stored and would leak if only the tool span were covered.",
            "A tool's results are hidden separately (redact_tool_results): often they are your own data, not secret.",
            "Support Triage hands on only the customer ID and first name, not the email, phone and address lookup_customer returned. Redaction can't help if the next prompt repeats them.",
            "The new address in the customer's own message reads __REDACTED__: that is the STREET_ADDRESS pattern, not the tool rule.",
        ],
        not_seeing_it=[NO_TRACE, OLD_TRACE],
    ),
    "refund-request": Guide(
        what_happened=(
            "Ana's book arrived damaged. The Order Desk checked the staff handbook and refunded\n"
            "the order in full, following the store's internal rules in its system prompt."
        ),
        check_in_aio=[
            "Open project {project} -> latest trace named refund-request.",
            "Open chat fixture-model under order_desk: where the system prompt would be, AIO says \"Only the prompt's fingerprint was exported — the text itself was not captured.\" The system message is gone from the input messages too.",
            "Open retrieval staff_handbook under order_desk: its input, the search query (refund damaged order), is readable. Its output, the handbook passage found, is a sha256:... fingerprint.",
        ],
        notice=[
            "The reply still makes sense: hiding the prompt doesn't hide what the model did with it.",
            "Hiding system prompts covers every model call, not only this agent's.",
            "The tool definitions (find_order, update_address) are hidden too, though the AIO UI doesn't show tool definitions, so there is nothing to see there.",
            "Patterns don't scan a retrieval's results, so the policy names that attribute (gen_ai.retrieval.documents) in redact_attributes. Any span attribute can be hidden this way.",
            "The Order Desk passes the model only the passage's ID (HB-01), not its text, so the text reaches AIO only on the retrieval span.",
            "AIO code: observability.py -> REDACTION: hide_system_instructions, hide_tool_definitions and redact_attributes. graph.py -> StaffHandbook.",
        ],
        not_seeing_it=[NO_TRACE, OLD_TRACE],
    ),
    "complaint": Guide(
        what_happened=(
            "Priya complained a third time and shared something personal. The Order Desk replied,\n"
            "Escalation wrote a summary for the store manager, and internal_notes wrote\n"
            "staff-only notes with her phone and home address (printed above)."
        ),
        check_in_aio=[
            "Open project {project} -> latest trace named complaint.",
            "Open chat fixture-model under escalation: its input says \"No input preview was ingested for this span.\" and its system prompt says \"Only the prompt's fingerprint was exported — the text itself was not captured.\" Its output, the manager summary, is readable.",
            "Compare chat fixture-model under support_triage: the same complaint is readable there, with only the phone number hidden.",
            "Look for internal_notes after escalation: there is no such span. The graph ran it, AIO never got it.",
        ],
        notice=[
            "private_model_inputs() hides everything sent to a model inside one block of code. It is for an agent whose prompts are private as a whole, not just in places.",
            "It works without a policy, and only on model calls: escalation's node span is still traced.",
            "Patterns can't recognise a personal story. Where a whole message must stay private, hide the model's inputs as a whole.",
            "suppress_spans stops a span being created at all, so there is nothing to redact. It also drops everything under it, so only use it on steps with nothing worth seeing inside.",
            "A node's saved input and output (@record_node) are sent separately from its span, so suppressing the span doesn't stop them. That is why internal_notes has no @record_node.",
            "AIO code: graph.py -> escalation() uses private_model_inputs(); observability.py -> REDACTION: suppress_spans.",
        ],
        not_seeing_it=[NO_TRACE, OLD_TRACE, "No escalation span -> the message must read as a complaint, for example by asking for a manager."],
    ),
}


def format_guide(name: str) -> str:
    """Turn one scenario's guide into the text block the CLI prints."""
    guide = GUIDES[name]
    width = 72
    lines = [f"-- {name} ".ljust(width, "-"), "WHAT HAPPENED"]
    lines += [f"  {line}" for line in guide.what_happened.splitlines()]
    lines.append("\nCHECK IN AIO")
    lines += [f"  {number}. {step}" for number, step in enumerate(guide.check_in_aio, start=1)]
    lines.append("\nWHAT TO NOTICE")
    lines += [f"  - {point}" for point in guide.notice]
    lines.append("\nNOT SEEING IT?")
    lines += [f"  {hint}" for hint in guide.not_seeing_it]
    lines.append("-" * width)
    # The guides say "{project}" wherever the AIO project's name goes.
    return "\n".join(lines).replace("{project}", observability.project_label())


# --- Running messages and scenarios -------------------------------------------------


def run_message(message: str, session_id: str, trace_name: str) -> dict:
    """Run the graph once for one customer message, as one trace. Returns the final state."""
    # The graph is built inside the trace so its set-up span lands in the
    # same trace, not in a separate one.
    with observability.traced_run(trace_name, session_id):
        return build_graph().invoke({"message": message})


def print_run(number: int, state: dict) -> None:
    """Show what the desk did with one message: the real values, before redaction."""
    print(f"\n[run {number}] Customer: {state['message']}")
    print(f"  intent: {state.get('intent')}   outcome: {state.get('outcome')}")
    print("\nReply to the customer:")
    print(state.get("reply", ""))
    if state.get("manager_summary"):
        print("\nSummary for the store manager:")
        print(state["manager_summary"])
    if state.get("staff_notes"):
        print("\nStaff-only notes (never traced):")
        print(state["staff_notes"])


def demo(name: str) -> None:
    """Run one scenario, show what happened, then print its guide."""
    session_id = f"{name}-{uuid.uuid4().hex[:8]}"
    state = run_message(SCENARIOS[name].message, session_id, name)
    print(f"scenario: {name}   session: {session_id}")
    print_run(1, state)
    print()
    print(format_guide(name))


def ask(message: str | None) -> None:
    """Send the desk your own customer messages, under the same redaction policy.

    With a message, send just that one. Without, show examples and keep asking
    until the user presses Enter on an empty line. All messages sent in one go
    share a session ID, so AIO shows them as one session.
    """
    session_id = f"ask-{uuid.uuid4().hex[:8]}"
    asked = 0

    if message:
        print_run(1, run_message(message, session_id, "ask"))
        asked = 1
    else:
        print("Write to the bookstore's customer service desk as a customer. Mention an order")
        print("number (#4471, #4488 or #4502) and anything else you like: an email, a phone")
        print("number, a card number, a new address. The desk sees it all; AIO gets the redacted copy.\n")
        print("Example messages:")
        for number, example in enumerate(EXAMPLE_MESSAGES, start=1):
            print(f"  {number}. {example}")
        while True:
            try:
                text = input("\nYour message (or an example's number; Enter to quit): ").strip()
            except EOFError:
                break
            if not text:
                break
            if text.isdigit() and 1 <= int(text) <= len(EXAMPLE_MESSAGES):
                text = EXAMPLE_MESSAGES[int(text) - 1]
            asked += 1
            print_run(asked, run_message(text, session_id, "ask"))
            # Send this message's trace now, not when you quit, so AIO shows it
            # whole while you keep typing.
            observability.flush()

    if asked:
        print(f"\nIn AIO: project {observability.project_label()} -> traces named ask, session {session_id}.")


def list_scenarios() -> None:
    print(f"{'scenario':<16} what it shows")
    for name, scenario in SCENARIOS.items():
        print(f"{name:<16} {scenario.shows}")


def main() -> None:
    parser = argparse.ArgumentParser(description="aio-redaction-langgraph: a customer service desk traced with AIO, with redaction")
    commands = parser.add_subparsers(dest="command", required=True)
    ask_command = commands.add_parser("ask", help="write your own customer messages")
    ask_command.add_argument("message", nargs="?", help="one message; leave out to pick from examples")
    demo_command = commands.add_parser("demo", help="run one scenario")
    demo_command.add_argument("scenario", choices=list(SCENARIOS))
    commands.add_parser("tour", help="run every scenario in order, with a guide after each")
    commands.add_parser("list", help="show the scenarios and what each one redacts")
    args = parser.parse_args()

    if args.command == "list":
        list_scenarios()
        return

    # Tracing, and with it the redaction policy, must start before any model client is created.
    observability.start_tracing()
    try:
        if args.command == "ask":
            ask(args.message)
        elif args.command == "demo":
            demo(args.scenario)
        else:
            names = list(SCENARIOS)
            for number, name in enumerate(names, start=1):
                print(f"\n=== Scenario {number} of {len(names)}: {name} ===\n")
                demo(name)
                if number < len(names):
                    input("\nPress Enter for the next scenario (Ctrl+C to stop)... ")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        # Send any buffered telemetry, on success and on failure alike.
        observability.flush()


if __name__ == "__main__":
    main()
