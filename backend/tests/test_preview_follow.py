"""#265: the print dialog's background slice, followed by the API process, which
publishes each change on ``preview-slice:<slicer>:<job id>`` so the dialog never polls."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from scadbuddy.bambuddy.preview import SlicePreview
from scadbuddy.core.events import Event, InProcessEventBus, PreviewSliceEvent
from scadbuddy.core.problems import ApiError
from scadbuddy.preview_follow.follows import MAX_FOLLOWS, PreviewFollows, Reader

SUBJECT = "a" * 12


def kinds(seen: list[Event]) -> list[str]:
    return [e.kind for e in seen]


def watching() -> tuple[InProcessEventBus, list[Event]]:
    bus = InProcessEventBus()
    seen: list[Event] = []
    bus.add_listener(seen.append)
    return bus, seen


def reads(*answers: SlicePreview | Exception) -> Reader:
    queue = list(answers)

    async def read() -> SlicePreview:
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return read


def running(progress: int | None = None) -> SlicePreview:
    return SlicePreview(job_id=7, status="running", progress=progress)


async def settle(follows: PreviewFollows) -> None:
    for _ in range(200):
        if follows.following == 0:
            return
        await asyncio.sleep(0)
    raise AssertionError("the follow did not end")


async def test_a_running_slice_is_followed_to_its_end_and_each_change_published() -> None:
    bus, seen = watching()
    follows = PreviewFollows(bus, interval=0)
    read = reads(running(10), running(10), running(40), SlicePreview(job_id=7, status="completed"))

    first = await follows.read("native", 7, SUBJECT, read)
    assert first.progress == 10
    await settle(follows)

    assert kinds(seen) == ["preview_slice.progress", "preview_slice.settled"]
    assert seen[-1] == PreviewSliceEvent(
        kind="preview_slice.settled", slicer="native", job_id=7, id=seen[-1].id, at=seen[-1].at
    )


async def test_a_finished_slice_is_not_followed() -> None:
    bus, seen = watching()
    follows = PreviewFollows(bus, interval=0)
    await follows.read("bambuddy", 7, SUBJECT, reads(SlicePreview(job_id=7, status="failed")))
    assert follows.following == 0
    assert seen == []


async def test_one_slice_is_followed_once_however_often_it_is_read() -> None:
    bus, _ = watching()
    follows = PreviewFollows(bus, interval=3600)
    read = reads(running())
    for _ in range(3):
        await follows.read("bambuddy", 7, SUBJECT, read)
    await follows.read("native", 7, SUBJECT, read)
    assert follows.following == 2
    await follows.aclose()
    assert follows.following == 0


async def test_a_read_that_fails_is_published_so_the_dialog_reads_and_shows_it() -> None:
    bus, seen = watching()
    follows = PreviewFollows(bus, interval=0)
    read = reads(running(), ApiError(404, "there is no background slice 7"))
    await follows.read("bambuddy", 7, SUBJECT, read)
    await settle(follows)
    assert kinds(seen) == ["preview_slice.settled"]


async def test_a_follow_out_of_time_tells_the_dialog_to_read_again() -> None:
    bus, seen = watching()
    now = [0.0]
    clock: Callable[[], float] = lambda: now[0]  # noqa: E731
    follows = PreviewFollows(bus, interval=0, limit=10, clock=clock)

    async def read() -> SlicePreview:
        now[0] += 6
        return running()

    await follows.read("native", 7, SUBJECT, read)
    await settle(follows)
    # A read of the route after it follows the slice again.
    assert kinds(seen) == ["preview_slice.progress"]


async def test_past_the_cap_a_read_says_its_slice_is_not_followed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The dialog then reads on its timer (#265 review): it is never left waiting for an
    event no follow will send."""
    bus, _ = watching()
    follows = PreviewFollows(bus, interval=3600)
    answers = [
        await follows.read("native", job + 1, SUBJECT, reads(running()))
        for job in range(MAX_FOLLOWS + 1)
    ]
    assert follows.following == MAX_FOLLOWS
    assert [a.followed for a in answers] == [True] * MAX_FOLLOWS + [False]
    assert "too many background slices to follow" in caplog.text
    # A slice already followed reads as followed, however full the process is.
    again = await follows.read("native", 1, SUBJECT, reads(running()))
    assert again.followed is True
    # An ended slice has nothing more to announce.
    done = await follows.read(
        "native", 999, SUBJECT, reads(SlicePreview(job_id=999, status="completed"))
    )
    assert done.followed is False
    await follows.aclose()


async def test_a_follow_with_no_change_for_a_while_stops_and_says_so() -> None:
    """A dialog that closed leaves its follow; it stops after `quiet_reads` reads that
    change nothing, and the ``progress`` it publishes makes a dialog that still watches
    read again, which follows again."""
    bus, seen = watching()
    reads_made = 0

    async def read() -> SlicePreview:
        nonlocal reads_made
        reads_made += 1
        return running(30)

    follows = PreviewFollows(bus, interval=0, quiet_reads=3)
    await follows.read("native", 7, SUBJECT, read)
    await settle(follows)
    assert reads_made == 1 + 3
    assert kinds(seen) == ["preview_slice.progress"]


async def test_a_change_starts_the_quiet_count_again() -> None:
    bus, seen = watching()
    follows = PreviewFollows(bus, interval=0, quiet_reads=2)
    read = reads(running(1), running(1), running(2), running(2), running(2))
    await follows.read("native", 7, SUBJECT, read)
    await settle(follows)
    # The change to 2, then the stop after two quiet reads.
    assert kinds(seen) == ["preview_slice.progress", "preview_slice.progress"]


async def test_shutdown_cancels_a_follow_whose_read_is_in_flight() -> None:
    bus, seen = watching()
    started = asyncio.Event()
    cancelled = False
    first = True

    async def read() -> SlicePreview:
        nonlocal first, cancelled
        if first:
            first = False
            return running()
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        raise AssertionError("never answers")

    follows = PreviewFollows(bus, interval=0)
    await follows.read("bambuddy", 7, SUBJECT, read)
    await asyncio.wait_for(started.wait(), 1)
    await follows.aclose()
    assert cancelled
    assert follows.following == 0
    # A cancelled read is not a failed one: nothing tells the dialog it settled.
    assert seen == []
