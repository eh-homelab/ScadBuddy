"""Answers a durable turn's row writes as the API's routes answer them (backend
scadbuddy/api/crud_agent_turns.py over agent_data/turns.py, #6449), for
test/turnRowsParity.pg.test.ts. Argv: database URL, search_path; stdin: one request a line, a
JSON object {method, url, body}; stdout: each one's answer, a JSON line {status, body}."""

import asyncio
import json
import re
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import psycopg
from psycopg.rows import dict_row

from scadbuddy.api import crud_agent_turns as routes
from scadbuddy.core.problems import ApiError

SESSION = re.compile(r"/sessions/([0-9a-f-]{36})/turn(/[a-z-]+)?$")

POSTS = {
    "/send-claim": (routes.send_claim, routes.SendClaim),
    "/queueable": (routes.queueable, routes.Queueable),
    "/give-back": (routes.give_back, routes.GiveBack),
    "/placement": (routes.placement, routes.Placement),
    "/spend": (routes.spend, routes.Spend),
    "/finish": (routes.finish, routes.Finish),
    "/begin": (routes.begin, routes.Begin),
    "/queue": (routes.queue_changed, routes.QueueWrite),
    "/end-lost": (routes.end_lost, routes.EndLost),
}


async def answer(state: Any, method: str, url: str, body: Any) -> Any:
    parsed = urlparse(url)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    if parsed.path.endswith("/turns/stale"):
        return await routes.stale(
            state, query.get("after", ""), int(query["grace_ms"]), int(query["limit"])
        )
    match = SESSION.search(parsed.path)
    if match is None:
        raise SystemExit(f"no route for {url}")
    sid, tail = match.group(1), match.group(2)
    if method == "GET" and tail is None:
        return await routes.turn_state(sid, state, int(query.get("grace_ms", 0)))
    if method == "GET" and tail == "/queue":
        return await routes.last_queue(sid, state)
    if tail == "/interrupt":
        return await routes.interrupt(sid, state)
    route, model = POSTS[tail or ""]
    return await route(sid, model.model_validate(body), state)


def main() -> None:
    url, search_path = sys.argv[1], sys.argv[2]
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f"SET search_path TO {search_path}")

        @contextmanager
        def connection() -> Any:
            yield conn

        pool = SimpleNamespace(connection=connection)
        state = SimpleNamespace(settings_store=SimpleNamespace(pool=pool))
        for line in sys.stdin:
            request = json.loads(line)
            try:
                result = asyncio.run(
                    answer(state, request["method"], request["url"], request["body"])
                )
                out = {"status": 200, "body": result.model_dump(mode="json")}
            except ApiError as err:
                out = {"status": err.status, "body": {"detail": err.detail, **err.extensions}}
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


main()
