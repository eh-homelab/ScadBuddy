"""Runs a scenario of gate store calls through the API's port (backend
scadbuddy/agent_data/gate.py, #5299 step 10), for test/gateBackend.pg.test.ts's parity check.
Argv: database URL, search_path; stdin: the scenario, a JSON list of {op, args} with camelCase
args (the TypeScript store's)."""

import json
import re
import sys
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from scadbuddy.agent_data import gate
from scadbuddy.agent_data.gate_canonical import response_hash


def snake(name: str) -> str:
    return re.sub(r"([A-Z])", lambda m: "_" + m.group(1).lower(), name)


def when(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> None:
    url, search_path = sys.argv[1], sys.argv[2]
    steps: list[dict[str, Any]] = json.load(sys.stdin)
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f"SET search_path TO {search_path}")
        for step in steps:
            args = {snake(k): v for k, v in step["args"].items()}
            op = step["op"]
            if op == "open":
                args["expires_at"] = when(args["expires_at"])
                args["hop_until"] = when(args.get("hop_until"))
                gate.open_input(conn, gate.OpenInput(**args))
            elif op == "resolve":
                raw = args.pop("raw_response", None)
                gate.resolve_input(
                    conn, **args, digest=None if raw is None else response_hash(raw)
                )
            elif op == "deny":
                gate.deny_input(conn, **args)
            elif op == "decide":
                gate.decide_input(conn, **args, live_run=None, check_live=False)
            else:
                raise SystemExit(f"unknown op {op}")


main()
