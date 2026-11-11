"""#4699 — the print watcher: each pass imports the prints ScadBuddy did not start from
Bambuddy's new archives, links a model only with confidence, and puts the rest in the
inbox. Bambuddy is mocked with respx; its archive is the recording
(`tests/bambuddy/recordings/archive-detail.json`), changed per test."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy.print_links import PrintImport, PrintLink
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.main import _auto_import_prints_logged
from scadbuddy.print_watch.component import AUTO_IMPORTS
from scadbuddy.print_watch.sweep import NO_MATCH
from tests.api.test_print_history import archive, state
from tests.api.test_send import API, configure

pytestmark = pytest.mark.requires_postgres

HASH = "c0ffee" * 10 + "abcd"
STARTED = "2026-11-10T20:00:00"


def later() -> str:
    """A naive UTC time after the watcher's floor, as Bambuddy writes it."""
    return (datetime.now(UTC) + timedelta(minutes=1)).replace(tzinfo=None).isoformat()


def a_print(archive_id: int, **fields: Any) -> dict[str, Any]:
    """An archive of printer 1 started outside ScadBuddy, made after the floor."""
    defaults: dict[str, Any] = {
        "print_name": "demo",
        "filename": "demo.gcode.3mf",
        "content_hash": HASH,
        "status": "printing",
        "created_at": later(),
        "started_at": STARTED,
        "source_3mf_path": None,
    }
    return archive(archive_id, **{**defaults, **fields})


def bambuddy(*archives: dict[str, Any]) -> respx.Route:
    respx.get(f"{API}/library/files/", params={"include_root": "false"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    return respx.get(f"{API}/archives/").mock(return_value=httpx.Response(200, json=list(archives)))


def switch_on(client: TestClient, **extra: Any) -> None:
    """The settings write records the floor (review of #6002); the archives are made
    after it."""
    configure(client, print_auto_import=True, **extra)


def run_pass(client: TestClient) -> None:
    asyncio.run(_auto_import_prints_logged(state(client)))


def auto(client: TestClient, **query: Any) -> list[dict[str, Any]]:
    response = client.get("/api/v1/print-imports/auto", params=query)
    assert response.status_code == 200, response.text
    rows: list[dict[str, Any]] = response.json()
    return rows


@respx.mock
def test_off_reads_nothing_from_bambuddy(client: TestClient, model: str) -> None:
    configure(client)
    listing = bambuddy(a_print(301))
    run_pass(client)
    assert not listing.called
    assert auto(client) == []
    settings = client.get("/api/v1/settings").json()
    assert settings["print_auto_import"] is False
    assert settings["print_auto_import_sources"] == ["archive", "archive_source", "library"]


@respx.mock
def test_a_job_named_as_one_model_is_linked_to_it(client: TestClient, model: str) -> None:
    switch_on(client)
    bambuddy(a_print(301))

    run_pass(client)

    linked = asyncio.run(state(client).print_links.linked(301))
    assert linked is not None and linked.slug == model and linked.matched_by == "import"
    (row,) = auto(client)
    assert (row["state"], row["slug"], row["matched_by"], row["source"]) == (
        "linked",
        model,
        "job_name",
        "archive",
    )
    activity = client.get("/api/v1/activity/print_auto_import:301", params={"advanced": 1})
    assert activity.status_code == 200, activity.text
    item = activity.json()["row"]
    assert (item["kind"], item["facets"]["matched_by"]) == ("print_auto_import", "job_name")


@respx.mock
def test_an_unmatched_job_lands_in_the_inbox_once(client: TestClient, model: str) -> None:
    switch_on(client)
    bambuddy(a_print(302, print_name="demo_elan", filename="demo_elan.gcode.3mf"))

    run_pass(client)
    run_pass(client)

    assert asyncio.run(state(client).print_links.linked(302)) is None
    (row,) = auto(client, state="inbox")
    assert (row["state"], row["slug"], row["reason"], row["suggested"]) == (
        "inbox",
        None,
        NO_MATCH,
        [model],
    )
    # A person imports it: it leaves the inbox.
    asyncio.run(
        state(client).print_links.record_import(
            PrintImport(archive_id=302, slug=model, source="archive", filename="demo_elan.3mf")
        )
    )
    assert auto(client, state="inbox") == []


@respx.mock
def test_a_print_scadbuddy_started_is_left_alone(client: TestClient, model: str) -> None:
    switch_on(client)
    asyncio.run(
        state(client).print_links.record(
            PrintSubject.output("a" * 32), PrintLink(archive_id=303, matched_by="queue_item")
        )
    )
    bambuddy(a_print(303))

    run_pass(client)

    assert auto(client) == []
    linked = asyncio.run(state(client).print_links.linked(303))
    assert linked is not None and linked.output_id == "a" * 32


@respx.mock
def test_a_print_no_source_had_says_why_and_reads_no_printer(
    client: TestClient, model: str
) -> None:
    switch_on(client)
    bambuddy(
        a_print(304, filename=None, extra_data={"no_3mf_available": True, "no_3mf_reason": "gone"})
    )
    printer = respx.get(f"{API}/printers/1/files")

    run_pass(client)

    (row,) = auto(client)
    assert row["state"] == "inbox" and row["source"] is None
    assert "archive: gone" in row["reason"] and "printer: not allowed" in row["reason"]
    assert row["suggested"] == [model]
    assert not printer.called


@respx.mock
def test_a_skipped_printer_is_left_alone(client: TestClient, model: str) -> None:
    switch_on(client, print_auto_import_skip_printers=[1])
    bambuddy(a_print(305))
    run_pass(client)
    assert auto(client) == []


@respx.mock
def test_an_earlier_import_of_the_file_names_the_model(client: TestClient, model: str) -> None:
    switch_on(client)
    asyncio.run(
        state(client).print_links.record_import(
            PrintImport(archive_id=200, slug=model, source="archive", filename="x", sha256=HASH)
        )
    )
    bambuddy(a_print(306, print_name="something else", filename="other.gcode.3mf"))

    run_pass(client)

    (row,) = auto(client)
    assert (row["state"], row["slug"], row["matched_by"]) == ("linked", model, "earlier_import")


@respx.mock
def test_an_earlier_import_from_a_slicer_project_names_the_model(
    client: TestClient, model: str
) -> None:
    """Review of #6002: the project's own hash is not the printed file's; the import
    keeps the archive's `content_hash`, which the next print of that file matches."""
    switch_on(client)
    asyncio.run(
        state(client).print_links.record_import(
            PrintImport(
                archive_id=201,
                slug=model,
                source="archive_source",
                filename="project.3mf",
                sha256="9" * 64,
                content_hash=HASH,
            )
        )
    )
    bambuddy(a_print(310, print_name="something else", filename="other.gcode.3mf"))

    run_pass(client)

    (row,) = auto(client)
    assert (row["slug"], row["matched_by"]) == (model, "earlier_import")


def test_switching_on_records_the_floor_and_off_forgets_it(client: TestClient) -> None:
    store = state(client).components.get(AUTO_IMPORTS)
    configure(client, print_auto_import=True)
    first = asyncio.run(store.floor(True))
    assert first is not None
    configure(client, print_auto_import=True)
    assert asyncio.run(store.floor(True)) == first  # switched on again: kept
    configure(client, print_auto_import=False)
    configure(client, print_auto_import=True)
    again = asyncio.run(store.floor(True))
    assert again is not None and again > first  # off, then on: a new floor


@respx.mock
def test_the_same_job_archived_again_is_not_imported_twice(client: TestClient, model: str) -> None:
    switch_on(client)
    listing = bambuddy(a_print(307))
    run_pass(client)
    # A re-sync: Bambuddy archives the same job (printer, file, start) under a new id.
    listing.mock(return_value=httpx.Response(200, json=[a_print(307), a_print(308)]))
    run_pass(client)
    assert [row["archive_id"] for row in auto(client)] == [307]


@respx.mock
def test_an_archive_from_before_the_switch_is_left_alone(client: TestClient, model: str) -> None:
    switch_on(client)
    before = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    bambuddy(a_print(309, created_at=before))
    run_pass(client)
    assert auto(client) == []


def test_only_a_person_changes_the_print_watcher(client: TestClient) -> None:
    agent = {"X-ScadBuddy-Agent-Author": "token:p1", "X-ScadBuddy-Agent-Author-Session": "s1"}
    for body in (
        {"print_auto_import": True},
        {"print_auto_import_sources": ["archive", "printer"]},
        {"print_auto_import_skip_printers": [1]},
    ):
        refused = client.put("/api/v1/settings", json=body, headers=agent)
        assert refused.status_code == 403, body
    assert client.get("/api/v1/settings").json()["print_auto_import"] is False
