"""Library clones' process handling, size polling and staging sweep, none of which
needs a real git: unlike ``test_libraries``, nothing here is ``requires_git``, so a
host without git still runs them (#248)."""

from __future__ import annotations

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
