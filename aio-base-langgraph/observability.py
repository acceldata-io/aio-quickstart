"""Everything to do with Acceldata AI Observability (AIO) lives in this file.

No other file imports the tracer. What it does:

1. `start_tracing()` turns tracing on once, at start-up, if the AIO settings are
   in the environment. Without them the app still runs, untraced. It also looks
   up the project's name, so messages can say where the traces went, gives
   `fixture-model` a price in AIO so model calls show a cost.
2. `traced_run()` wraps one graph run, so the run becomes one trace, and tags
   it with a session ID so runs can be grouped into a session.
3. `record_node()` marks a graph node so its input and output are saved on its span.
4. `flush()` sends any spans still waiting in memory before the app exits.

Model calls, tool calls and graph nodes are traced automatically once
`start_tracing()` has run, including the system prompts and tool descriptions.
"""

from __future__ import annotations

import atexit
import logging
import os
from contextlib import contextmanager

import acceldata_aio_tracer as aio
import requests
from opentelemetry import _logs, metrics, trace

from models import FIXTURE_MODEL_NAME

SERVICE_NAME = "aio-base-langgraph"

# Set to True to see the tracer's own debug logs, for example when no traces
# show up in AIO.
DEBUG_LOGS = False

# All five are read from the environment. AIO_PROJECT_ID is the ID of a project
# you created in AIO; the SDK sends it with every export as is.
REQUIRED_SETTINGS = ("AIO_ENDPOINT", "AIO_TENANT_ID", "AIO_PROJECT_ID", "AIO_ACCESS_KEY", "AIO_SECRET_KEY")

# Cost. AIO works out each model call's cost from its token counts and the price
# set for its model in your tenant. A model needs a price before AIO can show a
# cost, so start_tracing() gives fixture-model this demo price the first time it
# runs in a tenant. A price that already exists is left alone. Prices are US
# dollars per million tokens.
FIXTURE_MODEL_PRICE = {
    "inputUsdPerM": 3,
    "outputUsdPerM": 15,
    "cacheReadUsdPerM": 0,
    "cacheWriteUsdPerM": 0,
    "displayName": "Fixture model (demo)",
    "provider": "demo",
}

# The project's name in AIO, filled in by start_tracing(). Only used in printed
# messages: the traces themselves carry the project ID, never the name.
_project_name = ""


def start_tracing() -> bool:
    """Turn on AIO tracing. Call this before any model client is created.

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
    )
    if enabled:
        # A safety net: also flush if the app exits without calling flush().
        atexit.register(flush)
        _look_up_project_name()
        _set_fixture_model_price()
        print(f"AIO tracing is on: traces go to project {project_label()}.")
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
            headers=_aio_headers(),
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


def _set_fixture_model_price() -> None:
    """Give fixture-model a price in this tenant, unless it already has one.

    If it can't be set, the demo still runs; model spans just show no cost.
    """
    url = os.environ["AIO_ENDPOINT"].rstrip("/") + "/api/aio/model-prices"
    model = {"model": FIXTURE_MODEL_NAME}
    try:
        if requests.get(url, params=model, headers=_aio_headers(), timeout=10).status_code == 200:
            return  # already priced
        response = requests.put(url, params=model, json=FIXTURE_MODEL_PRICE, headers=_aio_headers(), timeout=10)
        response.raise_for_status()
        print(
            f"Set a demo price for {FIXTURE_MODEL_NAME} in AIO: "
            f"${FIXTURE_MODEL_PRICE['inputUsdPerM']} input / ${FIXTURE_MODEL_PRICE['outputUsdPerM']} output per million tokens."
        )
    except Exception as error:  # noqa: BLE001 - a missing price must never stop the demo
        print(f"Could not set a price for {FIXTURE_MODEL_NAME} ({error}); model spans will show no cost (README → Change the cost).")


def _aio_headers() -> dict[str, str]:
    """The access key pair, which AIO's API expects as request headers."""
    return {"accessKey": os.environ["AIO_ACCESS_KEY"], "secretKey": os.environ["AIO_SECRET_KEY"]}


@contextmanager
def traced_run(scenario: str, session_id: str, question: str):
    """Make one graph run into one trace, named after the scenario.

    Runs that share a `session_id` are grouped into one session in AIO.
    An exception inside is recorded on the trace and then raised again.
    """
    with aio.root_span(conversation_id=session_id, name=scenario, input_preview=question):
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
