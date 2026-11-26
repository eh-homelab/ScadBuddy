"""The printer view's ``lan_linked`` (P14) and the command read route (#7276 Task 6, P27)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api import printers as routes
from scadbuddy.printers.lan_command_models import (
    LanCommandInput,
    LanCommandOutcome,
    LanCommandPending,
    LanCommandReply,
    LanCommandRun,
)
from tests.api.test_printers import (
    CODE,
    _probe_ok,
    create,
    registry,
    watches,  # noqa: F401  (autouse fixture of the printer routes' tests)
)
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres


#: Every command was started as one command ``x``, which has no view (P37).
NO_VIEW = LanCommandRun(
    commands=[LanCommandInput(printer_id="p", name="x", payload={}, wait_s=1, kind="write")]
)


def test_the_view_says_whether_the_printer_is_lan_linked(client: TestClient) -> None:
    """P14: ``lan_linked`` is the backend's, from the host, pin and access code together."""
    printers = registry(client)
    printers.store.kek = KEK
    printers.probe = _probe_ok([])
    pid = create(client)["id"]
    assert client.get(f"/api/v1/printers/{pid}").json()["lan_linked"] is False
    linked = client.put(
        f"/api/v1/printers/{pid}/lan", json={"host": "10.0.0.9", "access_code": CODE}
    )
    assert linked.status_code == 200, linked.text
    assert client.get(f"/api/v1/printers/{pid}").json()["lan_linked"] is True
    assert [p["lan_linked"] for p in client.get("/api/v1/printers").json()] == [True]


def test_a_command_of_another_printer_or_none_is_404(client: TestClient) -> None:
    mine = create(client)["id"]
    other = create(client, serial="01P00A000000002", name="Other")["id"]
    for command_id in (f"lan-command-{other}-x-1", f"lan-command-{mine}-x-none", "nope"):
        response = client.get(f"/api/v1/printers/{mine}/commands/{command_id}")
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "command_not_found"


def test_a_truncated_printer_id_is_not_the_owner_of_a_command(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = create(client)["id"]
    asked: list[tuple[Any, ...]] = []

    async def read(*args: Any) -> tuple[LanCommandOutcome, LanCommandRun]:
        asked.append(args[1:])
        return LanCommandOutcome(replies=[]), NO_VIEW

    monkeypatch.setattr(routes, "read_lan_commands", read)
    for printer_id in (mine[:8], "nope"):
        response = client.get(f"/api/v1/printers/{printer_id}/commands/lan-command-{mine}-x-1")
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "command_not_found"
    assert asked == []
    spelled = mine.replace("-", "").upper()
    assert (
        client.get(f"/api/v1/printers/{spelled}/commands/lan-command-{mine}-x-1").status_code == 200
    )
    assert asked == [(mine, f"lan-command-{mine}-x-1")]


def test_a_finished_command_answers_200_and_an_open_one_202(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = create(client)["id"]
    answers: list[tuple[LanCommandOutcome | LanCommandPending, LanCommandRun | None]] = [
        (LanCommandOutcome(replies=[LanCommandReply(confirmed=False)]), NO_VIEW),
        (LanCommandPending(workflow_id="lan-command-x"), None),
    ]

    async def read(*_args: Any) -> tuple[LanCommandOutcome | LanCommandPending, Any]:
        return answers.pop(0)

    monkeypatch.setattr(routes, "read_lan_commands", read)
    done = client.get(f"/api/v1/printers/{pid}/commands/c")
    assert done.status_code == 200
    assert done.json()["replies"][0]["confirmed"] is False
    running = client.get(f"/api/v1/printers/{pid}/commands/c")
    assert running.status_code == 202
    assert running.json() == {"workflow_id": "lan-command-x"}
