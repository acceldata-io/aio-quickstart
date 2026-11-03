"""Create the five AIO rules that the demo scenarios trigger, in your project.

    set -a; source .env; set +a
    python create_rules.py

Reads AIO_ENDPOINT, AIO_ACCESS_KEY, AIO_SECRET_KEY and AIO_PROJECT_ID from the
environment. A rule whose name already exists in the project is skipped, so it
is safe to run again. To change a rule, edit RULES below (or the rule in the
AIO UI).
"""

from __future__ import annotations

import os
import sys

import requests

# One rule per scenario. `parameters` is what the rule checks.
RULES = [
    {
        "ruleType": "banned_term",
        "name": "Banned term in output — aio-base-langgraph",
        "unit": "RULE_UNIT_SPAN",
        "severity": "SEVERITY_HIGH",
        "parameters": {"banned_pattern": "(?i)megabooks"},  # "MegaBooks", any letter case
    },
    {
        "ruleType": "empty_output",
        "name": "Empty output — aio-base-langgraph",
        "unit": "RULE_UNIT_SPAN",
        "severity": "SEVERITY_MEDIUM",
        "parameters": {},
    },
    {
        "ruleType": "trace_error_spans",
        "name": "Trace contains failed spans — aio-base-langgraph",
        "unit": "RULE_UNIT_TRACE",
        "severity": "SEVERITY_MEDIUM",
        "parameters": {"max_error_spans": 0},  # one failed span is enough
    },
    {
        "ruleType": "trace_duration_bound",
        "name": "Trace slower than budget — aio-base-langgraph",
        "unit": "RULE_UNIT_TRACE",
        "severity": "SEVERITY_LOW",
        "parameters": {"max_duration_ms": 5000},  # 5 seconds
    },
    {
        "ruleType": "session_turn_count",
        "name": "Conversation ran long — aio-base-langgraph",
        "unit": "RULE_UNIT_SESSION",
        "severity": "SEVERITY_LOW",
        "parameters": {"max_turns": 4},
    },
]

REQUIRED_SETTINGS = ("AIO_ENDPOINT", "AIO_ACCESS_KEY", "AIO_SECRET_KEY", "AIO_PROJECT_ID")


def main() -> int:
    missing = [name for name in REQUIRED_SETTINGS if not os.environ.get(name)]
    if missing:
        print(f"Set {', '.join(missing)} first: set -a; source .env; set +a")
        return 1

    url = os.environ["AIO_ENDPOINT"].rstrip("/") + "/api/aio/rules"
    project_id = os.environ["AIO_PROJECT_ID"].strip()
    headers = {"accessKey": os.environ["AIO_ACCESS_KEY"], "secretKey": os.environ["AIO_SECRET_KEY"]}

    # The rules already in the project, so we don't create them twice.
    response = requests.get(url, params={"projectId": project_id, "pageSize": 200}, headers=headers, timeout=10)
    if not response.ok:
        print(f"Could not list the project's rules: HTTP {response.status_code} {response.text[:200]}")
        return 1
    existing = {rule.get("name") for rule in response.json().get("data", [])}

    failed = 0
    for rule in RULES:
        if rule["name"] in existing:
            print(f"Already exists, skipped: {rule['name']}")
            continue
        body = {
            "ruleType": rule["ruleType"],
            "name": rule["name"],
            "description": "",
            "enabled": True,
            "scope": {"projectId": project_id, "environment": ""},
            "unit": rule["unit"],
            "outcome": {"scoreType": "SCORE_TYPE_BOOL", "severity": rule["severity"]},
            "sampling": {"rate": 1},
            "parameters": rule["parameters"],
        }
        response = requests.post(url, json=body, headers=headers, timeout=10)
        if response.ok:
            print(f"Created: {rule['name']}")
        else:
            failed += 1
            print(f"Failed: {rule['name']}: HTTP {response.status_code} {response.text[:200]}")

    if not failed:
        print("Done. Rules only check telemetry that arrives after they exist, so wait about a minute before running the scenarios.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
