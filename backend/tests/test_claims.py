import hashlib
import os
import time
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


async def test_the_housekeeping_sweep_removes_old_claims(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from scadbuddy.core.paths import DataPaths
    from scadbuddy.main import _housekeeping_activities
    from scadbuddy.workflows.housekeeping import SWEEPS

    paths = DataPaths(tmp_path)
    old = ClaimStore(paths.claims).put(b"old")
    past = time.time() - timedelta(days=2).total_seconds()
    os.utime(paths.claims / old, (past, past))
    state = SimpleNamespace(paths=paths)
    sweeps = {fn.__temporal_activity_definition.name: fn for fn in _housekeeping_activities(state)}  # type: ignore[arg-type, attr-defined]
    await sweeps["housekeeping_sweep_claims"]()
    assert "housekeeping_sweep_claims" in SWEEPS
    assert not (paths.claims / old).exists()


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


def test_a_claim_renewed_while_the_sweep_looks_at_it_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 3c 1.4: a put between the sweep's stat and its removal keeps the claim."""
    claims = ClaimStore(tmp_path)
    name = claims.put(b"a")
    past = time.time() - timedelta(days=2).total_seconds()
    os.utime(tmp_path / name, (past, past))
    stat = Path.stat
    renewed = False

    def stat_then_renew(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal renewed
        result = stat(self, *args, **kwargs)
        if self.name == name and not renewed:
            renewed = True
            claims.put(b"a")
        return result

    monkeypatch.setattr(Path, "stat", stat_then_renew)
    assert claims.sweep(timedelta(days=1)) == 0
    assert renewed
    assert claims.get(name) == b"a"


def test_drop_removes_a_claim_and_tolerates_one_already_gone(tmp_path: Path) -> None:
    claims = ClaimStore(tmp_path)
    name = claims.put(b"a")
    claims.drop(name)
    claims.drop(name)
    with pytest.raises(LookupError):
        claims.get(name)
