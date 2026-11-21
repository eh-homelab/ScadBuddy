"""`FilamentSyncNudger` (#6265 Task 8, plan Decision 17): a signal-with-start of the
configured Bambuddy's ``FilamentBbSync``, over a fake client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.settings import FilamentSync, Settings
from scadbuddy.filament.bb_sync_nudge import (
    CHANGED_SIGNAL,
    FILAMENT_BB_SYNC_WORKFLOW,
    FilamentSyncNudger,
    SyncStart,
    instance_for,
    workflow_id,
)
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from tests.conftest import UNUSED_TEMPORAL_ADDRESS

URL = "https://bambuddy.test/"
QUEUE = "bambuddy"


class FakeClient:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.starts: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.signals: list[tuple[str, tuple[Any, ...]]] = []
        self.fail = fail

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        if self.fail is not None:
            raise self.fail
        self.starts.append((args, kwargs))

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        return FakeHandle(self, workflow_id)


class FakeHandle:
    def __init__(self, client: FakeClient, workflow_id: str) -> None:
        self.client = client
        self.id = workflow_id

    async def signal(self, *args: Any) -> None:
        if self.client.fail is not None:
            raise self.client.fail
        self.client.signals.append((self.id, args))


#: Only the spools push: a nudge starts the workflow.
PUSHING = FilamentSync(spools="sb_to_bb")


def settings(url: str | None, interval: int = 300, sync: FilamentSync = PUSHING) -> Any:
    def read(names: Collection[str]) -> dict[str, Any]:
        assert set(names) == {"bambuddy_url", "spool_overlay_refresh_seconds", "filament_sync"}
        return {
            "bambuddy_url": url,
            "spool_overlay_refresh_seconds": interval,
            "filament_sync": sync,
        }

    return read


def nudger(
    client: FakeClient, url: str | None, interval: int = 300, sync: FilamentSync = PUSHING
) -> FilamentSyncNudger:
    return FilamentSyncNudger(lambda: client, settings(url, interval, sync), QUEUE)  # type: ignore[arg-type,return-value]


def test_the_workflow_id_names_the_instance() -> None:
    assert workflow_id("0123456789ab") == "filament-bb-sync-0123456789ab"


async def test_nudge_signals_with_start() -> None:
    client = FakeClient()
    await nudger(client, URL, 120).nudge(full=True)
    ((args, kwargs),) = client.starts
    instance = instance_for(URL)
    assert args == (FILAMENT_BB_SYNC_WORKFLOW, SyncStart(instance=instance, interval_s=120))
    assert kwargs["id"] == workflow_id(instance) == f"filament-bb-sync-{instance}"
    assert kwargs["task_queue"] == QUEUE
    assert kwargs["start_signal"] == CHANGED_SIGNAL == "changed"
    assert kwargs["start_signal_args"] == [True]
    assert kwargs["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING


async def test_a_nudge_is_not_full_by_default() -> None:
    client = FakeClient()
    await nudger(client, URL).nudge()
    ((_, kwargs),) = client.starts
    assert kwargs["start_signal_args"] == [False]


@pytest.mark.parametrize("url", [None, ""])
async def test_nudge_without_bambuddy_does_nothing(url: str | None) -> None:
    client = FakeClient()
    await nudger(client, url).nudge(full=True)
    assert client.starts == []


@pytest.mark.parametrize(
    "sync",
    [
        FilamentSync(),
        FilamentSync(spools="off", weights="off", tags="off", presets="off"),
        FilamentSync(tags="two_way"),
    ],
    ids=["defaults", "all-off", "tags-only"],
)
async def test_a_nudge_with_nothing_pushing_does_nothing(sync: FilamentSync) -> None:
    """R-T8-2: no workflow when no data type pushes; the answer says why (R-T10-2)."""
    client = FakeClient()
    assert await nudger(client, URL, sync=sync).nudge(full=True) == "nothing_pushes"
    assert client.starts == []


@pytest.mark.requires_postgres
async def test_turning_a_direction_on_starts_the_workflow(tmp_path: Path, pg_conninfo: str) -> None:
    """R-T8-2, end to end at the nudge: over the real settings store, a nudge with the
    default directions starts nothing; once a save turns a push on (the save
    `put_settings` nudges after), the nudge starts the workflow."""
    store = SettingsStore(
        Settings(
            data_dir=tmp_path,
            database_url=pg_conninfo,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            bambuddy_url=URL,
        )
    )
    store.open()
    try:
        client = FakeClient()
        nudge = FilamentSyncNudger(lambda: client, store.read, QUEUE)  # type: ignore[arg-type,return-value]
        await nudge.nudge()
        assert client.starts == []
        store.save(SettingsPatch(filament_sync=FilamentSync(spools="two_way")))
        await nudge.nudge()
        ((args, kwargs),) = client.starts
        assert args[1].instance == instance_for(URL)
        assert kwargs["start_signal"] == CHANGED_SIGNAL
    finally:
        store.close()


async def test_a_nudge_failure_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeClient(fail=RuntimeError("Temporal is down"))
    with caplog.at_level(logging.WARNING, logger="scadbuddy.filament.bb_sync_nudge"):
        await nudger(client, URL).nudge()
    assert [r.message for r in caplog.records] == ["could not nudge the filament sync"]


async def test_a_settings_read_that_fails_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(names: Collection[str]) -> dict[str, Any]:
        raise OSError("the database is gone")

    client = FakeClient()
    with caplog.at_level(logging.WARNING, logger="scadbuddy.filament.bb_sync_nudge"):
        await FilamentSyncNudger(lambda: client, broken, QUEUE).nudge()  # type: ignore[arg-type,return-value]
    assert client.starts == []
    assert [r.message for r in caplog.records] == ["could not nudge the filament sync"]


async def test_a_nudge_that_hangs_ends_at_its_timeout(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scadbuddy.filament.bb_sync_nudge.NUDGE_TIMEOUT", 0.1)

    class Hanging(FakeClient):
        async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
            await asyncio.Event().wait()

    with caplog.at_level(logging.WARNING, logger="scadbuddy.filament.bb_sync_nudge"):
        await asyncio.wait_for(nudger(Hanging(), URL).nudge(), 5)
    assert [r.message for r in caplog.records] == ["could not nudge the filament sync"]


async def test_nudge_soon_runs_the_nudge_in_a_task() -> None:
    client = FakeClient()
    soon = nudger(client, URL)
    soon.nudge_soon(full=True)
    assert client.starts == []
    for _ in range(50):
        if client.starts:
            break
        await asyncio.sleep(0.01)
    ((_, kwargs),) = client.starts
    assert kwargs["start_signal_args"] == [True]


async def test_a_nudge_answers_why_it_started_nothing() -> None:
    client = FakeClient()
    assert await nudger(client, None).nudge(full=True) == "no_bambuddy"
    assert await nudger(client, URL).nudge(full=True) is None
    assert len(client.starts) == 1


async def test_raise_errors_raises_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """R-T10-2: "Sync now" learns that the start failed, so it never answers 202."""
    client = FakeClient(fail=RuntimeError("Temporal is down"))
    with (
        caplog.at_level(logging.WARNING, logger="scadbuddy.filament.bb_sync_nudge"),
        pytest.raises(RuntimeError, match="Temporal is down"),
    ):
        await nudger(client, URL).nudge(full=True, raise_errors=True)
    assert caplog.records == []


async def test_signal_running_signals_and_never_starts() -> None:
    """R-T10-3: the save that leaves nothing pushing tells a running workflow."""
    client = FakeClient()
    await nudger(client, URL, sync=FilamentSync()).signal_running()
    assert client.starts == []
    assert client.signals == [(workflow_id(instance_for(URL)), (CHANGED_SIGNAL, False))]


async def test_signal_running_with_a_url_signals_that_instance() -> None:
    """Decision 161: a save that changes ``bambuddy_url`` tells the old instance's
    workflow, whatever the settings now name."""
    client = FakeClient()
    old = "https://old-bambuddy.test"
    await nudger(client, URL).signal_running(old)
    assert client.starts == []
    assert client.signals == [(workflow_id(instance_for(old)), (CHANGED_SIGNAL, False))]


@pytest.mark.parametrize("url", [None, ""])
async def test_signal_running_without_bambuddy_does_nothing(url: str | None) -> None:
    client = FakeClient()
    await nudger(client, url).signal_running()
    assert client.signals == []
    assert client.starts == []


async def test_signal_running_with_no_workflow_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeClient(fail=RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b""))
    with caplog.at_level(logging.WARNING, logger="scadbuddy.filament.bb_sync_nudge"):
        await nudger(client, URL).signal_running()
    assert caplog.records == []
    assert client.starts == []


async def test_a_signal_failure_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeClient(fail=RPCError("down", RPCStatusCode.UNAVAILABLE, b""))
    with caplog.at_level(logging.WARNING, logger="scadbuddy.filament.bb_sync_nudge"):
        await nudger(client, URL).signal_running()
    assert [r.message for r in caplog.records] == ["could not signal the filament sync"]


async def test_signal_running_soon_signals_in_a_task() -> None:
    client = FakeClient()
    nudger(client, URL).signal_running_soon()
    assert client.signals == []
    for _ in range(50):
        if client.signals:
            break
        await asyncio.sleep(0.01)
    assert len(client.signals) == 1
