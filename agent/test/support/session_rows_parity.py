"""Reads sessions through the API's port of the session manager's row (backend
scadbuddy/agent_data/sessions.py ``get``, answered as ``api/crud_agent_sessions.py``'s
``GET …/sessions/{id}`` answers it), for test/sessionRowsParity.pg.test.ts.
Argv: database URL, search_path; stdin: a JSON list of session ids; stdout: a JSON object of
each id's wire row."""

import json
import sys

import psycopg
from psycopg.rows import dict_row

from scadbuddy.agent_data import sessions
from scadbuddy.api.crud_agent_sessions import FullRow, OneSession


def main() -> None:
    url, search_path = sys.argv[1], sys.argv[2]
    ids: list[str] = json.load(sys.stdin)
    out = {}
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f"SET search_path TO {search_path}")
        for session_id in ids:
            row = sessions.get(conn, session_id)
            answer = OneSession(session=None if row is None else FullRow.model_validate(row))
            out[session_id] = answer.model_dump(mode="json")
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
