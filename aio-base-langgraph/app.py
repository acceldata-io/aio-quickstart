"""Command line for the bookstore demo.

    python app.py ask                 ask your own questions (shows example prompts)
    python app.py ask "<question>"    ask one question
    python app.py demo <scenario>     run one scenario, then print its guide
    python app.py list                show the scenarios and the AIO rule each one targets
    python app.py tour                run every scenario in order, with a guide after each
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass

import observability
from graph import FAULTS, RETRY_BACKOFF_SECONDS, SLOW_SUPPLIER_SECONDS, Faults, build_graph, load_store, make_models

REORDER_QUESTION = "Which books are running low, and what should I reorder?"

# Shown by `python app.py ask`. Type the number to use one.
EXAMPLE_QUESTIONS = [
    REORDER_QUESTION,
    "Is The Midnight Library back in stock?",
    "How is the mystery shelf doing?",
    "What's running low on the literary shelf?",
    "Should I reorder To Kill a Mockingbird?",
    "Do we need more copies of Dune?",
    "How is the fantasy shelf doing?",
]


@dataclass(frozen=True)
class Scenario:
    questions: list[str]  # one graph run per question, all in the same session
    rule: str             # the AIO rule this scenario is meant to trigger
    level: str            # span, trace or session


SCENARIOS: dict[str, Scenario] = {
    "happy-path": Scenario([REORDER_QUESTION], rule="none (clean baseline)", level="-"),
    "banned-term": Scenario(["Should I reorder The Midnight Library?"], rule="banned_term", level="span"),
    "empty-output": Scenario([REORDER_QUESTION], rule="empty_output", level="span"),
    "agent-retry": Scenario([REORDER_QUESTION], rule="trace_error_spans", level="trace"),
    "slow-supplier": Scenario([REORDER_QUESTION], rule="trace_duration_bound", level="trace"),
    # Five runs, one session. Run 1 is healthy (skip branch);
    # runs 2-5 ask the same thing in different words.
    "repeat-questions": Scenario(
        [
            "How is the sci-fi shelf doing?",
            "Is The Midnight Library back in stock?",
            "Have we restocked Midnight Library yet?",
            "Any update on The Midnight Library?",
            "When will The Midnight Library be back on the shelf?",
        ],
        rule="session_turn_count",
        level="session",
    ),
}


# --- Scenario guides ---------------------------------------------------------------
# The one place the guide text lives. `demo` and `tour` print it, and TOUR.md
# has a section for every entry. Keep the two in step when you edit either.


@dataclass(frozen=True)
class Guide:
    what_happened: str
    check_in_aio: list[str]
    debug_it: list[str]
    not_seeing_it: list[str]


NO_TRACE = "No trace → check the AIO_* settings, then turn on DEBUG_LOGS in observability.py and run again (README → Troubleshooting)."
NEW_RULE = "No rule flag → run python create_rules.py, or check the rule is enabled for project {project}. Rules only check telemetry that arrives after they exist, so wait a minute after creating one, then run again."

GUIDES: dict[str, Guide] = {
    "happy-path": Guide(
        what_happened=(
            "The analyst found 3 low-stock books and handed them to the coordinator through `findings`.\n"
            "The supplier answered first time and the recommendation passed validation.\n"
            "Only the model is simulated (fixture-model)."
        ),
        check_in_aio=[
            "Open project {project} → latest trace named happy-path.",
            "Expand invoke_workflow bookstore: inventory_analyst → supplier_coordinator → validate → report.",
            "Open invoke_agent supplier_coordinator: its input is the analyst's findings. That is the handoff.",
            "Open chat fixture-model under supplier_coordinator: the token counts and the cost of the model call.",
            "Go to the Traces section and the Spans section: none of the rules from create_rules.py should flag this trace or any of its spans.",
        ],
        debug_it=[
            "Open a chat span: the system prompt is the agent's instructions, the user message is the facts as JSON.",
            "Open execute_tool check_supplier: its arguments (attempt=1) and its result.",
            "Keep this trace as the baseline to compare every other scenario against.",
            "AIO code: observability.py. start_tracing() calls aio.init(), traced_run() opens aio.root_span(), record_node() wraps aio.langgraph_node.",
        ],
        not_seeing_it=[NO_TRACE, "A rule flagged it → that rule is set too strictly; happy-path should be clean."],
    ),
    "banned-term": Guide(
        what_happened=(
            'The coordinator\'s reply (a fixture, simulated) recommends a competitor, "MegaBooks".\n'
            "The app's own checks don't look for that, so the run ends normally (outcome: ordered)."
        ),
        check_in_aio=[
            "Open project {project} → latest trace named banned-term.",
            "Expand supplier_coordinator → chat fixture-model: the output mentions MegaBooks.",
            "Go to the Spans section and find this trace's chat span under supplier_coordinator. If banned_term is configured with the term MegaBooks, check whether the rule flagged that span.",
        ],
        debug_it=[
            "Read the span's output and find the banned term.",
            "Compare it with the system prompt on the same span, which says not to mention other bookstores.",
            "Ask: would the app have caught this on its own? No. validate passed it.",
        ],
        not_seeing_it=[NO_TRACE, "No rule flag → check the term is exactly MegaBooks. " + NEW_RULE.removeprefix("No rule flag → ")],
    ),
    "empty-output": Guide(
        what_happened=(
            "The coordinator's model call succeeded but returned only whitespace (a fixture, simulated).\n"
            "validate saw the empty recommendation and sent the run to failure_report (outcome: failed)."
        ),
        check_in_aio=[
            "Open project {project} → latest trace named empty-output.",
            "Expand supplier_coordinator → chat fixture-model: the output is blank, yet the span is not an error.",
            "The run ends at failure_report instead of report.",
            "Go to the Spans section and find this trace's chat span under supplier_coordinator (the blank one). If empty_output is configured, check whether the rule flagged that span.",
        ],
        debug_it=[
            "The model span looks healthy: OK status, finish reason stop. Only the content is missing.",
            "Open validate: its input shows the empty recommendation. failure_report's input shows the error.",
            "Ask: why would a model return nothing? With a real model, check its token limit and content filters.",
        ],
        not_seeing_it=[NO_TRACE, NEW_RULE],
    ),
    "agent-retry": Guide(
        what_happened=(
            "Supplier lookup failed on attempt 1 (simulated timeout), then succeeded\n"
            f"on attempt 2 after a {RETRY_BACKOFF_SECONDS:g}s backoff. The user got a normal recommendation."
        ),
        check_in_aio=[
            "Open project {project} → latest trace named agent-retry.",
            "Expand supplier_coordinator: there are two check_supplier spans, and the first one is marked as an error.",
            "Go to the Traces section and find this agent-retry trace. If trace_error_spans is configured, check whether the rule flagged it.",
        ],
        debug_it=[
            "Open the failed span: read the error message and the attempt number in its arguments.",
            f"Compare its end time with the second span's start: the ~{RETRY_BACKOFF_SECONDS:g}s gap is the backoff.",
            "Ask: is the retry hiding a real upstream problem? The final answer looks fine, which is exactly why this rule exists.",
        ],
        not_seeing_it=[NO_TRACE, NEW_RULE],
    ),
    "slow-supplier": Guide(
        what_happened=(
            f"The supplier tool waited {SLOW_SUPPLIER_SECONDS:g}s before answering (simulated).\n"
            "The run succeeded, but took longer than a 5s budget."
        ),
        check_in_aio=[
            "Open project {project} → latest trace named slow-supplier.",
            f"Expand supplier_coordinator: execute_tool check_supplier takes about {SLOW_SUPPLIER_SECONDS:g}s, almost the whole trace.",
            "Go to the Traces section and find this slow-supplier trace. If trace_duration_bound is configured (5s budget), check whether the rule flagged it.",
        ],
        debug_it=[
            "Compare span durations: the tool span takes seconds, the model spans take milliseconds.",
            "Compare with the happy-path trace: the same spans, only the tool span is slower.",
            "Ask: is the supplier slow, or are we calling it badly? Should this call have a timeout?",
        ],
        not_seeing_it=[
            NO_TRACE,
            "No rule flag → the budget must be below the run time. If you lowered SLOW_SUPPLIER_SECONDS in graph.py, lower the budget too.",
            NEW_RULE,
        ],
    ),
    "repeat-questions": Guide(
        what_happened=(
            "Five runs under one session ID. Run 1 asked about the sci-fi shelf, which is healthy,\n"
            "so the supplier step was skipped. Runs 2-5 asked about The Midnight Library in different\n"
            "words. Every run succeeded."
        ),
        check_in_aio=[
            "Open project {project} → the session with the ID printed above.",
            "It holds five traces named repeat-questions. Run 1 has no supplier_coordinator span (the skip branch).",
            "Go to the Sessions section and find this session. If session_turn_count is configured (maximum 4 turns), check whether the rule flagged it.",
        ],
        debug_it=[
            "Read the inputs of runs 2-5 side by side: the same question in different words.",
            "Every trace is green. The problem only shows when you look at the whole session.",
            "Ask: why does the manager keep asking? The answer says what to order, not when the book will be back.",
            "AIO code: observability.py → traced_run(). All five runs pass the same session ID to aio.root_span() as conversation_id.",
        ],
        not_seeing_it=[
            NO_TRACE,
            "No session → the runs must share one session ID; it is printed at the top of the output.",
            "No rule flag → the maximum must be below 5 turns. " + NEW_RULE.removeprefix("No rule flag → "),
        ],
    ),
}


def format_guide(name: str) -> str:
    """Turn one scenario's guide into the text block the CLI prints."""
    guide = GUIDES[name]
    width = 72
    lines = [f"── {name} ".ljust(width, "─"), "WHAT HAPPENED"]
    lines += [f"  {line}" for line in guide.what_happened.splitlines()]
    lines.append("\nCHECK IN AIO")
    lines += [f"  {number}. {step}" for number, step in enumerate(guide.check_in_aio, start=1)]
    lines.append("\nDEBUG IT")
    lines += [f"  • {step}" for step in guide.debug_it]
    lines.append("\nNOT SEEING IT?")
    lines += [f"  {hint}" for hint in guide.not_seeing_it]
    lines.append("─" * width)
    # The guides say "{project}" wherever the AIO project's name goes.
    return "\n".join(lines).replace("{project}", observability.project_label())


# --- Running questions and scenarios -------------------------------------------------


def run_question(question: str, session_id: str, trace_name: str, faults: Faults = Faults()) -> dict:
    """Run the graph once for one question, as one trace. Returns the final state."""
    # The graph is built inside the trace so its set-up span lands in the
    # same trace, not in a separate one.
    with observability.traced_run(trace_name, session_id, question):
        analyst_model, coordinator_model = make_models()
        graph = build_graph(analyst_model, coordinator_model, faults)
        return graph.invoke({"question": question, "errors": []})


def run_scenario(name: str) -> tuple[str, list[dict]]:
    """Run every question of a scenario under one session ID.

    Returns the session ID and the final state of each run.
    """
    session_id = f"{name}-{uuid.uuid4().hex[:8]}"
    results = [run_question(question, session_id, name, FAULTS[name]) for question in SCENARIOS[name].questions]
    return session_id, results


def print_run(number: int, state: dict) -> None:
    """Show the manager what one run produced."""
    print(f"\n[run {number}] Q: {state['question']}")
    print(state["summary"])
    if state.get("recommendation", "").strip():
        print(state["recommendation"])
    for error in state.get("errors", []):
        print(f"  ! {error}")
    print(f"  outcome: {state['outcome']}")


def demo(name: str) -> None:
    """Run one scenario, show what happened, then print its guide."""
    session_id, results = run_scenario(name)
    print(f"scenario: {name}   session: {session_id}")
    for number, state in enumerate(results, start=1):
        print_run(number, state)
    print()
    print(format_guide(name))


def ask(question: str | None) -> None:
    """Answer the user's own questions, with no simulated problems.

    With a question, answer just that one. Without, show example prompts and
    keep asking until the user presses Enter on an empty line. All questions
    asked in one go share a session ID, so AIO shows them as one session.
    """
    session_id = f"ask-{uuid.uuid4().hex[:8]}"
    asked = 0

    if question:
        print_run(1, run_question(question, session_id, "ask"))
        asked = 1
    else:
        shelves = sorted({book["shelf"] for book in load_store()["books"].values()})
        print("Ask the bookstore's agents about stock. Name a book (part of the title is enough,")
        print(f'like "Harry Potter") or a shelf: {", ".join(shelves)}.')
        print("Anything else is read as a question about the whole store.\n")
        print("Example questions:")
        for number, example in enumerate(EXAMPLE_QUESTIONS, start=1):
            print(f"  {number}. {example}")
        while True:
            try:
                text = input("\nYour question (or an example's number; Enter to quit): ").strip()
            except EOFError:
                break
            if not text:
                break
            if text.isdigit() and 1 <= int(text) <= len(EXAMPLE_QUESTIONS):
                text = EXAMPLE_QUESTIONS[int(text) - 1]
            asked += 1
            print_run(asked, run_question(text, session_id, "ask"))

    if asked:
        print(f"\nIn AIO: project {observability.project_label()} → traces named ask, session {session_id}.")


def list_scenarios() -> None:
    print(f"{'scenario':<18} {'level':<8} rule it should trigger")
    for name, scenario in SCENARIOS.items():
        print(f"{name:<18} {scenario.level:<8} {scenario.rule}")


def main() -> None:
    parser = argparse.ArgumentParser(description="aio-base-langgraph: a LangGraph bookstore traced with AIO")
    commands = parser.add_subparsers(dest="command", required=True)
    ask_command = commands.add_parser("ask", help="ask your own questions")
    ask_command.add_argument("question", nargs="?", help="one question; leave out to pick from examples")
    demo_command = commands.add_parser("demo", help="run one scenario")
    demo_command.add_argument("scenario", choices=list(SCENARIOS))
    commands.add_parser("tour", help="run every scenario in order, with a guide after each")
    commands.add_parser("list", help="show scenarios and the rule each one targets")
    args = parser.parse_args()

    if args.command == "list":
        list_scenarios()
        return

    # Tracing must start before any model client is created.
    observability.start_tracing()
    try:
        if args.command == "ask":
            ask(args.question)
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
