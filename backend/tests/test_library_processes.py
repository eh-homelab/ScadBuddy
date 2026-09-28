"""Library clones' process handling, size polling and staging sweep, none of which
needs a real git: unlike ``test_libraries``, nothing here is ``requires_git``, so a
host without git still runs them (#248)."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.libraries import (
    CLONE_TIMEOUT,
    KILL_WAIT,
    STAGING_MAX_AGE_MARGIN,
    STAGING_PREFIX,
    LibraryStore,
    LibraryTooLargeError,
    _tree_size,
)


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path / "data")
    data.ensure()
    return data


def _running(pid: int) -> bool:
    """A killed orphan whose new parent never reaps it (pytest as PID 1 in the test
    image) stays a zombie: dead, but still answering `kill(pid, 0)`."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:  # gone, or going: ENOENT or ESRCH mid-read
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _age(path: Path) -> None:
    """Make a staging clone old enough for the boot sweep to take it."""
    old = time.time() - CLONE_TIMEOUT - STAGING_MAX_AGE_MARGIN - 60
    os.utime(path, (old, old))


class Unreapable:
    """A killed git stuck in the kernel: never reaped within KILL_WAIT."""

    pid = 0
    args = ("git",)

    def __init__(self) -> None:
        self.waits: list[float | None] = []
        self.reaped = threading.Event()

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.waits.append(timeout)
        raise subprocess.TimeoutExpired("git", timeout or 0)

    def wait(self, timeout: float | None = None) -> int:
        self.reaped.set()
        return -9


def test_a_killed_git_that_is_never_reaped_does_not_hold_up_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scadbuddy.library.libraries.os.killpg", lambda pid, sig: None)
    process = Unreapable()

    LibraryStore._kill(process)  # type: ignore[arg-type]

    assert process.waits == [KILL_WAIT]


def test_a_killed_git_that_is_not_reaped_in_time_is_reaped_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rather than never: it would stay a zombie, one per refused clone (#247)."""
    monkeypatch.setattr("scadbuddy.library.libraries.os.killpg", lambda pid, sig: None)
    process = Unreapable()

    LibraryStore._kill(process)  # type: ignore[arg-type]

    assert process.reaped.wait(timeout=5)


def test_a_killed_git_handed_to_the_reaper_is_logged_by_pid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """So a git stuck in the kernel is visible, not just quietly waited on (#399)."""
    monkeypatch.setattr("scadbuddy.library.libraries.os.killpg", lambda pid, sig: None)
    process = Unreapable()
    process.pid = 4242

    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.libraries"):
        LibraryStore._kill(process)  # type: ignore[arg-type]

    [record] = [r for r in caplog.records if "reaper" in r.getMessage()]
    assert "pid 4242" in record.getMessage()
    assert record.__dict__["pid"] == 4242


def test_a_clone_is_killed_while_it_runs_once_it_goes_over_the_size_cap(
    paths: DataPaths, tmp_path: Path
) -> None:
    """The cap stops the clone mid-transfer: an oversized repository never gets to
    write its whole tree to the volume before it is refused."""
    child_pid = tmp_path / "child.pid"
    fake_git = tmp_path / "git"
    # A clone that never finishes: a child keeps appending to the checkout.
    fake_git.write_text(
        "#!/bin/sh\n"
        'for last; do :; done\nmkdir -p "$last"\n'
        '(while :; do head -c 65536 /dev/zero >> "$last/blob"; sleep 0.01; done) &\n'
        f"echo $! > {child_pid}\nwait\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    store = LibraryStore(paths, catalogue=(), git=str(fake_git), timeout=60, max_bytes=1_000_000)

    started = time.monotonic()
    with pytest.raises(LibraryTooLargeError, match=r"reached \d+ MB, over the 1 MB"):
        store._clone("Big", "https://git.example/o/big.git", "main")

    assert time.monotonic() - started < 30
    assert list(paths.libraries.iterdir()) == []
    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _running(pid):
            break
        time.sleep(0.05)
    else:
        pytest.fail("the oversized clone's writer is still running")


def test_checking_out_a_refetched_commit_is_held_to_the_size_cap(
    paths: DataPaths, tmp_path: Path
) -> None:
    """The pinned ref moved on, so the commit is fetched and checked out: that
    checkout writes the tree and is killed once it goes over the cap, as the clone is."""
    child_pid = tmp_path / "child.pid"
    fake_git = tmp_path / "git"
    # clone and fetch succeed; rev-parse names another commit; checkout never ends.
    fake_git.write_text(
        "#!/bin/sh\n"
        'for arg; do case "$arg" in\n'
        '  clone) for last; do :; done; mkdir -p "$last"; exit 0;;\n'
        f"  rev-parse) echo {'b' * 40}; exit 0;;\n"
        "  fetch) exit 0;;\n"
        "  checkout) break;;\n"
        "esac; done\n"
        'while [ "$1" != -C ]; do shift; done\n'
        '(while :; do head -c 65536 /dev/zero >> "$2/blob"; sleep 0.01; done) &\n'
        f"echo $! > {child_pid}\nwait\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    store = LibraryStore(paths, catalogue=(), git=str(fake_git), timeout=60, max_bytes=1_000_000)

    started = time.monotonic()
    with pytest.raises(LibraryTooLargeError, match=r"reached \d+ MB, over the 1 MB"):
        store._clone("Big", "https://git.example/o/big.git", "main", commit="a" * 40)

    assert time.monotonic() - started < 30
    assert list(paths.libraries.iterdir()) == []
    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _running(pid):
            break
        time.sleep(0.05)
    else:
        pytest.fail("the oversized checkout's writer is still running")


def test_measuring_a_clone_stops_once_it_is_past_the_cap(tmp_path: Path) -> None:
    """A tree already too large is not walked to the end: the poll that finds it
    over kills the clone that much sooner (#247)."""
    for index in range(10):
        (tmp_path / f"part{index}").write_bytes(b"x" * 10)

    assert _tree_size(tmp_path) == 100
    assert _tree_size(tmp_path, limit=15) == 20


def test_sweep_staging_leaves_a_clone_another_replica_may_be_writing(
    paths: DataPaths,
) -> None:
    """Replicas share /data: a staging clone younger than the clone timeout plus a
    margin may be another replica's install in flight (#375)."""
    store = LibraryStore(paths, catalogue=())
    live = paths.libraries / f"{STAGING_PREFIX}live" / "BOSL2"
    live.mkdir(parents=True)
    dead = paths.libraries / f"{STAGING_PREFIX}dead"
    (dead / "BOSL2").mkdir(parents=True)
    _age(dead)

    assert store.sweep_staging() == [dead.name]
    assert live.is_dir()


def _checkout(paths: DataPaths, name: str, commit: str, *, old: bool = True) -> Path:
    checkout = paths.libraries / name / commit
    (checkout / name).mkdir(parents=True)
    if old:
        _age(checkout)
    return checkout


def test_sweep_checkouts_removes_only_what_keep_refuses(paths: DataPaths) -> None:
    kept = _checkout(paths, "BOSL2", "a" * 40)
    unpinned = _checkout(paths, "BOSL2", "b" * 40)
    other = _checkout(paths, "MCAD", "c" * 40)
    store = LibraryStore(paths, catalogue=())

    removed = store.sweep_checkouts(lambda name, commit: commit == "a" * 40)

    assert removed == [f"BOSL2@{'b' * 40}", f"MCAD@{'c' * 40}"]
    assert kept.is_dir()
    assert not unpinned.exists()
    assert not other.exists()
    assert not (paths.libraries / "MCAD").exists()


def test_sweep_checkouts_leaves_one_another_replica_may_not_have_recorded_yet(
    paths: DataPaths,
) -> None:
    """A clone moved into place moments ago on another replica sharing /data is not
    in any model.json until its pin is recorded: the same age guard as staging."""
    fresh = _checkout(paths, "BOSL2", "a" * 40, old=False)
    store = LibraryStore(paths, catalogue=())

    assert store.sweep_checkouts(lambda name, commit: False) == []
    assert fresh.is_dir()


def test_sweep_checkouts_keeps_one_it_cannot_decide_about(
    paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    checkout = _checkout(paths, "BOSL2", "a" * 40)
    store = LibraryStore(paths, catalogue=())

    def keep(name: str, commit: str) -> bool:
        raise OSError("EIO")

    assert store.sweep_checkouts(keep) == []
    assert checkout.is_dir()
    assert "could not sweep a library checkout" in caplog.text
