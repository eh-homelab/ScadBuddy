"""The flows repository shared by API replicas over NFS (#2503, as #2385 for models).

Each "replica" is its own `FlowHistory` on one directory, so none shares another's
in-process lock, and the ``flock`` is switched off: only the Postgres lock serialises
them, and the HEAD it records is what a replica waits to see before it counts versions.
"""

from __future__ import annotations

import fcntl
import threading
from pathlib import Path

import pytest

from scadbuddy.flows.definitions import FlowDefinitions
from scadbuddy.flows.repository import FlowHistory
from scadbuddy.flows.store import FlowStore
from scadbuddy.library.repo_lock import flows_coordinator
from scadbuddy.render.projection import JobProjection

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_git]

SCRIPT = "import asyncio\nasync def main():\n    return {n}\nasyncio.run(main())\n"


def replica(root: Path, store: FlowStore, conninfo: str) -> FlowDefinitions:
    return FlowDefinitions(
        FlowHistory(root, timeout=10.0, coordinator=flows_coordinator(conninfo)), store
    )


def test_two_replicas_register_in_turn_when_their_flock_does_not_hold(
    tmp_path: Path, jobs: JobProjection, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fcntl, "flock", lambda *_args: None)
    root = tmp_path / "data" / "flows"
    store = FlowStore(jobs.pool)
    first, second = replica(root, store, pg_conninfo), replica(root, store, pg_conninfo)
    first._start()
    second._start()
    errors: list[BaseException] = []

    def write(definitions: FlowDefinitions, offset: int) -> None:
        try:
            for n in range(5):
                definitions._write(
                    "swap", {"kind": "browser"}, None, SCRIPT.format(n=offset + n), None
                )
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=write, args=(first, 0)),
        threading.Thread(target=write, args=(second, 100)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    history = first.history
    versions = list(reversed(history.versions("swap")))
    assert [v.version for v in versions] == list(range(1, 11))
    with store._pool.connection() as conn:
        found = conn.execute(
            "SELECT version, commit FROM workflow_definitions WHERE name = 'swap' ORDER BY version"
        ).fetchall()
    assert [(r["version"], r["commit"]) for r in found] == [(v.version, v.commit) for v in versions]
