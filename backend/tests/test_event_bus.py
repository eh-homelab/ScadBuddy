"""The event bus (#264, #266): fan-out, bounded queues, thread-safe publishing, and
the publishers that live below the API (the render queue, the history, settings)."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver
from scadbuddy.core.config import load_config
from scadbuddy.core.events import (
    EVENT_KINDS,
    Event,
    FontInstalled,
    InProcessEventBus,
    JobEvent,
    ModelEvent,
    SettingsChanged,
    Subscription,
    decode_event,
    emit,
    encode_event,
)
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.render.job_store import JobStore, Reaped
from scadbuddy.render.jobs import Job, JobResult, RenderQueue
from scadbuddy.render.solids import WRAPPER_PREFIX


def _model(slug: str, kind: str = "model.updated") -> ModelEvent:
    return ModelEvent(kind=kind, slug=slug)  # type: ignore[arg-type]


async def test_every_subscriber_gets_every_event() -> None:
    bus = InProcessEventBus()
    first, second = bus.subscribe(), bus.subscribe()

    bus.publish(_model("a"))
    bus.publish(_model("b"))

    for subscription in (first, second):
        assert _slugs([await subscription.get(), await subscription.get()]) == ["a", "b"]


async def test_a_subscriber_can_filter_by_kind() -> None:
    bus = InProcessEventBus()
    fonts = bus.subscribe(kinds={"font.installed"})

    bus.publish(_model("a"))
    bus.publish(FontInstalled(family="Pacifico"))

    event = await fonts.get()
    assert event.kind == "font.installed"
    assert fonts.get_nowait() is None


async def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown event kinds"):
        InProcessEventBus().subscribe(kinds={"model.renamed"})


def _drain(subscription: Subscription) -> list[Event]:
    events: list[Event] = []
    while (event := subscription.get_nowait()) is not None:
        events.append(event)
    return events


def _slugs(events: list[Event]) -> list[str]:
    return [event.slug for event in events if isinstance(event, ModelEvent)]


async def test_a_slow_subscriber_loses_its_oldest_events_and_counts_them() -> None:
    bus = InProcessEventBus(queue_size=2)
    slow, roomy = bus.subscribe(), bus.subscribe(maxsize=10)

    for slug in ("a", "b", "c", "d"):
        bus.publish(_model(slug))

    assert slow.dropped == 2
    assert _slugs(_drain(slow)) == ["c", "d"]
    # One subscriber falling behind costs nobody else anything.
    assert roomy.dropped == 0
    assert _slugs(_drain(roomy)) == ["a", "b", "c", "d"]


async def test_drop_oldest_keeps_the_newest() -> None:
    bus = InProcessEventBus(queue_size=2)
    slow = bus.subscribe()
    for slug in ("a", "b", "c"):
        bus.publish(_model(slug))
    assert slow.dropped == 1
    assert _slugs(_drain(slow)) == ["b", "c"]


async def test_publishing_from_a_worker_thread_reaches_the_loop() -> None:
    bus = InProcessEventBus()
    subscription = bus.subscribe()

    thread = threading.Thread(target=bus.publish, args=(_model("from-a-thread"),))
    thread.start()
    thread.join()

    event = await asyncio.wait_for(subscription.get(), timeout=1)
    assert _slugs([event]) == ["from-a-thread"]


async def test_closing_ends_iteration_and_unsubscribes() -> None:
    bus = InProcessEventBus()
    subscription = bus.subscribe()
    bus.publish(_model("a"))
    received: list[Event] = []

    async def consume() -> None:
        async for event in subscription:
            received.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    assert bus.subscriber_count == 1
    await bus.aclose()
    await asyncio.wait_for(task, timeout=1)

    assert _slugs(received) == ["a"]
    assert bus.subscriber_count == 0
    bus.publish(_model("b"))  # nobody left, and nothing raises


async def test_a_subscription_is_a_context_manager() -> None:
    bus = InProcessEventBus()
    async with bus.subscribe() as subscription:
        assert bus.subscriber_count == 1
    assert subscription.closed
    assert bus.subscriber_count == 0


def test_a_failing_listener_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    bus = InProcessEventBus()
    seen: list[Event] = []

    def broken(event: Event) -> None:
        raise RuntimeError("boom")

    bus.add_listener(broken)
    remove = bus.add_listener(seen.append)
    with caplog.at_level(logging.ERROR):
        bus.publish(_model("a"))
    assert [event.kind for event in seen] == ["model.updated"]
    assert "an event listener failed" in caplog.text

    remove()
    bus.publish(_model("b"))
    assert len(seen) == 1


def test_emit_never_lets_publishing_break_the_caller(caplog: pytest.LogCaptureFixture) -> None:
    class Broken(InProcessEventBus):
        def publish(self, event: Event) -> None:
            raise RuntimeError("the bus is down")

    with caplog.at_level(logging.ERROR):
        emit(Broken(), _model("a"))
        emit(None, _model("a"))
    assert "could not publish an event" in caplog.text


def test_events_round_trip_through_their_wire_form() -> None:
    event = JobEvent(kind="job.done", job_id="f" * 32, slug="demo")
    assert decode_event(encode_event(event)) == event
    # Ids only: the payload names what changed and never carries it.
    assert set(event.model_dump()) == {"id", "at", "kind", "job_id", "slug"}


def test_every_kind_from_the_spec_is_known() -> None:
    assert {
        "job.pending",
        "job.running",
        "job.done",
        "job.failed",
        "job.superseded",
        "model.created",
        "model.updated",
        "model.deleted",
        "source.changed",
        "version.committed",
        "upstream.available",
        "output.created",
        "output.deleted",
        "print.progress",
        "print.settled",
        "library.changed",
        "library.removed",
        "font.installed",
        "settings.changed",
    } == EVENT_KINDS


# ── publishers below the API ─────────────────────────────────────────────────────


def _record(bus: InProcessEventBus) -> list[Event]:
    seen: list[Event] = []
    bus.add_listener(seen.append)
    return seen


async def test_the_render_queue_announces_each_state(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    seen = _record(bus)
    paths = DataPaths(tmp_path)

    async def render(job: Job) -> tuple[JobResult, list[str]]:
        if job.slug == "broken":
            raise RuntimeError("no")
        result = JobResult.model_validate(
            {
                "model_3mf": "a",
                "preview_glb": "b",
                "parts": [],
                "bbox_mm": {"min": [0, 0, 0], "max": [1, 1, 1], "size": [1, 1, 1]},
            }
        )
        return result, []

    queue = RenderQueue(load_config(), paths, render=render, events=bus)
    await queue.start()
    try:
        done = await queue.submit("demo", {})
        failed = await queue.submit("broken", {})
        await queue.join()
    finally:
        await queue.aclose()

    announced = [(e.kind, e.job_id) for e in seen if isinstance(e, JobEvent)]
    assert announced.count(("job.pending", done.id)) == 1
    assert ("job.running", done.id) in announced
    assert ("job.done", done.id) in announced
    assert ("job.failed", failed.id) in announced
    assert announced.index(("job.running", done.id)) < announced.index(("job.done", done.id))


def _jobs(seen: list[Event]) -> list[tuple[str, str]]:
    return [(e.kind, e.job_id) for e in seen if isinstance(e, JobEvent)]


async def test_a_superseded_job_is_announced_so_nobody_waits_on_it(tmp_path: Path) -> None:
    """#267: a job a newer submit replaced before it started must end for whoever
    follows it, or the UI waits forever."""
    bus = InProcessEventBus()
    seen = _record(bus)
    queue = RenderQueue(load_config(), DataPaths(tmp_path), events=bus)  # no workers
    try:
        first = await queue.submit("demo", {"width": 1})
        second = await queue.submit("demo", {"width": 2}, supersedes=first.id)
    finally:
        queue.close_thumbnails()

    assert _jobs(seen) == [
        ("job.pending", first.id),
        ("job.superseded", first.id),
        ("job.pending", second.id),
    ]


async def test_a_coalesced_submit_publishes_nothing_new(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    seen = _record(bus)
    queue = RenderQueue(load_config(), DataPaths(tmp_path), events=bus)
    try:
        first = await queue.submit("demo", {"width": 1})
        again = await queue.submit("demo", {"width": 1})
    finally:
        queue.close_thumbnails()

    assert again.id == first.id
    assert _jobs(seen) == [("job.pending", first.id)]


async def test_a_job_that_waited_past_its_deadline_is_failed_without_running(
    tmp_path: Path,
) -> None:
    bus = InProcessEventBus()
    seen = _record(bus)
    config = replace(load_config(), render_queue_timeout=1.0)
    queue = RenderQueue(config, DataPaths(tmp_path), events=bus)
    try:
        job = await queue.submit("demo", {})
        claimed = queue.store.claim()
        assert claimed is not None
        claimed.created_at = claimed.created_at - timedelta(minutes=5)
        await queue._run(claimed)
    finally:
        queue.close_thumbnails()

    assert _jobs(seen) == [("job.pending", job.id), ("job.failed", job.id)]


class _LosesOneWorker(JobStore):
    """The file store, but its first reap finds the running job's worker gone and
    requeues it, as the Postgres store does when a lease expires."""

    def __init__(self, paths: DataPaths) -> None:
        super().__init__(paths)
        self.lost: Job | None = None

    def reap(self, *, lease: float, max_attempts: int) -> Reaped:
        if self.lost is None:
            return Reaped()
        lost, self.lost = self.lost, None
        return Reaped(requeued=[lost])


async def test_a_requeued_job_is_pending_again_and_a_reaped_failure_is_failed(
    tmp_path: Path,
) -> None:
    bus = InProcessEventBus()
    seen = _record(bus)
    store = _LosesOneWorker(DataPaths(tmp_path))
    config = replace(load_config(), render_lease_timeout=0.03)
    queue = RenderQueue(config, DataPaths(tmp_path), store=store, events=bus)
    requeued = Job(id="a" * 32, slug="demo", created_at=datetime.now(UTC))
    store.lost = requeued
    await queue.start()
    try:
        for _ in range(100):
            if ("job.pending", requeued.id) in _jobs(seen):
                break
            await asyncio.sleep(0.01)
    finally:
        await queue.aclose()
    assert ("job.pending", requeued.id) in _jobs(seen)

    # The reaper's other outcome: out of attempts, failed.
    seen.clear()
    queue._settled(requeued, "failed")
    assert _jobs(seen) == [("job.failed", requeued.id)]


async def test_a_restart_announces_the_jobs_it_failed(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path)
    first = RenderQueue(load_config(), paths)
    job = await first.submit("demo", {})  # never started: no workers
    first.close_thumbnails()

    bus = InProcessEventBus()
    seen = _record(bus)
    second = RenderQueue(load_config(), paths, events=bus)
    await second.start()
    await second.aclose()

    assert [(e.kind, e.job_id) for e in seen if isinstance(e, JobEvent)] == [("job.failed", job.id)]


def test_every_settings_write_is_announced_with_its_section(tmp_path: Path) -> None:
    bus = InProcessEventBus()
    seen = _record(bus)
    store = SettingsStore(tmp_path / "settings.json", Settings(data_dir=tmp_path), events=bus)

    store.save(SettingsPatch(public_url="https://scad.example"))
    store.set_model_pipeline("demo", 3)
    store.remember_project(7)

    assert [e.section for e in seen if isinstance(e, SettingsChanged)] == [
        "connection",
        "model_pipeline",
        "last_project",
    ]


def _meta() -> OutputMeta:
    return OutputMeta.model_validate(
        {
            "id": "o1",
            "slug": "demo",
            "job_id": "j1",
            "created_at": "2026-09-27T00:00:00Z",
            "bbox_mm": {"min": [0, 0, 0], "max": [1, 1, 1], "size": [1, 1, 1]},
        }
    )


def _progress(stage: str, *, settled: bool = False) -> PrintProgress:
    return PrintProgress(route="slice_queue", stage=stage, settled=settled, bambuddy_url="x")  # type: ignore[arg-type]


def test_progress_is_announced_on_change_and_settled_once() -> None:
    bus = InProcessEventBus()
    seen = _record(bus)
    observer = ProgressObserver(bus)
    meta = _meta()

    observer.observe(meta, None)  # never printed: nothing to say
    observer.observe(meta, _progress("running"))
    observer.observe(meta, _progress("running"))  # a poll that found nothing new
    observer.observe(meta, _progress("done", settled=True))
    observer.observe(meta, _progress("done", settled=True))

    assert [e.kind for e in seen] == ["print.progress", "print.progress", "print.settled"]


def test_starting_a_print_is_progress_and_forgets_what_was_seen() -> None:
    bus = InProcessEventBus()
    seen = _record(bus)
    observer = ProgressObserver(bus)
    meta = _meta()
    observer.observe(meta, _progress("done", settled=True))
    seen.clear()

    observer.started(meta)
    observer.observe(meta, _progress("done", settled=True))

    assert [e.kind for e in seen] == ["print.progress", "print.progress", "print.settled"]


@pytest.mark.parametrize("distinct", [False, True], ids=["same-read", "different-reads"])
def test_concurrent_reads_publish_settled_exactly_once(distinct: bool) -> None:
    """Several pollers of one output finding it settled at the same moment: whichever
    order they land in, one of them is first and only that one publishes it."""
    bus = InProcessEventBus()
    seen = _record(bus)
    observer = ProgressObserver(bus)
    meta = _meta()
    observer.observe(meta, _progress("running"))
    seen.clear()
    readers = 16
    barrier = threading.Barrier(readers)

    def read(index: int) -> None:
        progress = _progress("done", settled=True)
        if distinct:
            # Settled, but not byte-identical: each read differs in a detail.
            progress = progress.model_copy(update={"copies_completed": index})
        barrier.wait()
        for _ in range(50):
            observer.observe(meta, progress)

    threads = [threading.Thread(target=read, args=(index,)) for index in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [e.kind for e in seen].count("print.settled") == 1
    if not distinct:
        assert [e.kind for e in seen] == ["print.progress", "print.settled"]


def test_the_observer_remembers_a_bounded_number_of_outputs() -> None:
    observer = ProgressObserver(None, capacity=2)
    for index in range(5):
        meta = _meta().model_copy(update={"id": f"o{index}"})
        observer.observe(meta, _progress("running"))
    assert len(observer._seen) == 2


@pytest.mark.requires_git
def test_the_history_reports_each_commit_and_the_templates_it_touched(
    tmp_path: Path,
) -> None:
    history = ModelHistory(tmp_path / "models", wrapper_prefix=WRAPPER_PREFIX)
    history.ensure_repo()
    seen: list[tuple[str, list[str]]] = []
    history.on_commit = lambda commit, touched: seen.append((commit, touched))

    (tmp_path / "models" / "demo").mkdir(parents=True)
    (tmp_path / "models" / "demo" / "model.scad").write_text("cube(1);\n", encoding="utf-8")
    created = history.commit("Add demo", "demo")

    assert seen == [(created, ["demo"])]
    assert history.commit("Nothing", "demo") is None
    assert len(seen) == 1  # no commit, no announcement


@pytest.mark.requires_git
def test_a_failing_commit_listener_does_not_fail_the_commit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    history = ModelHistory(tmp_path / "models", wrapper_prefix=WRAPPER_PREFIX)
    history.ensure_repo()

    def broken(commit: str, touched: list[str]) -> None:
        raise RuntimeError("boom")

    history.on_commit = broken
    (tmp_path / "models" / "demo").mkdir(parents=True)
    (tmp_path / "models" / "demo" / "model.scad").write_text("cube(1);\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert history.commit("Add demo", "demo") is not None
    assert "a commit listener failed" in caplog.text
