"""Everything to do with Acceldata AI Observability (AIO) lives in this file.

No other file imports the tracer. What it does:

1. `REDACTION` is the app's one redaction policy: what must never reach AIO.
   It is set once, at start-up, and applies to every run, whatever is asked.
2. `start_tracing()` turns tracing on once, at start-up, with that policy, if
   the AIO settings are in the environment. Without them the app still runs,
   untraced. It also looks up the project's name, so messages can say where
   the traces went.
3. `hide_arguments()` marks a tool so its arguments stay out of AIO, in the
   tool's own code rather than in the policy.
4. `private_model_inputs()` hides everything sent to a model inside one block
   of code, whatever the policy says.
5. `traced_run()` wraps one graph run, so the run becomes one trace, and tags
   it with a session ID so runs can be grouped into a session.
6. `record_node()` marks a graph node so its input and output are saved on its span.
7. `flush()` sends any spans still waiting in memory before the app exits.

Redaction happens as the telemetry leaves the app. The agents, the models and
your terminal always see the real values; only what AIO receives changes.
"""

from __future__ import annotations

import atexit
import logging
import os
from contextlib import contextmanager

import acceldata_aio_tracer as aio
import requests
from opentelemetry import _logs, metrics, trace

SERVICE_NAME = "aio-redaction-langgraph"

# Set to True to see the tracer's own debug logs, for example when no traces
# show up in AIO.
DEBUG_LOGS = False

# All five are read from the environment. AIO_PROJECT_ID is the ID of a project
# you created in AIO; the SDK sends it with every export as is.
REQUIRED_SETTINGS = ("AIO_ENDPOINT", "AIO_TENANT_ID", "AIO_PROJECT_ID", "AIO_ACCESS_KEY", "AIO_SECRET_KEY")

# --- The redaction policy ---------------------------------------------------------

# Patterns: personal data that can turn up anywhere in free text, in any
# message or tool call. Each match becomes "__REDACTED__"; the rest of the
# text stays.
EMAIL_ADDRESS = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
PHONE_NUMBER = r"\b\d{3}-\d{3}-\d{4}\b"
CARD_NUMBER = r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"
# A house number, a street name, then an optional flat and town: "48 Harbour Road, Apt 3, Portland".
STREET_ADDRESS = r"\b\d{1,5}(?: [A-Z][a-z]+)+ (?:Road|Street|Lane|Avenue|Court)(?:, (?:Apt|Unit) \d+)?(?:, [A-Z][a-z]+)?"

STORE_DOMAIN = "@bookstore.example"


def is_store_address(match: str) -> bool:
    """Keep the store's own email addresses: they are public, and useful in a trace.

    The SDK calls this with each pattern match. Returning True keeps the match.
    """
    return match.endswith(STORE_DOMAIN)


REDACTION = aio.redaction_policy(
    patterns=[EMAIL_ADDRESS, PHONE_NUMBER, CARD_NUMBER, STREET_ADDRESS],
    allow=is_store_address,
    # The Order Desk's system prompt holds the store's internal refund rules,
    # and the tool definitions describe internal systems. Only a fingerprint
    # of each is sent, never the text.
    hide_system_instructions=True,
    hide_tool_definitions=True,
    # lookup_customer is called with the customer's email and phone, and
    # returns their whole record. Hide both, whole, wherever they appear.
    redact_tool_arguments=["lookup_customer"],
    redact_tool_results=["lookup_customer"],
    # The staff handbook's passages are internal. Patterns only scan messages
    # and tool calls, so the retrieval span's output is named here, and hidden
    # whole. Its input, the search query, stays readable.
    redact_attributes=["gen_ai.retrieval.documents"],
    # The staff-only notes step is never traced, and nor is anything under it.
    suppress_spans=["internal_notes"],
)

# The project's name in AIO, filled in by start_tracing(). Only used in printed
# messages: the traces themselves carry the project ID, never the name.
_project_name = ""


def start_tracing() -> bool:
    """Turn on AIO tracing, with the redaction policy. Call this before any model client is created.

    Returns True when tracing is on. Never raises: a missing or wrong setting
    only means the run is not traced.
    """
    if DEBUG_LOGS:
        logging.basicConfig()
        logging.getLogger("acceldata_aio_tracer").setLevel(logging.DEBUG)

    missing = [name for name in REQUIRED_SETTINGS if not os.environ.get(name)]
    if missing:
        print(f"AIO tracing is off: set {', '.join(missing)} to send traces (see .env.example).")
        return False

    enabled = aio.init(
        service=SERVICE_NAME,
        tenant_id=os.environ["AIO_TENANT_ID"],
        project_id=os.environ["AIO_PROJECT_ID"],
        endpoint=os.environ["AIO_ENDPOINT"],
        redaction=REDACTION,
    )
    if enabled:
        # A safety net: also flush if the app exits without calling flush().
        atexit.register(flush)
        _look_up_project_name()
        print(f"AIO tracing is on, with redaction: traces go to project {project_label()}.")
    else:
        print("AIO tracing could not start; the SDK logged the reason above. The demo runs untraced.")
    return enabled


def project_label() -> str:
    """How to refer to the AIO project in printed messages: its name, else its ID."""
    project_id = os.environ.get("AIO_PROJECT_ID", "")
    if _project_name:
        return _project_name
    return f"with ID {project_id}" if project_id else "(tracing is off)"


def _look_up_project_name() -> None:
    """Find the name of the project whose ID is AIO_PROJECT_ID, to show to the user.

    This is a read-only request. If it fails, messages show the ID instead.
    If AIO lists projects but not this ID, the traces will not show up, so say so.
    """
    global _project_name
    project_id = os.environ["AIO_PROJECT_ID"].strip()
    try:
        response = requests.get(
            os.environ["AIO_ENDPOINT"].rstrip("/") + "/api/projects",
            headers={"accessKey": os.environ["AIO_ACCESS_KEY"], "secretKey": os.environ["AIO_SECRET_KEY"]},
            timeout=10,
        )
        response.raise_for_status()
        projects = response.json().get("projects") or []
    except Exception as error:  # noqa: BLE001 - a failed lookup must never stop the demo
        print(f"Could not look up the AIO project's name ({error.__class__.__name__}); showing its ID instead.")
        return

    for project in projects:
        if project.get("id") == project_id:
            _project_name = project.get("name") or ""
            return
    print(
        f"Warning: AIO has no project with ID {project_id} that these keys can see, so traces will not show up.\n"
        "  Check that AIO_PROJECT_ID is the project's ID (not its name), copied from the project you created."
    )


# --- Redaction in the app's own code ----------------------------------------------


def hide_arguments(tool):
    """Decorator: keep this tool's arguments out of AIO.

    The same as naming the tool in the policy's redact_tool_arguments, but
    written on the tool itself, where whoever edits it will see it. Arguments
    only: to hide what a tool returns, name it in redact_tool_results.
    """
    return aio.redact_arguments(tool)


@contextmanager
def private_model_inputs():
    """Hide everything sent to a model inside this block: system prompt and messages.

    Works with or without a policy. The model's reply, and every span that is
    not a model call, are left as they are.
    """
    with aio.redact_model_inputs():
        yield


# --- Traces and sessions ---------------------------------------------------------------


@contextmanager
def traced_run(scenario: str, session_id: str):
    """Make one graph run into one trace, named after the scenario.

    Runs that share a `session_id` are grouped into one session in AIO.
    An exception inside is recorded on the trace and then raised again.

    No input_preview is passed. It would be the customer's raw message, in an
    attribute patterns don't scan. Without it, AIO shows the first agent's
    input as the trace's input, and patterns already apply there.
    """
    with aio.root_span(conversation_id=session_id, name=scenario):
        yield


def record_node(input=None, output=None):  # noqa: A002 - same names as the SDK uses
    """Decorator: save a graph node's input and output on its span.

    `input(state)` and `output(state, result)` pick what to save. Leave them
    out to save the whole state and the whole result.
    """
    return aio.langgraph_node(input=input, output=output)


def flush() -> None:
    """Send everything still buffered to AIO. Safe to call more than once."""
    for provider in (trace.get_tracer_provider(), _logs.get_logger_provider(), metrics.get_meter_provider()):
        force_flush = getattr(provider, "force_flush", None)
        if force_flush:
            try:
                force_flush()
            except Exception:  # noqa: BLE001 - telemetry must never crash the app
                logging.getLogger(__name__).debug("flush failed", exc_info=True)
