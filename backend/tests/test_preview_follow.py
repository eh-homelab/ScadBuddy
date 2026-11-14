"""#265: the print dialog's background slice, followed by the API process, which
publishes each change on ``preview-slice:<slicer>:<job id>`` so the dialog never polls."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import NoReturn

import psycopg
import pytest

from scadbuddy.bambuddy.preview import SlicePreview
from scadbuddy.core.events import Event, InProcessEventBus, PreviewSliceEvent
from scadbuddy.core.problems import ApiError
from scadbuddy.preview_follow import claims as claims_module
from scadbuddy.preview_follow.claims import PgFollowClaims
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
    # Real time, not loop turns: a follow releases its claim on a thread, which a busy CI
    # runner can keep from finishing within any fixed number of turns.
    for _ in range(500):
        if follows.following == 0:
            return
        await asyncio.sleep(0.01)
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


class SharedClaims:
    """`FollowClaims` shared by the replicas of one test, as Postgres's advisory locks are."""

    def __init__(self) -> None:
        self.held: dict[str, str] = {}
        self.closed: list[str] = []
        self.made: list[str] = []

    def of(self, replica: str) -> ReplicaClaims:
        return ReplicaClaims(self, replica)


class ReplicaClaims:
    def __init__(self, shared: SharedClaims, replica: str) -> None:
        self.shared = shared
        self.replica = replica

    def claim(self, key: str) -> bool:
        self.shared.made.append(key)
        return self.shared.held.setdefault(key, self.replica) == self.replica

    def release(self, key: str) -> None:
        if self.shared.held.get(key) == self.replica:
            del self.shared.held[key]

    def close(self) -> None:
        self.shared.held = {k: r for k, r in self.shared.held.items() if r != self.replica}
        self.shared.closed.append(self.replica)


async def test_a_slice_read_on_two_replicas_is_followed_by_one() -> None:
    """#6158: every replica's sockets hear the follower's events over the Postgres bus, so
    a read on another replica answers ``followed`` and reads the slice no more."""
    bus, seen = watching()
    shared = SharedClaims()
    one = PreviewFollows(bus, claims=shared.of("one"), interval=0)
    two = PreviewFollows(bus, claims=shared.of("two"), interval=0)
    reads_made = 0
    answers = [running(10), running(10), running(40), SlicePreview(job_id=7, status="completed")]

    async def read() -> SlicePreview:
        nonlocal reads_made
        reads_made += 1
        return answers.pop(0) if len(answers) > 1 else answers[0]

    first = await one.read("native", 7, SUBJECT, read)
    other = await two.read("native", 7, SUBJECT, read)
    assert (first.followed, other.followed) == (True, True)
    assert (one.following, two.following) == (1, 0)
    await settle(one)
    # Each change once, and no reads but the two answered ones and one follow's.
    assert kinds(seen) == ["preview_slice.progress", "preview_slice.settled"]
    assert reads_made == 4
    assert shared.held == {}


async def test_a_follow_releases_its_claim_before_its_last_event() -> None:
    """The read that the last event causes may reach any replica, and it must be able to
    follow the slice again there."""
    bus = InProcessEventBus()
    shared = SharedClaims()
    held_at_event: list[dict[str, str]] = []
    bus.add_listener(lambda _: held_at_event.append(dict(shared.held)))
    follows = PreviewFollows(bus, claims=shared.of("one"), interval=0, quiet_reads=1)
    await follows.read("native", 7, SUBJECT, reads(running(5)))
    await settle(follows)
    assert held_at_event == [{}]
    again = PreviewFollows(bus, claims=shared.of("two"), interval=3600)
    await again.read("native", 7, SUBJECT, reads(running(5)))
    assert again.following == 1
    await again.aclose()


async def test_a_claim_on_its_way_is_not_made_twice() -> None:
    bus, _ = watching()
    shared = SharedClaims()
    follows = PreviewFollows(bus, claims=shared.of("one"), interval=3600)
    read = reads(running())
    answers = await asyncio.gather(*(follows.read("native", 7, SUBJECT, read) for _ in range(3)))
    assert [a.followed for a in answers] == [True] * 3
    assert shared.made == [f"native:7:{SUBJECT}"]
    assert follows.following == 1
    await follows.aclose()


async def test_shutdown_releases_every_claim() -> None:
    bus, seen = watching()
    shared = SharedClaims()
    follows = PreviewFollows(bus, claims=shared.of("one"), interval=3600)
    await follows.read("bambuddy", 7, SUBJECT, reads(running()))
    assert list(shared.held) == [f"bambuddy:7:{SUBJECT}"]
    await follows.aclose()
    assert shared.held == {}
    assert shared.closed == ["one"]
    assert seen == []


class SlowClaims(ReplicaClaims):
    """A first claim that waits on its thread until the test lets it answer."""

    def __init__(self, shared: SharedClaims, replica: str, *, fails: bool = False) -> None:
        super().__init__(shared, replica)
        self.started = threading.Event()
        self.go_on = threading.Event()
        self.calls: list[str] = []
        #: Whether the first claim raises instead of answering.
        self.fails = fails

    def claim(self, key: str) -> bool:
        if not self.started.is_set():
            self.started.set()
            assert self.go_on.wait(5)
            if self.fails:
                raise RuntimeError("the claim broke")
        return super().claim(key)

    def release(self, key: str) -> None:
        self.calls.append("release")
        super().release(key)

    def close(self) -> None:
        self.calls.append("close")
        super().close()


async def wait_for_thread(event: threading.Event) -> None:
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the claim's thread did not start")


async def cancel_a_claim_and_read_again(
    follows: PreviewFollows, slow: SlowClaims, seen: list[Event]
) -> None:
    """Cancel a read while its claim's thread runs, read again (told ``followed``), then
    let the claim answer and wait until it is settled."""
    reading = asyncio.create_task(follows.read("native", 7, SUBJECT, reads(running())))
    await wait_for_thread(slow.started)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    # The claim is on its way still: a read here meanwhile does not claim again, and
    # its dialog stops its timer.
    again = await follows.read("native", 7, SUBJECT, reads(running()))
    assert again.followed is True
    assert seen == []
    slow.go_on.set()
    for _ in range(500):
        if seen and not follows._claiming:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the cancelled claim did not settle")


async def test_a_read_cancelled_while_it_claims_releases_the_claim() -> None:
    """#6162: the claim's thread takes the lock after its read was cancelled. No follow
    uses it, so it is released, and ``progress`` makes the dialog that was told
    ``followed`` meanwhile read again, which follows the slice."""
    bus, seen = watching()
    shared = SharedClaims()
    slow = SlowClaims(shared, "one")
    follows = PreviewFollows(bus, claims=slow, interval=3600)
    await cancel_a_claim_and_read_again(follows, slow, seen)
    assert shared.held == {}
    assert follows.following == 0
    assert seen[-1] == PreviewSliceEvent(
        kind="preview_slice.progress", slicer="native", job_id=7, id=seen[-1].id, at=seen[-1].at
    )
    # The read that event causes follows the slice, here or on another replica.
    other = PreviewFollows(bus, claims=shared.of("two"), interval=3600)
    await other.read("native", 7, SUBJECT, reads(running()))
    assert other.following == 1
    await other.aclose()
    await follows.aclose()


async def test_a_cancelled_claim_that_fails_tells_the_dialog_to_read_again() -> None:
    bus, seen = watching()
    shared = SharedClaims()
    slow = SlowClaims(shared, "one", fails=True)
    follows = PreviewFollows(bus, claims=slow, interval=3600)
    await cancel_a_claim_and_read_again(follows, slow, seen)
    assert kinds(seen) == ["preview_slice.progress"]
    assert slow.calls == []
    again = await follows.read("native", 7, SUBJECT, reads(running()))
    assert again.followed is True
    assert follows.following == 1
    await follows.aclose()


async def test_shutdown_waits_for_a_cancelled_claim_and_releases_it() -> None:
    bus, _ = watching()
    shared = SharedClaims()
    slow = SlowClaims(shared, "one")
    follows = PreviewFollows(bus, claims=slow, interval=3600)
    reading = asyncio.create_task(follows.read("native", 7, SUBJECT, reads(running())))
    await wait_for_thread(slow.started)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    slow.go_on.set()
    await follows.aclose()
    # Released by the cancelled claim itself, before the connection closes.
    assert slow.calls == ["release", "close"]
    assert shared.held == {}


def test_a_claim_postgres_cannot_answer_follows_here() -> None:
    """The dialog still hears of its slice, at worst from two replicas."""
    claims = PgFollowClaims("postgresql://nobody@127.0.0.1:1/nothing")
    assert claims.claim("native:7:x") is True
    assert claims.open_connections() == 0
    claims.release("native:7:x")
    claims.close()


def test_after_a_failed_claim_claims_wait_before_they_connect_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6162: a connect can take `CONNECT_TIMEOUT`, under the claims' lock. During an
    outage the claims answer at once for `CONNECT_BACKOFF`, so the reads of new slices
    do not queue behind each other's connects."""
    connects: list[str] = []

    def connect(conninfo: str, **_: object) -> NoReturn:
        connects.append(conninfo)
        raise psycopg.OperationalError("connection timeout expired")

    monkeypatch.setattr(psycopg, "connect", connect)
    now = [100.0]
    claims = PgFollowClaims("postgresql://nobody@db/nothing", clock=lambda: now[0])
    assert claims.claim("native:7:x") is True
    assert claims.claim("native:8:x") is True
    now[0] += claims_module.CONNECT_BACKOFF - 1
    assert claims.claim("native:9:x") is True
    assert len(connects) == 1
    now[0] += 1
    assert claims.claim("native:9:x") is True
    assert len(connects) == 2
    assert claims.open_connections() == 0
    claims.close()


class BrokenConnection:
    """A connection that opens, then fails its statement, as one whose server went."""

    def __init__(self) -> None:
        self.closed = False

    def execute(self, *_: object) -> NoReturn:
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    def close(self) -> None:
        self.closed = True


def test_a_failed_statement_on_a_live_connection_starts_the_back_off_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failed statement drops the connection, so the next claim would connect."""
    opened: list[BrokenConnection] = []

    def connect(conninfo: str, **_: object) -> BrokenConnection:
        opened.append(BrokenConnection())
        return opened[-1]

    monkeypatch.setattr(psycopg, "connect", connect)
    now = [100.0]
    claims = PgFollowClaims("postgresql://nobody@db/nothing", clock=lambda: now[0])
    assert claims.claim("native:7:x") is True
    assert len(opened) == 1
    assert opened[0].closed
    assert claims.open_connections() == 0
    now[0] += claims_module.CONNECT_BACKOFF - 1
    assert claims.claim("native:8:x") is True
    assert len(opened) == 1
    now[0] += 1
    assert claims.claim("native:8:x") is True
    assert len(opened) == 2
    claims.close()


class ReleaseFailsConnection:
    """A connection that takes a lock, then fails the release, as one whose server went."""

    def __init__(self) -> None:
        self.closed = False

    def execute(self, sql: str, *_: object) -> ReleaseFailsConnection:
        if "unlock" in sql:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return self

    def fetchone(self) -> tuple[bool]:
        return (True,)

    def close(self) -> None:
        self.closed = True


def test_a_failed_release_starts_the_back_off_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """#6174: the failed release drops the connection, as a failed claim does, so the next
    claim waits for the back-off and does not connect under the lock at once."""
    opened: list[ReleaseFailsConnection] = []

    def connect(conninfo: str, **_: object) -> ReleaseFailsConnection:
        opened.append(ReleaseFailsConnection())
        return opened[-1]

    monkeypatch.setattr(psycopg, "connect", connect)
    now = [100.0]
    claims = PgFollowClaims("postgresql://nobody@db/nothing", clock=lambda: now[0])
    assert claims.claim("native:7:x") is True
    claims.release("native:7:x")
    assert opened[0].closed
    assert claims.open_connections() == 0
    # #6182: the dropped connection freed the lock, so a second release does not connect.
    claims.release("native:7:x")
    assert len(opened) == 1
    now[0] += claims_module.CONNECT_BACKOFF - 1
    assert claims.claim("native:8:x") is True
    assert len(opened) == 1
    now[0] += 1
    assert claims.claim("native:8:x") is True
    assert len(opened) == 2
    claims.close()


@pytest.mark.requires_postgres
def test_one_process_holds_a_slice_claim_at_a_time(pg_conninfo: str) -> None:
    one, two = PgFollowClaims(pg_conninfo), PgFollowClaims(pg_conninfo)
    try:
        assert one.claim("native:7:x") is True
        assert one.open_connections() == 1
        assert two.claim("native:7:x") is False
        assert two.claim("native:8:x") is True
        one.release("native:7:x")
        assert two.claim("native:7:x") is True
        two.close()
        # Closing a process's connection frees every slice it held. The server frees them
        # when its backend for that session ends, which can come just after the close
        # returns (#6195), so the claim is tried again for a short time.
        for _ in range(100):
            if one.claim("native:8:x"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("closing the connection did not free its slices")
        # A claim that cannot reach Postgres also answers True: the slice was claimed only
        # if the connection is still open (#6198).
        assert one.open_connections() == 1
    finally:
        one.close()
        two.close()
