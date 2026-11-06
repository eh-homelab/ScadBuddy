"""The projects worker's payload keys through the CRUD route (#5298 Task 7, plan D10):
the API answers a key sealed and makes it under the subject's lock; `RemotePayloadKeys`
opens it with the KEK and caches it as `PgPayloadKeys` does."""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import quote

import pytest

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.core.secrets import open_bytes
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.projects import BASE, RemotePayloadKeys
from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.worker import PROJECTS_CRUD_AREAS
from scadbuddy.workflows.payload_codec import (
    PgPayloadKeys,
    SubjectForgottenError,
    payload_key_context,
)
from tests.crud.crud_support import TEST_KEK, api_payload_keys, served_crud

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=4)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def api_keys(jobs: JobProjection) -> PgPayloadKeys:
    return api_payload_keys(FlowStore(jobs.pool))


@pytest.fixture
async def crud(jobs: JobProjection, api_keys: PgPayloadKeys) -> AsyncIterator[CrudClient]:
    async with served_crud(FlowStore(jobs.pool), keys=api_keys, unavailable_budget=0.3) as crud:
        yield crud


def _flow() -> str:
    return f"flow-{uuid.uuid4()}"


def _rows(jobs: JobProjection, subject: str) -> int:
    with jobs.pool.connection() as conn:
        row: Any = conn.execute(
            "SELECT count(*) AS n FROM ai_payload_keys WHERE subject = %s", (subject,)
        ).fetchone()
    return int(row["n"])


def _forget(jobs: JobProjection, subject: str) -> None:
    with jobs.pool.connection() as conn:
        conn.execute("INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)", (subject,))
        conn.execute("DELETE FROM ai_payload_keys WHERE subject = %s", (subject,))


async def _refused(crud: CrudClient, subject: str, create: bool) -> CrudRefusedError:
    with pytest.raises(CrudRefusedError) as refused:
        await crud.request(
            "POST", f"{BASE}/payload-keys/{quote(subject, safe='')}", json={"create": create}
        )
    return refused.value


def test_the_area_is_served_and_awaited() -> None:
    assert "payload-keys" in CRUD_AREAS["projects"]
    assert "payload-keys" in PROJECTS_CRUD_AREAS


async def test_two_creates_at_once_make_one_key(crud: CrudClient, jobs: JobProjection) -> None:
    subject = _flow()
    one, two = await asyncio.gather(
        RemotePayloadKeys(crud, TEST_KEK).key_for(subject, True),
        RemotePayloadKeys(crud, TEST_KEK).key_for(subject, True),
    )
    assert one == two
    assert _rows(jobs, subject) == 1
    assert await RemotePayloadKeys(crud, TEST_KEK).key_for(subject, False) == one


async def test_the_answer_is_the_key_sealed_never_open(crud: CrudClient) -> None:
    subject = _flow()
    response = await crud.request("POST", f"{BASE}/payload-keys/{subject}", json={"create": True})
    body = response.json()
    sealed = base64.b64decode(body["dek_sealed"])
    assert body["kek_id"] == TEST_KEK.id
    opened = open_bytes(TEST_KEK.key, sealed, payload_key_context(subject))
    assert sealed != opened and opened not in sealed
    assert base64.b64encode(opened).decode() not in response.text
    assert await RemotePayloadKeys(crud, TEST_KEK).key_for(subject, False) == opened


async def test_a_forgotten_subject_is_410_and_nothing_is_made(
    crud: CrudClient, jobs: JobProjection
) -> None:
    subject = _flow()
    await RemotePayloadKeys(crud, TEST_KEK).key_for(subject, True)
    _forget(jobs, subject)
    for create in (True, False):
        refused = await _refused(crud, subject, create)
        assert refused.status == 410
        assert refused.problem["reason"] == "subject-forgotten"
        with pytest.raises(SubjectForgottenError):
            await RemotePayloadKeys(crud, TEST_KEK).key_for(subject, create)
    assert _rows(jobs, subject) == 0


async def test_a_session_key_is_read_never_made(
    crud: CrudClient, jobs: JobProjection, api_keys: PgPayloadKeys
) -> None:
    session = f"session-{uuid.uuid4()}"
    refused = await _refused(crud, session, True)
    assert (refused.status, refused.problem["reason"]) == (403, "subject-refused")
    assert _rows(jobs, session) == 0
    # The agent makes a session's key; the worker only reads it.
    made = await api_keys.key_for(session, True)
    assert await RemotePayloadKeys(crud, TEST_KEK).key_for(session, False) == made


async def test_a_session_encode_reads_the_agents_key_and_asks_to_make_none(
    crud: CrudClient, api_keys: PgPayloadKeys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The codec encodes with `create` (a grant check's result in a session workflow); on
    a cache miss the worker reads the session's key, never asking to make it."""
    session = f"session-{uuid.uuid4()}"
    made = await api_keys.key_for(session, True)
    sent: list[Any] = []
    request = crud.request

    async def spy(method: str, path: str, **kwargs: Any) -> Any:
        sent.append(kwargs.get("json"))
        return await request(method, path, **kwargs)

    monkeypatch.setattr(crud, "request", spy)
    keys = RemotePayloadKeys(crud, TEST_KEK)
    assert await keys.key_for(session, True) == made
    keys._cache.clear()  # expired or evicted
    assert await keys.key_for(session, True) == made
    assert sent == [{"create": False}, {"create": False}]


#: A fixed id: xdist workers must collect the same parameters.
ID = "0b0e2a0c-1111-4222-8333-444455556666"


@pytest.mark.parametrize(
    "subject",
    [
        "flow-not-a-uuid",
        f"render-{ID}",
        f"flow-{ID}-call-1",
        f"bgtask-{ID}-0000abcd",
        f"bridge-{ID}",
        f"flow-{ID}\n",
        f"session-{ID}\n",
    ],
)
async def test_any_other_subject_is_refused_for_good(crud: CrudClient, subject: str) -> None:
    for create in (True, False):
        refused = await _refused(crud, subject, create)
        assert (refused.status, refused.problem["reason"]) == (403, "subject-refused")


async def test_a_key_is_held_30_s_and_a_forgotten_subject_never(
    crud: CrudClient, jobs: JobProjection
) -> None:
    keys = RemotePayloadKeys(crud, TEST_KEK)
    held = _flow()
    key = await keys.key_for(held, True)
    _forget(jobs, held)
    # As `PgPayloadKeys`: another process can open a subject for 30 s after a forget.
    assert await keys.key_for(held, False) == key
    gone = _flow()
    _forget(jobs, gone)
    with pytest.raises(SubjectForgottenError):
        await keys.key_for(gone, False)
    with jobs.pool.connection() as conn:
        conn.execute("DELETE FROM ai_forgotten_subjects WHERE subject = %s", (gone,))
    assert await keys.key_for(gone, True) != key
    assert "\\x" not in repr(keys) and str(key) not in repr(keys)
