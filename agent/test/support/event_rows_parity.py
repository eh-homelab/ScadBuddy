"""Answers event-log reads as the API's routes answer them (backend
scadbuddy/agent_data/events.py behind api/crud_agent_events.py), for
test/eventRowsParity.pg.test.ts. Argv: database URL, search_path; stdin: one request URL a
line; stdout: each one's answer body, as a JSON line."""

import json
import re
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

import psycopg
from psycopg.rows import dict_row

from scadbuddy.agent_data import events
from scadbuddy.api.crud_agent_events import LastSeq, Logged, Read, TurnEvents, UserTurn

ROUTE = re.compile(r"/sessions/([0-9a-f-]{36})/events(/last-seq|/user-turn|/turn)?$")


def answer(conn: psycopg.Connection[Any], url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    match = ROUTE.search(parsed.path)
    if match is None:
        raise SystemExit(f"no route for {url}")
    sid, tail = match.group(1), match.group(2)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    if tail is None:
        rows = events.read(conn, sid, int(query.get("after", 0)), int(query.get("limit", 500)))
        return Read(events=[Logged.model_validate(r) for r in rows]).model_dump(mode="json")
    if tail == "/last-seq":
        return LastSeq(seq=events.last_seq(conn, sid)).model_dump(mode="json")
    if tail == "/user-turn":
        text = events.user_turn_text(conn, sid, query["turn_id"])
        return UserTurn(text=text).model_dump(mode="json")
    after = int(query["after"]) if "after" in query else None
    here, through, texts = events.turn_events(conn, sid, query["turn_id"], after)
    return TurnEvents(here=here, after=through, events=texts).model_dump(mode="json")


def main() -> None:
    url, search_path = sys.argv[1], sys.argv[2]
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f"SET search_path TO {search_path}")
        for line in sys.stdin:
            sys.stdout.write(json.dumps(answer(conn, line.strip())) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
