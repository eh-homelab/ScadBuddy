"""Runs a scenario of gate store calls through agent-durable's store.py, for
test/gateStore.pg.test.ts's parity check. Argv: database URL, search_path; stdin: the
scenario, a JSON list of {op, args} with camelCase args (the TypeScript port's)."""

import asyncio
import json
import re
import sys
from typing import Any

import psycopg

from scadbuddy_durable.gate import store


def snake(name: str) -> str:
    return re.sub(r"([A-Z])", lambda m: "_" + m.group(1).lower(), name)


async def main() -> None:
    url, search_path = sys.argv[1], sys.argv[2]
    steps: list[dict[str, Any]] = json.load(sys.stdin)
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    await conn.execute(f"SET search_path TO {search_path}")
    try:
        for step in steps:
            args = {snake(k): v for k, v in step["args"].items()}
            if step["op"] == "open":
                await store.open_input(conn, store.OpenInput(**args))
            elif step["op"] == "resolve":
                await store.resolve_input(conn, store.ResolveInput(**args))
            elif step["op"] == "deny":
                await store.deny_input(conn, store.DenyInput(**args))
            else:
                raise SystemExit(f"unknown op {step['op']}")
    finally:
        await conn.close()


asyncio.run(main())
