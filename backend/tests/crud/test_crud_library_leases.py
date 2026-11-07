"""The render worker's library leases, pin holds and install slots through the CRUD
routes (#5298 Task 19, inventory R22-R24): `RemoteCheckoutLeases` and
`RemoteInstallSlots` over `api/crud_render.py`, beside the API's own SQL gate.

- Every write is natural: the worker chooses the token, and the same token answers the
  same lease, hold or slot. One token never takes two slots.
- A lease or a pin's hold is inserted on the API under `REMOVAL_LOCK` shared, the key the
  SQL path takes, so the API's removal, an older worker on SQL and the routes exclude
  each other. A take that waits on a removal keeps waiting, as the SQL take did: the
  route answers a 423 ``removal-running`` after `LEASE_LOCK_WAIT`, and the client sends
  it again.
- A renew or release names its token; a slot held by another token is a 409
  ``not-holder``, a lease or hold with no row a 404, never a silent success. ``ttl_s``
  is at most `LEASE_TTL_MAX`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from scadbuddy.api import crud_render
from scadbuddy.crud import render as crud_module
from scadbuddy.crud.client import CrudRefusedError, CrudSyncClient
from scadbuddy.crud.render import BASE, RemoteCheckoutLeases, RemoteInstallSlots
from scadbuddy.library.libraries import (
    LEASE_TTL_MAX,
    CheckoutGate,
    CheckoutLeases,
    InstallPermits,
)
from scadbuddy.store.index import Pool
from tests.crud.crud_support import threaded_render_crud
from tests.support.store import store_pool

pytestmark = pytest.mark.requires_postgres

JOB = "a" * 32


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


@pytest.fixture
def client(pool: Pool) -> Iterator[CrudSyncClient]:
    with threaded_render_crud(pool) as blocking:
        yield blocking


def _checkout(root: Path, name: str = "BOSL2", commit: str = "c" * 40) -> Path:
    checkout = root / name / commit
    (checkout / name).mkdir(parents=True)
    return checkout


def _api(pool: Pool, root: Path) -> CheckoutGate:
    return CheckoutGate(CheckoutLeases(pool, root, poll=0.05))


def _rows(pool: Pool, table: str, token: uuid.UUID) -> int:
    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE token = %s",
            (token,),
        ).fetchone()
    assert row is not None
    return int(row["n"])


# -- leases (R22) ------------------------------------------------------------------


async def test_a_lease_taken_while_the_apis_removal_holds_the_lock_waits(
    pool: Pool,
    client: CrudSyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The removal outlasts several of the route's lock waits: the lease keeps waiting,
    as the SQL take did, and is taken once the removal ends."""
    monkeypatch.setattr(crud_render, "LEASE_LOCK_WAIT", 0.2)
    api = _api(pool, tmp_path)
    worker = CheckoutGate(RemoteCheckoutLeases(client))
    checkout = _checkout(tmp_path)
    leased, done = asyncio.Event(), asyncio.Event()

    async def render() -> None:
        async with worker.rendering(JOB, [checkout]):
            leased.set()
            await done.wait()

    with caplog.at_level(logging.INFO, logger=crud_module.__name__):
        async with api.removing():
            task = asyncio.create_task(render())
            await asyncio.sleep(1.0)
            # Not taken while the removal runs: it would have checked for leases already.
            assert not leased.is_set()
            assert not task.done()
        await asyncio.wait_for(leased.wait(), 10)
    assert any(
        record.getMessage() == "a library take waits for a removal on the API"
        for record in caplog.records
    )
    assert await asyncio.to_thread(api.leased, checkout) == [JOB]
    done.set()
    await asyncio.wait_for(task, 10)
    assert await asyncio.to_thread(api.leased, checkout) == []


def _expires(pool: Pool, token: uuid.UUID) -> datetime:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT max(expires_at) AS at FROM library_leases WHERE token = %s", (token,)
        ).fetchone()
    assert row is not None and row["at"] is not None
    return cast(datetime, row["at"])


def test_a_renew_moves_the_leases_expiry(
    pool: Pool, client: CrudSyncClient, tmp_path: Path
) -> None:
    """Checked on the row, not on wall-clock timing: a renew sets a fresh TTL from the
    database's now, past the take's."""
    leases = RemoteCheckoutLeases(client, ttl=1.0)
    token = leases.take(JOB, [_checkout(tmp_path)])
    with pool.connection() as conn:
        # Its TTL spent, as if a second had passed: a renew must move it forward.
        conn.execute(
            "UPDATE library_leases SET expires_at = expires_at - interval '1 second'"
            " WHERE token = %s",
            (token,),
        )
    spent = _expires(pool, token)
    leases.renew(token)
    assert _expires(pool, token) >= spent + timedelta(seconds=1)


async def test_a_rendering_gate_renews_its_lease(
    pool: Pool, client: CrudSyncClient, tmp_path: Path
) -> None:
    """The gate renews through the routes while the render runs: waited for on the row,
    with a wide margin, not at a wall-clock time."""
    worker = CheckoutGate(RemoteCheckoutLeases(client, ttl=1.0))

    def expiry() -> datetime:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT max(expires_at) AS at FROM library_leases WHERE holder = %s", (JOB,)
            ).fetchone()
        assert row is not None and row["at"] is not None
        return cast(datetime, row["at"])

    async with worker.rendering(JOB, [_checkout(tmp_path)]):
        first = await asyncio.to_thread(expiry)
        async with asyncio.timeout(20):
            while await asyncio.to_thread(expiry) <= first:
                await asyncio.sleep(0.05)


async def test_a_lease_never_renewed_lapses(
    pool: Pool, client: CrudSyncClient, tmp_path: Path
) -> None:
    api = _api(pool, tmp_path)
    checkout = _checkout(tmp_path)
    RemoteCheckoutLeases(client, ttl=0.5).take(JOB, [checkout])
    assert await asyncio.to_thread(api.leased, checkout) == [JOB]
    await asyncio.sleep(1.0)
    assert await asyncio.to_thread(api.leased, checkout) == []


async def test_a_lease_holds_every_checkout_it_names(
    pool: Pool, client: CrudSyncClient, tmp_path: Path
) -> None:
    """One lease on two libraries: a row per checkout under the one token."""
    api = _api(pool, tmp_path)
    bosl, other = _checkout(tmp_path), _checkout(tmp_path, "Round-Anything", "d" * 40)
    async with CheckoutGate(RemoteCheckoutLeases(client)).rendering(JOB, [bosl, other]):
        assert await asyncio.to_thread(api.leased, bosl) == [JOB]
        assert await asyncio.to_thread(api.leased, other.parent) == [JOB]


def test_a_take_sent_again_is_the_same_lease(pool: Pool, client: CrudSyncClient) -> None:
    token = uuid.uuid4()
    body = {
        "token": str(token),
        "holder": JOB,
        "checkouts": [{"library": "BOSL2", "commit": "c" * 40}],
        "ttl_s": 60,
    }
    client.request("POST", f"{BASE}/leases", json=body)
    client.request("POST", f"{BASE}/leases", json=body)
    assert _rows(pool, "library_leases", token) == 1


def test_a_token_held_for_another_lease_is_refused(pool: Pool, client: CrudSyncClient) -> None:
    token = uuid.uuid4()
    body = {
        "token": str(token),
        "holder": JOB,
        "checkouts": [{"library": "BOSL2", "commit": "c" * 40}],
        "ttl_s": 60,
    }
    client.request("POST", f"{BASE}/leases", json=body)
    for other in ({"holder": "b" * 32}, {"checkouts": [{"library": "BOSL2", "commit": "d" * 40}]}):
        with pytest.raises(CrudRefusedError) as refused:
            client.request("POST", f"{BASE}/leases", json={**body, **other})
        assert (refused.value.status, refused.value.problem.get("reason")) == (409, "token-taken")
    assert _rows(pool, "library_leases", token) == 1


def test_a_renew_names_a_live_token_and_a_release_is_natural(client: CrudSyncClient) -> None:
    leases = RemoteCheckoutLeases(client)
    with pytest.raises(CrudRefusedError) as refused:
        leases.renew(uuid.uuid4())
    assert (refused.value.status, refused.value.problem.get("reason")) == (404, "no-lease")
    token = leases.take(JOB, [Path("/libraries/BOSL2") / ("c" * 40)])
    leases.renew(token)
    leases.drop(token)
    leases.drop(token)  # sent again after a lost answer: nothing left, a success
    with pytest.raises(CrudRefusedError):
        leases.renew(token)


def test_ttl_is_bounded(client: CrudSyncClient) -> None:
    """A stolen credential cannot pin a library for longer than `LEASE_TTL_MAX` without
    renewing it."""
    body = {
        "token": str(uuid.uuid4()),
        "holder": JOB,
        "checkouts": [{"library": "BOSL2", "commit": "c" * 40}],
    }
    for path, extra in (("/leases", body), ("/pin-holds", {"token": body["token"]})):
        for ttl in (LEASE_TTL_MAX + 1, 0):
            with pytest.raises(CrudRefusedError) as refused:
                client.request("POST", f"{BASE}{path}", json={**extra, "ttl_s": ttl})
            assert refused.value.status == 422


# -- pin holds (R23) ---------------------------------------------------------------


async def test_a_workers_pin_holds_off_the_apis_removal(
    pool: Pool, client: CrudSyncClient, tmp_path: Path
) -> None:
    api = _api(pool, tmp_path)
    worker = CheckoutGate(RemoteCheckoutLeases(client))
    removed = asyncio.Event()

    async def remove() -> None:
        async with api.removing():
            removed.set()

    async with worker.pinning():
        task = asyncio.create_task(remove())
        await asyncio.sleep(1.0)
        assert not removed.is_set()
    await asyncio.wait_for(task, 10)


async def test_a_pin_waits_out_the_apis_removal(
    pool: Pool, client: CrudSyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crud_render, "LEASE_LOCK_WAIT", 0.2)
    api = _api(pool, tmp_path)
    worker = CheckoutGate(RemoteCheckoutLeases(client))
    pinned = asyncio.Event()

    async def pin() -> None:
        async with worker.pinning():
            pinned.set()

    async with api.removing():
        task = asyncio.create_task(pin())
        await asyncio.sleep(1.0)
        assert not pinned.is_set()
    await asyncio.wait_for(task, 10)
    assert pinned.is_set()


def test_a_pin_hold_sent_again_is_one_row(pool: Pool, client: CrudSyncClient) -> None:
    token = uuid.uuid4()
    body = {"token": str(token), "ttl_s": 60}
    client.request("POST", f"{BASE}/pin-holds", json=body)
    client.request("POST", f"{BASE}/pin-holds", json=body)
    assert _rows(pool, "library_pin_holds", token) == 1
    client.request("PUT", f"{BASE}/pin-holds/{token}", json={"ttl_s": 60})
    client.request("DELETE", f"{BASE}/pin-holds/{token}")
    client.request("DELETE", f"{BASE}/pin-holds/{token}")
    assert _rows(pool, "library_pin_holds", token) == 0
    with pytest.raises(CrudRefusedError) as refused:
        client.request("PUT", f"{BASE}/pin-holds/{token}", json={"ttl_s": 60})
    assert (refused.value.status, refused.value.problem.get("reason")) == (404, "no-hold")


# -- install slots (R24) -----------------------------------------------------------


def test_one_token_never_takes_two_slots(pool: Pool, client: CrudSyncClient) -> None:
    """Sent again, one after the other or at once, a claim answers the slot its token
    holds; the API's limit (2 here) still caps the other tokens."""
    slots = RemoteInstallSlots(client)
    token = uuid.uuid4()
    with ThreadPoolExecutor(max_workers=6) as executor:
        claimed = set(executor.map(lambda _: slots.claim(token), range(6)))
    assert len(claimed) == 1 and None not in claimed
    assert slots.claim(token) in claimed
    assert _rows(pool, "library_install_slots", token) == 1
    other = slots.claim(uuid.uuid4())
    assert other is not None and other not in claimed
    assert slots.claim(uuid.uuid4()) is None


def test_a_slot_is_renewed_and_released_only_by_its_token(client: CrudSyncClient) -> None:
    slots = RemoteInstallSlots(client)
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    slot = slots.claim(mine)
    assert slot is not None
    for call in (slots.renew, slots.drop):
        with pytest.raises(CrudRefusedError) as refused:
            call(slot, theirs)
        assert (refused.value.status, refused.value.problem.get("reason")) == (409, "not-holder")
    slots.renew(slot, mine)
    slots.drop(slot, mine)
    slots.drop(slot, mine)  # sent again: the slot is free, nothing of anyone's taken
    assert slots.claim(theirs) is not None


async def test_installs_are_capped_across_the_worker_and_the_api(
    pool: Pool, client: CrudSyncClient
) -> None:
    worker = InstallPermits(1, slots=RemoteInstallSlots(client), poll=0.05)
    api = InstallPermits(2, pool)
    entered = 0
    both = threading.Event()

    async def install(permits: InstallPermits) -> None:
        nonlocal entered
        async with permits.permit():
            entered += 1
            if entered == 2:
                both.set()
            await asyncio.sleep(0.5)

    started = time.monotonic()
    # The API's limit is 2: a third waits for one of the first two.
    await asyncio.wait_for(asyncio.gather(install(worker), install(api), install(api)), 20)
    assert both.is_set() and time.monotonic() - started >= 1.0
