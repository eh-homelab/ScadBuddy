"""#6905: every sync pass is recorded, a failed one too. A pass names its id, instance and
trigger; a step that raises after the pass starts (a CRUD call that timed out or was
refused, the settings read) is recorded with a fixed label and raised again. SB is the
fake `FilamentSyncApi` of `test_bb_sync_run.py`; no call reaches a live Bambuddy."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from uuid import UUID

import pytest
import respx

from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.filament.bb_sync_models import (
    BbSnapshot,
    BbWrite,
    BbWriteResult,
    Kind,
    ResultsReport,
    SyncPassInput,
    SyncPassResult,
    SyncPlan,
)
from scadbuddy.filament.bb_sync_run import FilamentSyncActivities, failure_label
from scadbuddy.library.settings_store import StoredSettings
from tests.filament.test_bb_sync_run import (
    INSTANCE,
    FakeSync,
    Round,
    _listings,
    _load,
    _plan,
    _run,
    _spool_updates,
    _write,
)


class FailingSync(FakeSync):
    """A `FakeSync` whose ``plan`` or ``results`` raises, and whose ``record_pass`` raises
    ``record_error`` when one is given."""

    def __init__(
        self,
        rounds: Sequence[Round] = (),
        *,
        plan_error: Exception | None = None,
        results_error: Exception | None = None,
        record_error: Exception | None = None,
    ) -> None:
        super().__init__(rounds)
        self.plan_error = plan_error
        self.results_error = results_error
        self.record_error = record_error

    async def plan(self, snap: BbSnapshot, limit: int) -> SyncPlan:
        if self.plan_error is not None:
            raise self.plan_error
        return await super().plan(snap, limit)

    async def results(
        self,
        instance: str,
        writes: Sequence[BbWrite],
        results: Sequence[BbWriteResult],
        pass_id: str | None,
        unconfirmed: Sequence[tuple[Kind, UUID]] = (),
    ) -> ResultsReport:
        if self.results_error is not None:
            raise self.results_error
        return await super().results(instance, writes, results, pass_id, unconfirmed)

    async def record_pass(self, result: SyncPassResult) -> None:
        if self.record_error is not None:
            raise self.record_error
        await super().record_pass(result)


@respx.mock
async def test_a_clean_pass_names_its_id_instance_and_trigger() -> None:
    _listings()
    sync = FakeSync()

    first = await _run(sync, trigger="timer")
    second = await _run(sync, trigger="more")

    assert sync.recorded == [first, second]
    assert first.error is None and first.instance == INSTANCE and first.trigger == "timer"
    assert first.pass_id is not None and second.pass_id is not None
    assert first.pass_id != second.pass_id
    assert second.trigger == "more"


@respx.mock
async def test_the_activity_passes_the_workflows_trigger() -> None:
    _listings()
    sync = FakeSync()

    await FilamentSyncActivities(sync, _load).one_pass(
        SyncPassInput(instance=INSTANCE, full=False, trigger="change"), lambda: None
    )

    [recorded] = sync.recorded
    assert recorded.trigger == "change"


@respx.mock
async def test_a_crud_timeout_in_the_plan_is_recorded_and_raised() -> None:
    """The prod failure of 2026-11-21 (#6902): the plan call timed out, and the pass was
    recorded nowhere. Now its row says so, and the activity still fails."""
    _listings()
    error = CrudUnavailableError("CRUD route unavailable: ReadTimeout", "transport")
    sync = FailingSync(plan_error=error)

    with pytest.raises(CrudUnavailableError):
        await _run(sync, trigger="full")

    [recorded] = sync.recorded
    assert recorded.error == "crud_unavailable:transport"
    assert recorded.trigger == "full" and recorded.instance == INSTANCE
    assert recorded.pass_id is not None and recorded.started_at is not None
    assert recorded.written == 0 and recorded.instance_changed is False


@respx.mock
async def test_a_refused_results_call_is_recorded_with_its_status() -> None:
    _listings()
    _spool_updates()
    sync = FailingSync(
        [_plan([_write("update", 7, {"note": "x"})])],
        results_error=CrudRefusedError(409, {"detail": "secret detail"}),
    )

    with pytest.raises(CrudRefusedError):
        await _run(sync)

    [recorded] = sync.recorded
    assert recorded.error == "crud_refused:409"
    # Bambuddy took the write before the results call failed, so the row counts it.
    assert recorded.written == 1 and recorded.failed == 0


async def test_a_settings_read_that_fails_is_recorded_as_internal() -> None:
    sync = FakeSync()

    async def broken() -> StoredSettings:
        raise RuntimeError("the database said something private")

    with pytest.raises(RuntimeError):
        await _run(sync, load=broken)

    [recorded] = sync.recorded
    # Never the message.
    assert recorded.error == "internal"


@respx.mock
async def test_a_record_that_fails_too_raises_the_first_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _listings()
    first = CrudUnavailableError("CRUD route unavailable: ReadTimeout", "transport")
    sync = FailingSync(
        plan_error=first,
        record_error=CrudUnavailableError("CRUD route unavailable", "unreached"),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(CrudUnavailableError) as raised:
        await _run(sync)

    assert raised.value is first
    assert sync.recorded == []
    [record] = [r for r in caplog.records if r.message == "could not record a failed sync pass"]
    assert record.__dict__["error"] == "crud_unavailable:transport"
    assert record.__dict__["record_error"] == "crud_unavailable:unreached"


def test_the_failure_labels() -> None:
    assert failure_label(CrudRefusedError(422, {})) == "crud_refused:422"
    assert failure_label(CrudUnavailableError("x", "database_unavailable")) == (
        "crud_unavailable:database_unavailable"
    )
    assert failure_label(ValueError("private")) == "internal"
