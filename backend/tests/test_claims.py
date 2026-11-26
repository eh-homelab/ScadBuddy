import hashlib
import os
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.operations.claims import ClaimStore


def test_a_claim_reads_back_what_was_put(tmp_path: Path) -> None:
    claims = ClaimStore(tmp_path)
    name = claims.put(b"cube(1);")
    assert name == hashlib.sha256(b"cube(1);").hexdigest()
    assert claims.get(name) == b"cube(1);"


def test_the_same_bytes_are_one_claim_and_putting_them_again_renews_it(tmp_path: Path) -> None:
    claims = ClaimStore(tmp_path)
    name = claims.put(b"a")
    past = time.time() - timedelta(days=2).total_seconds()
    os.utime(tmp_path / name, (past, past))
    assert claims.put(b"a") == name
    assert claims.sweep(timedelta(days=1)) == 0
    assert [p.name for p in tmp_path.iterdir()] == [name]


def test_the_sweep_removes_only_claims_past_their_age(tmp_path: Path) -> None:
    claims = ClaimStore(tmp_path)
    old, new = claims.put(b"old"), claims.put(b"new")
    past = time.time() - timedelta(days=2).total_seconds()
    os.utime(tmp_path / old, (past, past))
    assert claims.sweep(timedelta(days=1)) == 1
    assert not (tmp_path / old).exists()
    assert claims.get(new) == b"new"


def test_the_sweep_of_no_claims_is_nothing(tmp_path: Path) -> None:
    assert ClaimStore(tmp_path / "absent").sweep(timedelta(days=1)) == 0


def test_a_claim_that_was_swept_is_a_lookup_error(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        ClaimStore(tmp_path).get(hashlib.sha256(b"x").hexdigest())


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "..", "ab" * 31 + "/."])
def test_a_name_that_is_not_a_digest_is_refused(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        ClaimStore(tmp_path).get(bad)


async def test_the_housekeeping_sweep_removes_old_claims(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from types import SimpleNamespace

    from scadbuddy.batches.store import Pruned
    from scadbuddy.core.paths import DataPaths
    from scadbuddy.main import BATCH_TTL, PENDING_BATCH_TTL, _housekeeping_activities
    from scadbuddy.workflows.housekeeping import SWEEPS

    paths = DataPaths(tmp_path)
    old = ClaimStore(paths.claims).put(b"old")
    past = time.time() - timedelta(days=2).total_seconds()
    os.utime(paths.claims / old, (past, past))
    pruned: list[tuple[timedelta, timedelta]] = []

    async def prune(ttl: timedelta, *, pending_older_than: timedelta) -> Pruned:
        pruned.append((ttl, pending_older_than))
        return Pruned(count=2, pending=["b7"])

    batches = SimpleNamespace(prune=prune)
    state = SimpleNamespace(
        paths=paths,
        # #7373: no blob store, so no upload blobs to sweep.
        store=SimpleNamespace(content=None),
        components=SimpleNamespace(get=lambda key: batches),
    )
    sweeps = {fn.__temporal_activity_definition.name: fn for fn in _housekeeping_activities(state)}  # type: ignore[arg-type, attr-defined]
    await sweeps["housekeeping_sweep_claims"]()
    assert "housekeeping_sweep_claims" in SWEEPS
    assert not (paths.claims / old).exists()
    # #2718: the same sweep forgets batch prints left untouched; #3798: one that still
    # followed a pending file is kept longer, and named when it goes.
    assert pruned == [(BATCH_TTL, PENDING_BATCH_TTL)]
    named = [r for r in caplog.records if r.getMessage().startswith("forgot batch prints with")]
    assert [getattr(r, "batches", None) for r in named] == [["b7"]]


def test_a_put_survives_the_sweep_removing_the_claim_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 3c 1.4: the sweep may unlink a claim while a put renews it; the put
    holds the claim all the same, and never fails over it."""
    claims = ClaimStore(tmp_path)
    name = claims.put(b"a")

    def swept_first(real: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            (tmp_path / name).unlink(missing_ok=True)
            return real(*args, **kwargs)

        return call

    monkeypatch.setattr(os, "utime", swept_first(os.utime))
    monkeypatch.setattr(os, "replace", swept_first(os.replace))
    assert claims.put(b"a") == name
    assert claims.get(name) == b"a"


@pytest.fixture
def puts() -> Iterator[list[threading.Thread]]:
    """The puts `_put_waits_on_the_lock` starts, joined at teardown whether or not the
    test passed, so none runs on into the next test (#7966)."""
    started: list[threading.Thread] = []
    yield started
    for put in started:
        put.join(timeout=5)


def _put_waits_on_the_lock(
    claims: ClaimStore,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    puts: list[threading.Thread],
) -> list[bool]:
    """Patch ``os.stat`` so that the first stat of the claim ``name`` (the check of a
    release or the sweep) starts a put of its bytes in a thread, and records whether that
    put was still waiting on the store's lock when the check went on (#7947)."""
    stat = os.stat
    waited: list[bool] = []

    def stat_with_a_put(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if Path(path).name == name and not waited:
            put = threading.Thread(target=claims.put, args=(b"a",))
            put.start()
            puts.append(put)
            put.join(timeout=0.3)
            waited.append(put.is_alive())
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat_with_a_put)
    return waited


def test_a_put_waits_while_the_sweep_checks_and_removes_a_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, puts: list[threading.Thread]
) -> None:
    """#7947 (review 3c 1.4): a put of an old claim's bytes cannot come between the
    sweep's check and its removal; it waits on the lock, then writes the claim anew."""
    claims = ClaimStore(tmp_path / "claims")
    name = claims.put(b"a")
    past = time.time() - timedelta(days=2).total_seconds()
    os.utime(tmp_path / "claims" / name, (past, past))
    waited = _put_waits_on_the_lock(claims, name, monkeypatch, puts)
    assert claims.sweep(timedelta(days=1)) == 1
    for put in puts:
        put.join(timeout=5)
    assert waited == [True]
    assert claims.get(name) == b"a"


def test_a_hold_says_whether_it_created_the_claim(tmp_path: Path) -> None:
    claims = ClaimStore(tmp_path)
    first, second = claims.hold(b"a"), claims.hold(b"a")
    assert first.name == second.name == claims.put(b"a")
    assert (first.created, second.created) == (True, False)


def test_release_removes_the_claim_its_hold_created(tmp_path: Path) -> None:
    claims = ClaimStore(tmp_path)
    held = claims.hold(b"a")
    claims.release(held)
    claims.release(held)
    with pytest.raises(LookupError):
        claims.get(held.name)
    assert list(tmp_path.iterdir()) == []


def test_release_keeps_a_claim_another_request_put_first(tmp_path: Path) -> None:
    """Review 3c 1.2 race: B puts the bytes before A; A did not create the claim, so
    A's release leaves it for B's run."""
    claims = ClaimStore(tmp_path)
    b = claims.hold(b"a")
    a = claims.hold(b"a")
    claims.release(a)
    assert claims.get(b.name) == b"a"


def test_release_keeps_a_claim_another_request_put_since(tmp_path: Path) -> None:
    """Review 3c 1.2 race: B puts the bytes after A; the claim is no longer the file A
    wrote, so A's release leaves it for B's run, and B's release, not the creator's,
    leaves it to the sweep."""
    claims = ClaimStore(tmp_path)
    a = claims.hold(b"a")
    b = claims.hold(b"a")
    claims.release(a)
    claims.release(b)
    assert claims.get(a.name) == b"a"
    assert [path.name for path in tmp_path.iterdir()] == [a.name]


def test_release_never_moves_a_claim_another_request_put_since(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7853: B renewed the claim A created, and B's run may be reading it. A's release
    leaves it in place: a move aside and back would leave no file for a moment, and a
    read of B's in that gap is a LookupError (two copies of one archive photo, #7232)."""
    claims = ClaimStore(tmp_path)
    a = claims.hold(b"a")
    claims.hold(b"a")
    moved: list[tuple[Any, Any]] = []
    rename = os.rename

    def recording_rename(src: Any, dst: Any) -> None:
        moved.append((src, dst))
        rename(src, dst)

    monkeypatch.setattr(os, "rename", recording_rename)
    claims.release(a)
    assert moved == []
    assert claims.get(a.name) == b"a"


def test_a_put_waits_while_a_release_checks_and_removes_its_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, puts: list[threading.Thread]
) -> None:
    """#7947: a put of the same bytes cannot come between `release`'s check (still the
    file its hold wrote) and its removal, so the claim's name is never missing while
    another put's file is there (two copies of one archive photo, #7232). The put waits
    on the lock, then writes the claim anew."""
    claims = ClaimStore(tmp_path / "claims")
    held = claims.hold(b"a")
    waited = _put_waits_on_the_lock(claims, held.name, monkeypatch, puts)
    claims.release(held)
    for put in puts:
        put.join(timeout=5)
    assert waited == [True]
    assert claims.get(held.name) == b"a"
    assert [path.name for path in (tmp_path / "claims").iterdir()] == [held.name]


def test_hold_file_moves_a_streamed_file_in_and_a_second_renews_it(tmp_path: Path) -> None:
    """A media upload is streamed to disk, never read into memory (#1054): its file
    is moved in under the digest computed while streaming."""
    claims = ClaimStore(tmp_path / "claims")
    digest = hashlib.sha256(b"video").hexdigest()
    first = tmp_path / "upload-1"
    first.write_bytes(b"video")
    held = claims.hold_file(first, digest)
    assert held.name == digest and held.created
    assert not first.exists()
    assert claims.get(digest) == b"video"
    past = time.time() - timedelta(days=2).total_seconds()
    os.utime(tmp_path / "claims" / digest, (past, past))
    second = tmp_path / "upload-2"
    second.write_bytes(b"video")
    again = claims.hold_file(second, digest)
    assert again.name == digest and not again.created
    assert not second.exists()
    assert claims.sweep(timedelta(days=1)) == 0
    # The first put's release keeps the claim the second renewed.
    claims.release(held)
    assert claims.get(digest) == b"video"
    assert sorted(p.name for p in (tmp_path / "claims").iterdir()) == [digest]
