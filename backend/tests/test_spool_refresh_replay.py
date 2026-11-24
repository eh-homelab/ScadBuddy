"""``SpoolRefresh`` replays the histories it wrote (#3818 tier A, plan 2026-10-18 Task 6b).

On the versioned ``library`` worker it is AUTO_UPGRADE (`VersionedSpoolRefresh`), so a
run left open across a deploy replays on the new code. A change to the commands
``SpoolRefresh.run`` issues goes behind ``workflow.patched``; this test is what says so.

``refreshed`` was recorded with two providers that answered, ``failed`` with a provider
whose activity raised and a trays step that raised (``SCADBUDDY_RECORD_HISTORIES=1`` runs
the recording below), both before the kept local presets' step (#4645); ``held`` with
that step, behind its patch; ``bb_pull`` with Bambuddy's spools pulled after it, behind
``filament-bb-pull`` (#6261); ``providers_gone`` on the code behind
``spool-providers-gone`` (#6262 Task 12), which runs only the kept presets and the pull;
``capture`` with the capture backstop after them, behind ``filament-capture-backstop``
(#6263 Task 8); ``rfid`` with the tag push after the backstop, behind ``filament-rfid``
(#6264 Task 4); ``held_gone`` without the kept presets' step, behind
``spool-refresh-held-gone`` (#7244). Record only the new one (``-k held_gone``).
Never re-record them: a later change adds a history recorded on the changed code beside
them.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowHistory
from temporalio.worker import Replayer, Worker

from scadbuddy.workflows.spool_refresh import (
    CAPTURE_ACTIVITY,
    FILAMENT_PULL_ACTIVITY,
    FILAMENT_RFID_ACTIVITY,
    FILAMENT_RFID_PATCH,
    HELD_ACTIVITY,
    HELD_GONE_PATCH,
    LIST_ACTIVITY,
    PROVIDER_ACTIVITY,
    SEEN_ACTIVITY,
    SpoolRefresh,
)
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "spool_refresh_histories"


@pytest.mark.parametrize(
    "name",
    ["refreshed", "failed", "held", "bb_pull", "providers_gone", "capture", "rfid", "held_gone"],
)
async def test_spool_refresh_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"spool-refresh-{name}", (HISTORIES / f"{name}.json").read_text()
    )
    await Replayer(workflows=[SpoolRefresh]).replay_workflow(history)


def test_the_capture_history_ends_with_the_backstop() -> None:
    history = WorkflowHistory.from_json(
        "spool-refresh-capture", (HISTORIES / "capture.json").read_text()
    )
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled == [HELD_ACTIVITY, FILAMENT_PULL_ACTIVITY, CAPTURE_ACTIVITY]


def test_the_rfid_history_ends_with_the_tag_push_and_carries_all_three_markers() -> None:
    history = WorkflowHistory.from_json("spool-refresh-rfid", (HISTORIES / "rfid.json").read_text())
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled == [
        HELD_ACTIVITY,
        FILAMENT_PULL_ACTIVITY,
        CAPTURE_ACTIVITY,
        FILAMENT_RFID_ACTIVITY,
    ]
    markers = {
        json.loads(event.marker_recorded_event_attributes.details["patch-data"].payloads[0].data)[
            "id"
        ]
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
        and event.marker_recorded_event_attributes.marker_name == "core_patch"
    }
    assert markers == {"spool-providers-gone", "filament-capture-backstop", FILAMENT_RFID_PATCH}


def test_the_patched_history_runs_only_the_presets_and_the_pull() -> None:
    """Behind ``spool-providers-gone`` no provider is listed or refreshed and no tray is
    recorded: the retired overlay's activities are only for runs opened before it."""
    history = WorkflowHistory.from_json(
        "spool-refresh-providers_gone", (HISTORIES / "providers_gone.json").read_text()
    )
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled == [HELD_ACTIVITY, FILAMENT_PULL_ACTIVITY]


def test_the_held_gone_history_keeps_no_copy_of_bambuddys_presets() -> None:
    """Behind ``spool-refresh-held-gone`` (#7244) a run schedules no kept presets' step;
    the pull, which imports the presets by direction, still runs."""
    history = WorkflowHistory.from_json(
        "spool-refresh-held_gone", (HISTORIES / "held_gone.json").read_text()
    )
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled == [FILAMENT_PULL_ACTIVITY, CAPTURE_ACTIVITY, FILAMENT_RFID_ACTIVITY]
    markers = {
        json.loads(event.marker_recorded_event_attributes.details["patch-data"].payloads[0].data)[
            "id"
        ]
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
        and event.marker_recorded_event_attributes.marker_name == "core_patch"
    }
    assert HELD_GONE_PATCH in markers


@pytest.fixture
def recording_temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
# Only the newest history is recorded: the older ones were recorded by the code of their time
# and are never re-recorded (this code's workflow always takes every patched branch, so a new
# recording of them would carry the later steps and their markers).
@pytest.mark.parametrize("name", ["held_gone"])
async def test_record(recording_temporal: str, name: str) -> None:
    @activity.defn(name=LIST_ACTIVITY)
    async def names() -> list[str]:
        return ["a", "b"]

    @activity.defn(name=PROVIDER_ACTIVITY)
    async def refresh(provider: str) -> str:
        if name == "failed" and provider == "a":
            raise RuntimeError("could not record the failure")
        return "refreshed"

    @activity.defn(name=SEEN_ACTIVITY)
    async def seen() -> None:
        if name == "failed":
            raise RuntimeError("no reports")

    @activity.defn(name=HELD_ACTIVITY)
    async def held() -> str:
        return "refreshed"

    @activity.defn(name=FILAMENT_PULL_ACTIVITY)
    async def bb_pull() -> str:
        return "pulled"

    @activity.defn(name=CAPTURE_ACTIVITY)
    async def capture() -> str:
        return "captured"

    @activity.defn(name=FILAMENT_RFID_ACTIVITY)
    async def rfid() -> str:
        return "done"

    client = await Client.connect(recording_temporal)
    queue = f"spools-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[SpoolRefresh],
        activities=[names, refresh, seen, held, bb_pull, capture, rfid],
    ):
        handle = await client.start_workflow(
            SpoolRefresh.run, id=f"spool-providers-{queue}", task_queue=queue
        )
        if name in {"providers_gone", "capture", "rfid", "held_gone"}:
            expected: dict[str, str] = {}
        else:
            expected = {"a": "failed" if name == "failed" else "refreshed", "b": "refreshed"}
        assert await handle.result() == expected
        history = await handle.fetch_history()
    HISTORIES.mkdir(exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json())
