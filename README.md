# AIO Quickstart

Small command-line LangGraph apps, traced with the
[`acceldata-aio-tracer`](https://pypi.org/project/acceldata-aio-tracer/) SDK, to
help you try AIO. They use a stand-in model, so you need **no LLM API key**.

| App | Start here to see |
|---|---|
| [`aio-base-langgraph`](aio-base-langgraph/) | Traces, spans and sessions, token cost, and rules |
| [`aio-redaction-langgraph`](aio-redaction-langgraph/) | How to keep personal and sensitive data out of AIO |

New to AIO? Start with `aio-base-langgraph`.

## Run an app

You need Python 3.10 or newer, and a project in AIO.

```bash
cd aio-base-langgraph            # or any folder/project you want to try
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env             # fill in your AIO settings
set -a; source .env; set +a
python app.py tour
```

Each app's README covers its settings, scenarios and troubleshooting
