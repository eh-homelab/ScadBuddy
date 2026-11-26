"""Forgetting a durable subject behind a CRUD route (#5299 step 6): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`), area
``forget``. The route is one transaction (D2); the SQL is ``agent_data/forget.py``. The
agent's store (``agent/src/sessions/forget.ts`` ``CrudSubjectKeys``) calls it. The area's
other routes (a browser call's key dropped, the key sweep, a flow run's images) are Go's
(``tools/go/core/internal/forget``, #7667). This one stays here: it opens transcripts under
the KEK.

- ``POST …/forgotten-subjects/{subject}``: forgetSubject's key transaction, under the
  subject's lock: the tombstone, a flow run's images and the key, a session's forks first
  taking their ranges (``transcripts.py`` ``copy_fork_ranges``, #5299 step 9, part c). With no
  KEK and a fork that takes transcript entries it is 409 ``forks-take-ranges``: the agent
  copies its forks' ranges and forgets it in its own transaction.

``DELETE /workflow-runs/{id}`` forgets a run with the same statements (``flows/forget.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import Path, status
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.agent_data import forget
from scadbuddy.agent_data.payload_keys import SUBJECT_PATTERN, KeyNotHeldError
from scadbuddy.agent_data.rotation import kek_at
from scadbuddy.agent_data.workspace import Keks
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

Pool = ConnectionPool[Connection[DictRow]]

T = TypeVar("T")

#: A session whose forks take transcript entries, and no KEK to copy them with.
FORKS_TAKE_RANGES = "forks-take-ranges"


def _pool(state: Any) -> Pool:
    pool: Pool = state.settings_store.pool
    return pool


async def _in_conn[T](state: Any, fn: Callable[[Any], T]) -> T:
    def run() -> T:
        with _pool(state).connection() as conn:
            return fn(conn)

    return await asyncio.to_thread(run)


class Forgotten(BaseModel):
    #: Whether the subject had a key, now deleted.
    key: bool


@router.post("/forgotten-subjects/{subject}", name="crud_agent_forget_subject")
async def forget_subject(
    subject: Annotated[str, Path(pattern=SUBJECT_PATTERN)], state: StateDep
) -> Forgotten:
    if subject.startswith("bridge-"):
        raise ApiError(422, "a browser call's key is dropped, not forgotten")
    try:
        keys = Keks(
            kek_at(state.settings.secret_key_file),
            kek_at(state.settings.secret_key_previous_file),
        )
        key = await _in_conn(state, lambda conn: forget.forget(conn, subject, keys))
    except forget.ForksTakeRangesError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{subject} has forks, which take their ranges of its transcript first, and the API"
            " has no SCADBUDDY_SECRET_KEY_FILE.",
            reason=FORKS_TAKE_RANGES,
        ) from None
    except KeyNotHeldError as err:
        # A transcript key sealed under a KEK the API does not hold: nothing is forgotten.
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, str(err), reason="unavailable"
        ) from None
    return Forgotten(key=key)
