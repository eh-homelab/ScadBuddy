# Activity (background work by principal) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One indexed, filterable record of every long-running thing (renders, operations,
print runs, agent operations), who started it, and what it made, shown on an Activity
page with live updates and Simple/Advanced detail, and readable by each principal over
`/mcp`.

**Architecture:** A backend `activity` table, written by each source at the one place it
already announces a change (`_announce(conn, …)` / `publish_in`), in the same
transaction. Kinds are discovered from `scadbuddy/<feature>/activity.py` (`ACTIVITY_KINDS`)
plus the agent's `ai_activity_kinds` rows. The UI follows an `activity` topic on the
existing realtime socket and re-reads REST. The agent's `activity_*` tools pin the
caller's principal unless it holds `outward`.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Temporal (backend); TypeScript, Hono,
postgres.js, zod (agent); React 19, Vite, msw, vitest, Playwright (frontend).

**Spec:** `docs/superpowers/specs/2026-10-09-principal-jobs-design.md` (#1956, #1960).

## Deviations from the spec (decided while planning; read before Task 1)

1. **Name: `activity`, not `jobs`.** `/api/v1/jobs/{job_id}`, the `Job`/`JobStatus` types,
   `JobEvent` and `job.*` kinds already mean *render jobs*. The feature is `activity`:
   table `activity`, routes `/api/v1/activity…`, `ACTIVITY_KINDS`, `ActivityEvent`, topic
   `activity`, agent table `ai_activity_kinds`, tools `activity_list`/`activity_get`/
   `activity_watch`. The spec's "job" below means one activity row.
2. **Live updates ride `WS /api/v1/ws`, not a new SSE route.** That socket is the UI's one
   live channel (`api/realtime.py`, `frontend/src/lib/realtime.ts`): ids only, then a REST
   re-read. `Last-Event-ID` replay is not needed: on `subscribed`/`resync` the page
   re-reads. `activity_watch` polls the REST route.
3. **Attribution at the route, status at the writer.** Renders and print runs carry no
   author through their workflows. The route that started one already has
   `current_author()` and the returned id, so it calls `ActivityIndex.attribute(...)`;
   the writers never touch `principal`/`session`/`via_tool` (an upsert keeps them). An
   operation already carries `OperationInput.author`, so its writer fills them directly.
4. **Agent operations over the existing bus.** The agent publishes
   `{"kind": "ai_operation.changed", "operation_id": …}` on `scadbuddy_events` (its
   existing `PG_CHANNEL`) inside the insert/finish transaction. The backend declares the
   event (as it does `SessionBusEvent`) and its activity component ingests the
   `ai_operations` row. No new channel and no `LISTEN` grant.
5. **`via_tool` travels as a header.** `agent/src/tools/authorship.ts` `authorHeaders`
   adds `X-ScadBuddy-Agent-Tool: <tool name>`; `core/authorship.py` reads it into
   `AgentAuthor.tool`.

## Global Constraints

- Statuses, exactly: `queued`, `running`, `waiting`, `succeeded`, `failed`, `cancelled`.
- Principal values: the `X-ScadBuddy-Agent-Author` value as received (the agent's
  `principalHeader(id)`: percent-encoded, ≤300 chars); `browser` when absent; `system` for
  schedules. A headless-browser request (`X-ScadBuddy-Agent-Session`) is the session
  owner's principal: `browser` when `ai_sessions.owner_kind = 'browser'`, else
  `ai_sessions.owner_id`.
- Agent kind keys start with `agent.`; a code-registered key starting with `agent.` fails
  discovery.
- Every error is an RFC 9457 problem (`core/problems.py` `ApiError`); an unknown query
  parameter or undeclared facet answers **400** (FastAPI ignores unknown params; check
  them explicitly).
- Backend migrations: new file `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`;
  agent migrations: `agent/src/db/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`, tables
  `ai_`-prefixed. Never edit a merged migration.
- Stored settings (`library/settings_store.py`, read fresh each use):
  `activity_ttl_seconds` (default 2592000 = 30 d, min 86400),
  `activity_repair_window_seconds` (default 86400), `activity_follow` (list of principals,
  default `["browser"]`).
- The backend reads `ai_*` tables only with `SELECT`, and treats a missing table
  (`psycopg.errors.UndefinedTable`) as "no agent": empty, never an error.
- A failed activity write never fails its source's write: log, count
  (`scadbuddy_activity_write_failures`), and leave it to the repair pass.
- Workflow code changes go behind `workflow.patched(...)` (backend) / `patched(...)`
  (agent) with a replay test.
- Never commit `backend/openapi.json` or either `schema.d.ts`.

## Review Focus

1. **A render coalesced onto another principal's render.** Expected: the row keeps the
   first principal; the joiner gets the same id back. Pin it: Task 4 test
   `test_coalesced_render_keeps_first_principal`.
2. **The agent's tables are absent (agent not deployed) or Postgres lacks `ai_sessions`.**
   Expected: `/activity` lists backend kinds, no 500; a headless-marker request with no
   `ai_sessions` row is pinned to an impossible principal (sees nothing), not everything.
   Pin it: Task 7 tests `test_facets_without_agent_tables`, `test_unknown_session_sees_nothing`.
3. **A facet filter on a kind that does not declare it, or a misspelled param
   (`stauts=failed`).** Expected: 400 naming it, not an unfiltered list. Pin it: Task 7
   `test_unknown_param_is_400`.
4. **An activity write fails mid-source-transaction (e.g. a constraint).** Expected: the
   source's row still commits. Pin it: Task 2 `test_upsert_failure_does_not_abort_source`
   (the upsert runs under a `SAVEPOINT`).
5. **A principal with a `%`-encoded id filters its own activity over `/mcp`.** Expected:
   it matches, because the tool filters by `principalHeader(id)`, the stored form. Pin
   it: Task 11 `activity_list pins the encoded principal`.

---

## File Structure

Backend (`backend/scadbuddy/`):
- `activity/__init__.py` — empty; makes `activity` a feature package.
- `activity/models.py` — `ActivityStatus`, `ActivityRow`, `Facet`, `ActivityKind`.
- `activity/index.py` — `ActivityIndex`: `upsert_in`, `attribute`, `list`, `get`,
  `facets`, `prune`, `ingest_agent_operation`.
- `activity/kinds.py` — discovery (`ACTIVITY_KINDS` via `feature_exports`), agent kinds
  from `ai_activity_kinds`, `KindRegistry`.
- `activity/component.py` — `ACTIVITY` component; `run` subscribes the bus for
  `ai_operation.changed`.
- `activity/visibility.py` — `pinned_principal(request, pool)`.
- `render/activity.py`, `operations/activity.py`, `bambuddy/activity.py` — each source's
  `ACTIVITY_KINDS` and its `row_of(...)` mapping.
- `api/activity.py` — routes.
- `migrations/<ts>_activity.sql`.
- Modified: `core/events.py` (`ActivityEvent`, `AgentOperationEvent`), `api/realtime.py`
  (topics), `core/authorship.py` (`tool`), `library/settings_store.py`, `core/metrics.py`,
  `render/projection.py`, `operations/store.py`, `bambuddy/runs.py`,
  `workflows/operation_activities.py`, `workflows/housekeeping.py`, the render and print
  routes, `api/operations.py` (`Job-Location`→`Activity-Location`).

Agent (`agent/src/`):
- `db/migrations/<ts>_activity.sql` — `ai_operations` columns, `ai_activity_kinds`.
- `activity/kinds.ts` — `upsertActivityKinds(sql, kinds)`.
- `tools/activity.ts` — `activityTools`.
- Modified: `tools/authorship.ts`, `operations/store.ts`, `temporal/names.ts`,
  `temporal/operationActivities.ts`, `operations/run.ts`, `routes/pluginPackages.ts`,
  `tools/index.ts`, `main.ts`.

Frontend (`frontend/src/`):
- `pages/ActivityPage.tsx`, `components/activity/{ActivityFilters,ActivityTable,ActivityDrawer,ActivityBell}.tsx`,
  `lib/activityQuery.ts`, `lib/advanced.ts`, `mocks/features/activity.ts`, tests beside
  each, `e2e/activity.spec.ts`.
- Modified: `App.tsx`, `components/AppShell.tsx`, `api/client.ts`, `api/types.ts`.

---

### Task 1: The record, its event, and its topic

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_activity.sql`, `backend/scadbuddy/activity/__init__.py`, `backend/scadbuddy/activity/models.py`
- Modify: `backend/scadbuddy/core/events.py`, `backend/scadbuddy/api/realtime.py`
- Test: `backend/tests/test_activity_index.py`, `backend/tests/test_pg_migrations.py` (existing; no change expected)

**Interfaces:**
- Produces: table `activity`; `ActivityStatus = Literal["queued","running","waiting","succeeded","failed","cancelled"]`; `ActivityRow` (pydantic); `ActivityEvent(kind="activity.changed", activity_id: str, principal: str | None)`; `AgentOperationEvent(kind="ai_operation.changed", operation_id: str)`; realtime topics `activity`, `activity:<id>`.

- [ ] **Step 1: Write the migration**

```sql
-- Activity (spec 2026-10-09 principal jobs, plan deviation 1): one row per long-running
-- thing, written by its source at the place it announces a change. `id` is
-- `<source>:<source id>`; `principal`, `session` and `via_tool` are set once by the
-- route that started it and never overwritten by a status write.
CREATE TABLE activity (
    id text PRIMARY KEY,
    kind text NOT NULL,
    principal text,
    session text,
    via_tool text,
    subject text NOT NULL DEFAULT '',
    status text NOT NULL CHECK (status IN ('queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled')),
    title text NOT NULL,
    parent_id text,
    facets jsonb NOT NULL DEFAULT '{}'::jsonb,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    source text NOT NULL,
    source_id text NOT NULL,
    workflow_id text,
    run_id text,
    traceparent text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CHECK ((status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL))
);
CREATE INDEX activity_recent ON activity (created_at DESC, id DESC);
CREATE INDEX activity_principal ON activity (principal, created_at DESC);
CREATE INDEX activity_kind ON activity (kind, created_at DESC);
CREATE INDEX activity_open ON activity (status) WHERE finished_at IS NULL;
CREATE INDEX activity_parent ON activity (parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX activity_facets ON activity USING gin (facets jsonb_path_ops);
```

`detail` holds what a source with no row of its own to read back keeps (agent operations:
request, result, error).

- [ ] **Step 2: Write `activity/models.py`**

```python
"""What an activity row is (spec 2026-10-09 §3) and what a kind declares (§4)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

ActivityStatus = Literal["queued", "running", "waiting", "succeeded", "failed", "cancelled"]
STATUSES: tuple[ActivityStatus, ...] = (
    "queued", "running", "waiting", "succeeded", "failed", "cancelled",
)
FINISHED: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})
FacetType = Literal["keyword", "number", "bool", "time"]


class ActivityRow(BaseModel):
    id: str
    kind: str
    principal: str | None = None
    session: str | None = None
    via_tool: str | None = None
    subject: str = ""
    status: ActivityStatus
    title: str
    parent_id: str | None = None
    facets: dict[str, Any] = {}
    detail: dict[str, Any] = {}
    source: str
    source_id: str
    workflow_id: str | None = None
    run_id: str | None = None
    traceparent: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class Facet:
    name: str
    type: FacetType
    label: str
    filterable: bool = True


@dataclass(frozen=True)
class ActivityLink:
    rel: str
    label: str
    href: str


@dataclass(frozen=True)
class ActivityKind:
    key: str
    label: str
    facets: tuple[Facet, ...] = ()
    #: Stages, request, result, error, read from the source; `row.detail` when None.
    detail: Callable[[ActivityRow], Mapping[str, Any]] | None = None
    links: Callable[[ActivityRow], list[ActivityLink]] = field(default=lambda row: [])
```

- [ ] **Step 3: Add the events** — in `core/events.py`, after `OperationEvent`:

```python
class ActivityEvent(BaseEvent):
    """An activity row changed (spec 2026-10-09): re-read ``GET /activity/{id}``."""

    kind: Literal["activity.changed"] = "activity.changed"
    activity_id: str
    principal: str | None = None


class AgentOperationEvent(BaseEvent):
    """The agent recorded or ended an ``ai_operations`` row; the agent publishes it
    itself (``agent/src/operations/store.ts``). The activity component ingests it; it
    goes to no WebSocket topic."""

    kind: Literal["ai_operation.changed"] = "ai_operation.changed"
    operation_id: str
```

Add both to the `Event` union (before `BusResync`).

- [ ] **Step 4: Route the topic** — in `api/realtime.py` `topics_of`, add before the
  fallthrough:

```python
        case ActivityEvent():
            return ["activity", f"activity:{event.activity_id}"]
        case AgentOperationEvent():
            return []
```

(import both beside `OperationEvent`).

  `topics_of` alone is not enough: the socket refuses a subscribe whose topic fails
  `valid_topic` (`_TOPIC`, same file). Add `"activity"` to `COLLECTION_TOPICS` and, in
  `_TOPIC`'s list, the per-row topic bounded to the real id shapes:

```python
            # An activity row (spec 2026-10-09): ``activity:<source>:<source id>``.
            r"activity:(?:render|operation|print_run|agent_operation):[A-Za-z0-9_-]{1,64}",
```

- [ ] **Step 5: Test the event round trip and the topic**

```python
"""Activity (spec 2026-10-09): the record, its event and topic."""

from scadbuddy.api.realtime import topics_of, valid_topic
from scadbuddy.core.events import ActivityEvent, AgentOperationEvent, decode_event, encode_event


def test_activity_event_round_trips_and_routes() -> None:
    event = ActivityEvent(activity_id="render:abc", principal="browser")
    assert decode_event(encode_event(event)) == event
    assert topics_of(event) == ["activity", "activity:render:abc"]


def test_activity_topics_are_valid() -> None:
    assert valid_topic("activity")
    assert valid_topic("activity:render:abc123")
    assert valid_topic("activity:agent_operation:3f2c9a10-1b2c-4d5e-8f90-123456789abc")
    assert not valid_topic("activity:nope:x")
    assert not valid_topic("activity:render:")


def test_agent_operation_event_decodes_and_goes_nowhere() -> None:
    payload = '{"id":"e1","at":"2026-10-09T00:00:00Z","kind":"ai_operation.changed","operation_id":"x"}'
    event = decode_event(payload)
    assert isinstance(event, AgentOperationEvent)
    assert topics_of(event) == []
```

  And a socket test in `backend/tests/api/test_realtime_activity.py`, built on the
  existing `/api/v1/ws` tests in `tests/api/test_realtime.py` (copy their client, the
  subscribe frame and the publish helper): subscribe to `activity` and to
  `activity:render:abc123`, publish `ActivityEvent(activity_id="render:abc123")` on the
  app's bus, and assert both subscriptions are acknowledged (not refused) and the
  `activity.changed` frame arrives.

- [ ] **Step 6: Run** `cd backend && uv run --frozen pytest tests/test_activity_index.py tests/test_pg_migrations.py tests/api/test_realtime_activity.py -v` — PASS (migrations test needs `SCADBUDDY_TEST_DATABASE_URL`).
- [ ] **Step 7: Commit** `feat(activity): the activity record, its event and realtime topic`.

---

### Task 2: `ActivityIndex` writes

**Files:**
- Create: `backend/scadbuddy/activity/index.py`
- Modify: `backend/scadbuddy/core/metrics.py`
- Test: `backend/tests/test_activity_index.py`

**Interfaces:**
- Consumes: `ActivityRow`, `ActivityEvent` (Task 1); `TransactionalEvents.publish_in(conn, event)`.
- Produces:
  - `ActivityIndex(pool, *, events: TransactionalEvents | None, metrics: Metrics | None = None)`
  - `upsert_in(conn, row: ActivityRow | Callable[[], ActivityRow], *, attribution: Attribution | None = None) -> None` — never raises; a callable is built inside the guard, and `attribution` is written in the same savepoint
  - `Attribution(principal: str, session: str | None, via_tool: str | None)` (frozen dataclass in `activity/index.py`)
  - `attribute(activity_id: str, *, principal: str, session: str | None, via_tool: str | None) -> None` (async)
  - `get(activity_id) -> ActivityRow | None` (async)

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from psycopg import Connection

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.activity.models import ActivityRow
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


class Events:
    def __init__(self) -> None:
        self.published: list[object] = []

    def publish_in(self, conn: Connection[object], event: object) -> None:
        self.published.append(event)


def row(**over: object) -> ActivityRow:
    base = dict(id="render:j1", kind="render", status="running", title="Render keychain",
                source="render_jobs", source_id="j1", subject="keychain")
    return ActivityRow.model_validate({**base, **over})


async def test_upsert_then_attribute_keeps_principal_across_status_writes(pg_pool: PgPool) -> None:
    events = Events()
    index = ActivityIndex(pg_pool, events=events)
    with pg_pool.connection() as conn:
        index.upsert_in(conn, row())
    await index.attribute("render:j1", principal="token:t1", session="s1", via_tool="render_model")
    with pg_pool.connection() as conn:
        index.upsert_in(conn, row(status="succeeded"))
    got = await index.get("render:j1")
    assert got is not None
    assert (got.status, got.principal, got.session, got.via_tool) == ("succeeded", "token:t1", "s1", "render_model")
    assert got.finished_at is not None
    assert len(events.published) == 3


async def test_attribute_before_upsert_is_kept(pg_pool: PgPool) -> None:
    index = ActivityIndex(pg_pool, events=None)
    await index.attribute("render:j2", principal="browser", session=None, via_tool=None)
    with pg_pool.connection() as conn:
        index.upsert_in(conn, row(id="render:j2", source_id="j2"))
    got = await index.get("render:j2")
    assert got is not None and got.principal == "browser" and got.title == "Render keychain"


async def test_upsert_failure_does_not_abort_source(pg_pool: PgPool) -> None:
    index = ActivityIndex(pg_pool, events=None)
    with pg_pool.connection() as conn, conn.transaction():
        conn.execute("CREATE TEMP TABLE src (x int)")
        conn.execute("INSERT INTO src VALUES (1)")
        index.upsert_in(conn, row(status="bogus"))  # violates the CHECK
        index.upsert_in(conn, lambda: {}["missing"])  # a mapping that raises
        conn.execute("INSERT INTO src VALUES (2)")
        assert conn.execute("SELECT count(*) FROM src").fetchone() == {"count": 2}
```

`attribute` before any upsert creates a placeholder (`status='queued'`,
`title=''`, `kind=''`, `source=''`, `source_id=''`) that the first upsert fills; an upsert
writes every column except `principal`, `session`, `via_tool`, `created_at`, and fills
`kind`/`title`/`source`/`source_id` over a placeholder's empties. Reads never show a
placeholder (`source <> ''`); the repair sweep treats one as missing and fills it from its
source, and `prune` drops a placeholder older than the repair window that no source
filled (Task 8). A `status` that is not one
of the six fails the CHECK, so `ActivityRow.model_validate` must not be what rejects it in
that test: build it with `ActivityRow.model_construct(...)` for the bad row.

- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_activity_index.py -v` — FAIL (`ModuleNotFoundError: scadbuddy.activity.index`).

- [ ] **Step 3: Implement**

```python
"""The activity record's writes and reads (spec 2026-10-09 §3, §5, §6)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from psycopg import Connection, sql
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.activity.models import FINISHED, ActivityRow, ActivityStatus
from scadbuddy.core.events import ActivityEvent
from scadbuddy.core.metrics import Metrics
from scadbuddy.render.pg_store import TransactionalEvents

logger = logging.getLogger(__name__)

_WRITTEN = (
    "kind", "subject", "status", "title", "parent_id", "facets", "detail", "source",
    "source_id", "workflow_id", "run_id", "traceparent",
)
_COLUMNS = (
    "id", "kind", "principal", "session", "via_tool", "subject", "status", "title",
    "parent_id", "facets", "detail", "source", "source_id", "workflow_id", "run_id",
    "traceparent", "created_at", "updated_at", "finished_at",
)


@dataclass(frozen=True)
class Attribution:
    principal: str
    session: str | None
    via_tool: str | None


class ActivityIndex:
    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]],
        *,
        events: TransactionalEvents | None,
        metrics: Metrics | None = None,
    ) -> None:
        self.pool = pool
        self.events = events
        self.metrics = metrics

    def upsert_in(
        self,
        conn: Connection[Any],
        row: ActivityRow | Callable[[], ActivityRow],
        *,
        attribution: Attribution | None = None,
    ) -> None:
        """Write ``row`` in the caller's transaction, under a savepoint: a failure is
        logged and counted, and the caller's own write still commits (Review Focus 4).
        A callable is built inside the guard, so a read or a mapping that raises cannot
        abort the source's write either."""
        activity_id = row.id if isinstance(row, ActivityRow) else None
        try:
            with conn.transaction():
                if not isinstance(row, ActivityRow):
                    row = row()
                activity_id = row.id
                values = row.model_dump(include=set(_WRITTEN))
                values["facets"] = json.dumps(values["facets"])
                values["detail"] = json.dumps(values["detail"])
                finished = row.status in FINISHED
                conn.execute(
                    sql.SQL(
                        "INSERT INTO activity (id, {cols}, finished_at) VALUES (%(id)s, {vals}, "
                        "CASE WHEN %(finished)s THEN now() END) "
                        "ON CONFLICT (id) DO UPDATE SET {sets}, updated_at = now(), "
                        "finished_at = CASE WHEN %(finished)s "
                        "THEN coalesce(activity.finished_at, now()) END"
                    ).format(
                        cols=sql.SQL(", ").join(map(sql.Identifier, _WRITTEN)),
                        vals=sql.SQL(", ").join(sql.Placeholder(c) for c in _WRITTEN),
                        sets=sql.SQL(", ").join(
                            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in _WRITTEN
                        ),
                    ),
                    {**values, "id": row.id, "finished": finished},
                )
                if attribution is not None:
                    conn.execute(
                        "UPDATE activity SET principal = coalesce(principal, %s),"
                        " session = coalesce(session, %s), via_tool = coalesce(via_tool, %s)"
                        " WHERE id = %s",
                        (attribution.principal, attribution.session, attribution.via_tool, row.id),
                    )
                if self.events is not None:
                    found = conn.execute("SELECT principal FROM activity WHERE id = %s", (row.id,)).fetchone()
                    principal = None if found is None else found["principal"]
                    self.events.publish_in(conn, ActivityEvent(activity_id=row.id, principal=principal))
        except Exception:
            logger.exception("an activity write failed", extra={"activity_id": activity_id})
            if self.metrics is not None:
                self.metrics.activity_write_failures.inc()

    async def attribute(self, activity_id: str, *, principal: str, session: str | None, via_tool: str | None) -> None:
        await asyncio.to_thread(self._attribute, activity_id, principal, session, via_tool)

    def _attribute(self, activity_id: str, principal: str, session: str | None, via_tool: str | None) -> None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO activity (id, kind, principal, session, via_tool, status, title, source, source_id)"
                " VALUES (%s, '', %s, %s, %s, 'queued', '', '', '')"
                " ON CONFLICT (id) DO UPDATE SET principal = coalesce(activity.principal, EXCLUDED.principal),"
                " session = coalesce(activity.session, EXCLUDED.session),"
                " via_tool = coalesce(activity.via_tool, EXCLUDED.via_tool)",
                (activity_id, principal, session, via_tool),
            )
            if self.events is not None:
                self.events.publish_in(conn, ActivityEvent(activity_id=activity_id, principal=principal))

    async def get(self, activity_id: str) -> ActivityRow | None:
        return await asyncio.to_thread(self._get, activity_id)

    def _get(self, activity_id: str) -> ActivityRow | None:
        with self.pool.connection() as conn:
            found = conn.execute(
                sql.SQL("SELECT {} FROM activity WHERE id = %s AND source <> ''").format(
                    sql.SQL(", ").join(map(sql.Identifier, _COLUMNS))
                ),
                (activity_id,),
            ).fetchone()
        return None if found is None else ActivityRow.model_validate(found)
```

The `coalesce` in `_attribute` is what keeps the first principal of a coalesced render
(Review Focus 1). In `core/metrics.py` `Metrics.__init__`, beside `events_dropped`:

```python
        self.activity_write_failures = Counter(
            "scadbuddy_activity_write_failures",
            "Activity rows a source could not write; the repair pass restores them.",
            registry=r,
        )
```

Add a Task 2 test that reads it: `metrics.registry.get_sample_value("scadbuddy_activity_write_failures_total") == 1`
after the failing upsert (pass `metrics=Metrics()` to that test's index).

- [ ] **Step 4: Run** the tests — PASS.
- [ ] **Step 5: Commit** `feat(activity): ActivityIndex upsert under a savepoint, attribution kept`.

---

### Task 3: The kind registry

**Files:**
- Create: `backend/scadbuddy/activity/kinds.py`
- Test: `backend/tests/test_activity_kinds.py`

**Interfaces:**
- Consumes: `ActivityKind`, `Facet` (Task 1); `core/components.py` `feature_exports`; `OperationKind` (`operations/kinds.py`).
- Produces:
  - `ACTIVITY_MODULE = "activity"`, `ACTIVITY_ATTR = "ACTIVITY_KINDS"`
  - `class KindRegistry`: `get(key) -> ActivityKind | None`, `all() -> list[ActivityKind]`, `facet(key, name) -> Facet | None`
  - `discover_kinds(operation_kinds: Iterable[str], agent_rows: list[dict]) -> KindRegistry`
  - `load_agent_kinds(conn) -> list[dict]`
  - `class AgentKeyCollisionError(ValueError)`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from scadbuddy.activity.kinds import AgentKeyCollisionError, discover_kinds
from scadbuddy.activity.models import ActivityKind


def test_operations_yield_kinds_and_agent_rows_are_data() -> None:
    reg = discover_kinds(["model_import"], [
        {"key": "agent.plugin_package_install", "label": "Install plugin package",
         "facets": [{"name": "package", "type": "keyword", "label": "Package"}], "retired_at": None},
    ])
    assert reg.get("operation.model_import") is not None
    agent = reg.get("agent.plugin_package_install")
    assert agent is not None and agent.label == "Install plugin package"
    assert reg.facet("agent.plugin_package_install", "package") is not None
    assert reg.get("render") is not None  # discovered from render/activity.py (Task 4)


def test_code_key_with_agent_prefix_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import scadbuddy.activity.kinds as kinds
    monkeypatch.setattr(kinds, "feature_exports", lambda module, attr: [(ActivityKind(key="agent.x", label="x"),)])
    with pytest.raises(AgentKeyCollisionError):
        discover_kinds([], [])
```

- [ ] **Step 2: Run** — FAIL (no module).
- [ ] **Step 3: Implement**

```python
"""Activity kinds (spec 2026-10-09 §4): each feature's ``ACTIVITY_KINDS`` from its
``scadbuddy/<feature>/activity.py``, one per operation kind, and the agent's
``ai_activity_kinds`` rows. Nothing is listed by hand."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from psycopg import Connection, errors

from scadbuddy.activity.models import ActivityKind, Facet
from scadbuddy.core.components import feature_exports

ACTIVITY_MODULE = "activity"
ACTIVITY_ATTR = "ACTIVITY_KINDS"
AGENT_PREFIX = "agent."


class AgentKeyCollisionError(ValueError):
    pass


class KindRegistry:
    def __init__(self, kinds: Iterable[ActivityKind]) -> None:
        self._kinds: dict[str, ActivityKind] = {}
        for kind in kinds:
            if kind.key in self._kinds:
                raise ValueError(f"two activity kinds are registered as {kind.key!r}")
            self._kinds[kind.key] = kind

    def get(self, key: str) -> ActivityKind | None:
        return self._kinds.get(key)

    def all(self) -> list[ActivityKind]:
        return list(self._kinds.values())

    def facet(self, key: str, name: str) -> Facet | None:
        kind = self._kinds.get(key)
        return None if kind is None else next((f for f in kind.facets if f.name == name), None)


def operation_kind_key(name: str) -> str:
    return f"operation.{name}"


def discover_kinds(operation_kinds: Iterable[str], agent_rows: list[dict[str, Any]]) -> KindRegistry:
    code: list[ActivityKind] = [k for group in feature_exports(ACTIVITY_MODULE, ACTIVITY_ATTR) for k in group]
    for kind in code:
        if kind.key.startswith(AGENT_PREFIX):
            raise AgentKeyCollisionError(f"{kind.key!r}: the {AGENT_PREFIX!r} prefix is the agent's")
    ops = [
        ActivityKind(key=operation_kind_key(name), label=name.replace("_", " ").capitalize())
        for name in operation_kinds
        if not any(k.key == operation_kind_key(name) for k in code)
    ]
    agent = [
        ActivityKind(
            key=row["key"],
            label=row["label"],
            facets=tuple(Facet(name=f["name"], type=f["type"], label=f["label"]) for f in row["facets"]),
        )
        for row in agent_rows
        if row["key"].startswith(AGENT_PREFIX)
    ]
    return KindRegistry([*code, *ops, *agent])


def load_agent_kinds(conn: Connection[Any]) -> list[dict[str, Any]]:
    """The agent's kinds, retired ones too (they still label old rows); none when the
    agent's tables are absent."""
    try:
        with conn.transaction():
            return list(conn.execute(
                "SELECT key, label, facets, retired_at FROM ai_activity_kinds ORDER BY key"
            ).fetchall())
    except errors.UndefinedTable:
        return []
```

An operation kind without its own `operations/activity.py` entry gets a label from its
name; a feature that wants a better label or facets declares `operation.<name>` in its own
`activity.py`.

- [ ] **Step 4: Run** — PASS after Task 4's `render/activity.py` exists; until then mark
  `assert reg.get("render")` with Task 4 and run it there.
- [ ] **Step 5: Commit** `feat(activity): discovered kinds, operation kinds and agent kinds as data`.

---

### Task 4: Renders write activity; the render route attributes

**Files:**
- Create: `backend/scadbuddy/render/activity.py`
- Modify: `backend/scadbuddy/render/projection.py` (`_announce` and its callers), `backend/scadbuddy/core/authorship.py`, the render route that calls `RenderService.submit` (find it: `grep -rn "\.submit(" backend/scadbuddy/api/`)
- Test: `backend/tests/test_activity_render.py`, `backend/tests/test_authorship.py` (or the existing authorship test: `grep -rln author_from backend/tests`)

**Interfaces:**
- Consumes: `ActivityIndex.upsert_in`, `.attribute` (Task 2).
- Produces:
  - `render.activity.row_of(job: Job) -> ActivityRow`
  - `ACTIVITY_KINDS = (RENDER,)` with key `render` and facets `slug`, `model_version`
  - `AgentAuthor.tool: str | None`; header `X-ScadBuddy-Agent-Tool`
  - `attribute_request(index, activity_id) -> None` in `activity/index.py`

- [ ] **Step 1: `AgentAuthor.tool`.** In `core/authorship.py`, add `TOOL_HEADER = "x-scadbuddy-agent-tool"`, a field `tool: str | None = None` on `AgentAuthor`, and in `author_from` read it with the same `_PRINCIPAL` pattern check (a malformed value is the same 400 the principal gets). Test:

```python
from starlette.datastructures import Headers
from scadbuddy.core.authorship import author_from


def test_tool_header_is_read() -> None:
    got = author_from(Headers({"x-scadbuddy-agent-author": "token:t1", "x-scadbuddy-agent-tool": "render_model"}))
    assert got is not None and got.tool == "render_model"
```

- [ ] **Step 2: Write the failing render tests**

```python
import pytest

from scadbuddy.render.activity import row_of
from scadbuddy.render.job_models import Job

pytestmark = pytest.mark.requires_postgres


@pytest.mark.parametrize(("state", "expected"), [
    ("pending", "queued"), ("running", "running"), ("done", "succeeded"), ("failed", "failed"),
    ("cancelled", "cancelled"),
])
def test_status_mapping(state: str, expected: str, make_job) -> None:  # make_job: see Step 3
    assert row_of(make_job(state=state)).status == expected


def test_arrange_is_its_own_kind(make_job) -> None:
    assert row_of(make_job(state="done", kind="arrange")).kind == "arrange"
```

Plus an api test in `backend/tests/api/test_activity_attribution.py` (uses the api
`client` fixture and the fake openscad):

```python
async def test_coalesced_render_keeps_first_principal(client, activity_index) -> None:
    body = {"params": {"text": "Hi"}}
    first = await client.post("/api/v1/models/name-keychain/render", json=body,
                              headers={"X-ScadBuddy-Agent-Author": "token:t1"})
    second = await client.post("/api/v1/models/name-keychain/render", json=body)
    assert first.json()["id"] == second.json()["id"]
    got = await activity_index.get(f"render:{first.json()['id']}")
    assert got is not None and got.principal == "token:t1"
```

Check the real render route path and body in `tests/api/test_jobs.py` and copy them;
add an `activity_index` fixture to `tests/api/conftest.py` that returns
`app.state.components.get(ACTIVITY).index` (Task 6).

- [ ] **Step 3: Implement `render/activity.py`**

```python
"""Renders as activity (spec 2026-10-09 §3): ``render_jobs`` states mapped onto the six."""

from __future__ import annotations

from scadbuddy.activity.models import ActivityKind, ActivityLink, ActivityRow, ActivityStatus, Facet
from scadbuddy.render.job_models import Job
from scadbuddy.render.projection import workflow_id_for

#: `render/job_models.py` `JobState`; `cancelled` is a superseded render.
_STATUS: dict[str, ActivityStatus] = {
    "pending": "queued", "running": "running", "done": "succeeded", "failed": "failed",
    "cancelled": "cancelled",
}


def row_of(job: Job) -> ActivityRow:
    verb = "Arrange" if job.kind == "arrange" else "Render"
    return ActivityRow(
        id=f"render:{job.id}", kind=job.kind, subject=job.slug, status=_STATUS[job.state],
        title=f"{verb} {job.slug}", source="render_jobs", source_id=job.id,
        facets={"slug": job.slug, "model_version": job.model_version},
        workflow_id=workflow_id_for(job.id), traceparent=job.traceparent,
    )


RENDER = ActivityKind(
    key="render", label="Render",
    facets=(Facet("slug", "keyword", "Model"), Facet("model_version", "keyword", "Version", filterable=False)),
    links=lambda row: [ActivityLink("job", "Render", f"/api/v1/jobs/{row.source_id}")],
)
ARRANGE = ActivityKind(
    key="arrange", label="Arrange", facets=(Facet("slug", "keyword", "Model"),),
    links=RENDER.links,
)
ACTIVITY_KINDS = (RENDER, ARRANGE)
```

`Job`'s state field is `state` (`JobState`), its kind `kind` (`"render" | "arrange"`);
check the remaining names (`model_version`, `traceparent`) in `render/job_models.py`. `make_job` is a fixture in the test file that builds a `Job`
with `Job.model_validate({...})` from the minimum fields `job_models.Job` requires.

- [ ] **Step 4: Write from the projection.** Every state change already goes through
  `_announce(conn, job_id, slug, kind)` inside its transaction: `accept` (insert),
  `mark_started`, `finish`, `_release`, the legacy fails. In `JobProjection.__init__` add
  `self.activity: ActivityIndex | None = None`; in `_announce`, after the publish, add:

```python
        if self.activity is not None:
            self.activity.upsert_in(conn, lambda: row_of(self._read_in(conn, job_id)))
```

  Pass a lambda, not the row: the read and the mapping then run inside `upsert_in`'s
  savepoint, so a `KeyError` in `_STATUS` cannot abort the projection's write.

  where `_read_in(conn, job_id)` is `_job(conn.execute(f"SELECT {cols} FROM render_jobs WHERE id = %s", (job_id,)).fetchone())` using the module's `PROJECTION_COLUMNS` (copy the shape of `read`). `ACTIVITY`'s component (Task 6) sets `core.projection.activity = index` when it builds.

- [ ] **Step 5: Attribute at the route.** In `activity/index.py` add:

```python
async def attribute_request(index: ActivityIndex, activity_id: str, request: Request) -> None:
    """Who started ``activity_id``: the agent author, the headless-browser session's owner,
    or the browser user (spec §3)."""
    author = current_author()
    session_marker = request.headers.get(AGENT_ACTOR_HEADER)
    if author is not None and author.principal is not None:
        await index.attribute(activity_id, principal=author.principal, session=author.session, via_tool=author.tool)
    elif session_marker is not None:
        principal = await session_owner(index.pool, session_marker)
        tool = None if author is None else author.tool
        await index.attribute(activity_id, principal=principal or UNKNOWN_SESSION, session=session_marker, via_tool=tool)
    else:
        await index.attribute(activity_id, principal="browser", session=None, via_tool=None)
```

  `session_owner` and `UNKNOWN_SESSION` come from Task 7's `activity/visibility.py`;
  `AGENT_ACTOR_HEADER` from `api/agent_actor.py`. In the render route, after
  `job = await render.submit(...)`, add
  `await attribute_request(activity.index, f"render:{job.id}", request)` with
  `activity: Annotated[ActivityComponent, component_dep(ACTIVITY)]`.
  Known race, accepted: two *concurrent* identical renders from different principals
  both return the same id, and whichever `attribute` commits first is recorded (the
  `coalesce` keeps it). Sequential joins, the case that happens, keep the starter
  (`test_coalesced_render_keeps_first_principal`). Making it exact needs `submit` to say
  whether it started the workflow; that is not worth a change to `RenderService` here.

- [ ] **Step 6: Run** `uv run --frozen pytest tests/test_activity_render.py tests/api/test_activity_attribution.py -v` — PASS.
- [ ] **Step 7: Commit** `feat(activity): renders write activity; the route records who started one`.

---

### Task 5: Operations and print runs write activity

**Files:**
- Create: `backend/scadbuddy/operations/activity.py`, `backend/scadbuddy/bambuddy/activity.py`
- Modify: `backend/scadbuddy/operations/store.py` (`_insert`, `_finish`, `insert` signature), `backend/scadbuddy/workflows/operation_activities.py` (`insert`), `backend/scadbuddy/bambuddy/runs.py` (`_announce`), the print-run start route (`grep -rn "print_runs\.\|PrintCommands" backend/scadbuddy/api/ | grep -i start`)
- Test: `backend/tests/test_activity_sources.py`

**Interfaces:**
- Consumes: Task 2, Task 4's `attribute_request`.
- Produces: `operations.activity.row_of(op: Operation, kind_label: str | None = None) -> ActivityRow`; `bambuddy.activity.row_of(run: PrintRun) -> ActivityRow`; `OperationStore.insert(..., author: OperationAuthor | None = None)`.

- [ ] **Step 1: Failing tests**

```python
pytestmark = pytest.mark.requires_postgres


async def test_operation_insert_and_finish_write_activity(store_with_activity, index) -> None:
    op = await store_with_activity.insert(
        "o1", kind="model_import", subject="keychain", operation_key="k", request={},
        workflow_id="op-model_import-k", workflow_run_id="r1", retention=None,
        author=OperationAuthor(principal="token:t1", session="s1"),
    )
    got = await index.get("operation:o1")
    assert got is not None and (got.status, got.principal, got.kind) == ("running", "token:t1", "operation.model_import")
    await store_with_activity.finish(op.id, result={"ok": True})
    got = await index.get("operation:o1")
    assert got is not None and got.status == "succeeded" and got.principal == "token:t1"


@pytest.mark.parametrize(("run_status", "expected"), [
    ("running", "running"), ("succeeded", "succeeded"), ("failed", "failed"),
])
def test_print_run_status_mapping(run_status: str, expected: str, make_run) -> None:
    assert bambuddy_row_of(make_run(status=run_status)).status == expected
```

  `print_runs.status` is `running | succeeded | failed` (`bambuddy/runs.py` `RunStatus`);
  the model's subject field is `subject` (column `output_id`: `output:<id>` or
  `library:<file id>`). Facets: `subject`, and `may_have_queued` (bool, not filterable).

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement `operations/activity.py`**

```python
"""Operations as activity: one kind per ``OperationKind`` (``activity/kinds.py``)."""

from scadbuddy.activity.kinds import operation_kind_key
from scadbuddy.activity.models import ActivityRow
from scadbuddy.operations.store import Operation

_STATUS = {"running": "running", "succeeded": "succeeded", "failed": "failed"}


def row_of(op: Operation, principal: str | None = None) -> ActivityRow:
    return ActivityRow(
        id=f"operation:{op.id}", kind=operation_kind_key(op.kind), subject=op.subject,
        status=_STATUS[op.status], title=f"{op.kind.replace('_', ' ').capitalize()} {op.subject}",
        source="operations", source_id=op.id, workflow_id=op.workflow_id, run_id=op.workflow_run_id,
    )


ACTIVITY_KINDS: tuple[()] = ()
```

  In `OperationStore`, take `activity: ActivityIndex | None = None` in `__init__`; in
  `_insert`, inside the transaction where it announces, call
  `self.activity.upsert_in(conn, lambda: row_of(op), attribution=Attribution(author.principal or "browser", author.session, author.tool) if author else Attribution("browser", None, None))`:
  the attribution is written in `upsert_in`'s savepoint, never as a bare `UPDATE` in the
  store's transaction, so it cannot fail the operation's insert. In `_finish` call
  `upsert_in(conn, lambda: row_of(op))` likewise. In `operation_activities.insert`, pass
  `author=op.author` (and add `tool: str | None = None` to `OperationAuthor` in
  `operation_models.py`). In `api/operations.py`, `_author()` must resolve the principal
  the way `attribute_request` does, or a headless-browser request (`author_from` gives
  `AgentAuthor(principal=None, session=<marker>)`) falls through to `browser`. Make it
  async and give it the request and the pool:

```python
async def _author(request: Request, pool: ConnectionPool[Connection[DictRow]]) -> OperationAuthor | None:
    author = current_author()
    if author is None:
        return None
    principal = author.principal
    if principal is None and request.headers.get(AGENT_ACTOR_HEADER) is not None:
        principal = await session_owner(pool, request.headers[AGENT_ACTOR_HEADER]) or UNKNOWN_SESSION
    return OperationAuthor(**{**vars(author), "principal": principal})
```

  and in `run_operation` call `author=await _author(request, ops.store.pool)` (use the
  pool the store already holds). `AgentAuthor.tool` from Task 4 flows in unchanged.
  Test in `backend/tests/api/test_activity_attribution.py`:

```python
async def test_headless_operation_is_its_session_owners(client, agent_tables, activity_index) -> None:
    sid = agent_tables.insert_session(owner_kind="bearer", owner_id="token:t1")
    headers = {"X-ScadBuddy-Agent-Session": sid}
    started = await client.post("/api/v1/fonts/install", json={"family": "Lobster Two"}, headers=headers)
    op_id = started.json()["id"]
    page = (await client.get("/api/v1/activity", headers=headers)).json()
    assert [r["principal"] for r in page["items"] if r["id"] == f"operation:{op_id}"] == ["token:t1"]
```

  Pick any cheap operation route from `tests/api/test_operations*.py` if font install
  is not one; the point is a request carrying only the session marker. A pydantic field with a default
  is replay-safe; no `patched()` is needed since the workflow code does not change.
  If `Operation` has no `workflow_id`/`workflow_run_id` fields, read them from the row
  the store already selects.

- [ ] **Step 4: Implement `bambuddy/activity.py`** (same shape, key `print_run`), and call
  `upsert_in` from `PrintRunStore._announce` (`bambuddy/runs.py`), which `_insert` and
  `_finish` already call in their transactions. In the print-start route, after the run id is known, call
  `attribute_request(activity.index, f"print_run:{run.id}", request)`.
- [ ] **Step 5: Run** — PASS. Also run `uv run --frozen pytest tests/test_operations_store.py tests/test_print_runs_store.py tests/test_operation_workflow.py -n auto` to show the sources are unchanged.
- [ ] **Step 6: Commit** `feat(activity): operations and print runs write activity`.

---

### Task 6: The component, and agent operations ingested

**Files:**
- Create: `backend/scadbuddy/activity/component.py`
- Modify: `backend/scadbuddy/activity/index.py` (`ingest_agent_operation`), `backend/scadbuddy/worker.py` (the worker processes' own stores)
- Test: `backend/tests/test_activity_agent_ingest.py`, `backend/tests/test_worker.py`

The component wires the API process's stores only. The workers build their own:
`worker.py` makes a `JobProjection` for the render worker (the `project` activity writes
`mark_started`/`finish` there), and `build_print_deps` a `PrintRunStore` and an
`OperationStore` for the `bambuddy` queue. Each gets
`activity=ActivityIndex(<its pool>, events=<its events>, metrics=<its metrics>)` (the
`events` there already NOTIFY in the row's transaction, so the API's listeners hear the
`activity.changed`). Without this, every state change made on a worker is missing until
the repair sweep. In `tests/test_worker.py`, assert that the stores `build_print_deps`
returns, and the render worker's projection, carry a non-`None` `activity`.

**Interfaces:**
- Produces:
  - `ACTIVITY: Key[ActivityComponent]`
  - `ActivityComponent(index: ActivityIndex, kinds: Callable[[], KindRegistry])` — `kinds()` reloads agent kinds at most every 60 s
  - `ActivityIndex.ingest_agent_operation(operation_id: str) -> None` (async)

- [ ] **Step 1: Failing test** — create the agent's tables in the test schema with the
  agent's migration SQL (copy the `CREATE TABLE ai_operations` from
  `agent/src/db/migrations/20261004T1345Z_operations.sql` plus Task 10's columns into a
  fixture), insert a row, call `ingest_agent_operation`, and assert:

```python
async def test_ingest_agent_operation(index, agent_tables) -> None:
    agent_tables.insert_operation(id="a1", kind="plugin_package_install", subject="hindsight",
                                  status="succeeded", principal="browser", via_tool=None,
                                  request={"source": "x"}, result={"ok": True})
    await index.ingest_agent_operation("a1")
    got = await index.get("agent_operation:a1")
    assert got is not None
    assert (got.kind, got.status, got.principal) == ("agent.plugin_package_install", "succeeded", "browser")
    assert got.detail["result"] == {"ok": True}


async def test_ingest_without_agent_tables_is_a_no_op(index) -> None:
    await index.ingest_agent_operation("missing")
    assert await index.get("agent_operation:missing") is None
```

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement**

```python
    async def ingest_agent_operation(self, operation_id: str) -> None:
        await asyncio.to_thread(self._ingest_agent_operation, operation_id)

    def _ingest_agent_operation(self, operation_id: str) -> None:
        with self.pool.connection() as conn, conn.transaction():
            try:
                with conn.transaction():
                    op = conn.execute(
                        "SELECT id, kind, subject, status, request, result, error, principal, session,"
                        " via_tool, workflow_id, workflow_run_id FROM ai_operations WHERE id = %s",
                        (operation_id,),
                    ).fetchone()
            except errors.UndefinedTable:
                return
            if op is None:
                return
            self.upsert_in(
                conn,
                lambda: ActivityRow(
                    id=f"agent_operation:{op['id']}", kind=f"agent.{op['kind']}",
                    subject=op["subject"] or "", status=_AGENT_STATUS[op["status"]],
                    title=f"{op['kind'].replace('_', ' ').capitalize()} {op['subject'] or ''}".strip(),
                    source="ai_operations", source_id=op["id"], workflow_id=op["workflow_id"],
                    run_id=op["workflow_run_id"],
                    detail={"request": op["request"], "result": op["result"], "error": op["error"]},
                ),
                attribution=Attribution(op["principal"] or "browser", op["session"], op["via_tool"]),
            )


#: `ai_operations.status` (agent `src/operations/`); anything else raises inside
#: `upsert_in`'s guard, so it is logged and counted, never lost in the listener task.
_AGENT_STATUS: dict[str, ActivityStatus] = {"running": "running", "succeeded": "succeeded", "failed": "failed"}
```

  Check `_AGENT_STATUS` against the statuses the agent's `ai_operations` CHECK allows and
  add any it has. Add a test that an `ai_operations` row with an unknown status (insert it
  with the CHECK dropped in the test schema) leaves no row and raises nothing.

  `component.py`:

```python
"""Activity as a component (spec 2026-10-09): the index, the kinds, and the bus
consumer that ingests the agent's operations."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.activity.kinds import KindRegistry, discover_kinds, load_agent_kinds
from scadbuddy.api.deps import transactional_events
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.events import AgentOperationEvent, Event, EventBus
from scadbuddy.operations.component import OPERATIONS

KINDS_TTL = 60.0


@dataclass
class ActivityComponent:
    index: ActivityIndex
    kinds: Callable[[], KindRegistry]
    bus: EventBus


ACTIVITY: Key[ActivityComponent] = Key("activity")


def _build(core: Core, components: Components) -> ActivityComponent:
    ops = components.get(OPERATIONS)
    index = ActivityIndex(core.projection.pool, events=transactional_events(core.events), metrics=core.metrics)
    core.projection.activity = index
    ops.store.activity = index
    core.print_runs.store.activity = index  # `PrintCommands.store`, as `api/outputs.py` reads it
    cache: dict[str, object] = {"at": 0.0, "reg": None}

    def kinds() -> KindRegistry:
        if cache["reg"] is None or time.monotonic() - float(cache["at"]) > KINDS_TTL:  # type: ignore[arg-type]
            with core.projection.pool.connection() as conn:
                cache["reg"] = discover_kinds(ops.kinds.keys(), load_agent_kinds(conn))
            cache["at"] = time.monotonic()
        return cache["reg"]  # type: ignore[return-value]

    return ActivityComponent(index=index, kinds=kinds, bus=core.events)


@asynccontextmanager
async def _run(component: ActivityComponent) -> AsyncIterator[None]:
    """Ingest every ``ai_operation.changed`` while the app runs. The bus calls listeners
    on its own thread (``PgNotifyEventBus.add_listener``), so hop onto the loop, as
    ``api/runtime.py`` ``follow_changes`` does."""
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()

    def ingest(operation_id: str) -> None:
        task = loop.create_task(component.index.ingest_agent_operation(operation_id))
        pending.add(task)
        task.add_done_callback(pending.discard)

    def on_event(event: Event) -> None:
        if isinstance(event, AgentOperationEvent):
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(ingest, event.operation_id)

    remove = component.bus.add_listener(on_event)
    try:
        yield
    finally:
        remove()
        for task in pending:
            task.cancel()


COMPONENT = Component(ACTIVITY, build=_build, run=_run)
```

  Type `bus` as `EventBus` (`core/events.py`) and replace the `# type: ignore`s with the
  real types. The print-runs store is reached as `core.print_runs` (`PrintCommands`):
  find its `PrintRunStore` attribute with `grep -n "class PrintCommands" -A 15 backend/scadbuddy/bambuddy/*.py`. Only one API process needs to ingest, but every process doing it is
  harmless: the upsert is idempotent.

- [ ] **Step 4: Run** — PASS; `uv run --frozen pytest tests/test_components.py` still passes.
- [ ] **Step 5: Commit** `feat(activity): the activity component ingests the agent's operations`.

---

### Task 7: Reads, filters, facets and visibility

**Files:**
- Create: `backend/scadbuddy/activity/visibility.py`, `backend/scadbuddy/api/activity.py`
- Modify: `backend/scadbuddy/activity/index.py` (`list`, `facets`), `backend/scadbuddy/api/operations.py` (`Activity-Location` on 202). Routers in `scadbuddy/api/` are mounted automatically (`main.py` `_api_router`); `tests/api/test_routes.py` checks no two routes match one request.
- Test: `backend/tests/api/test_activity.py`

**Interfaces:**
- Produces:
  - `GET /api/v1/activity` → `ActivityPage {items: ActivityRow[], next_cursor: str | None}`
  - `GET /api/v1/activity/facets?since=` → `ActivityFacets {kinds: [{key,label,facets:[{name,type,label,filterable}]}], statuses: [str], principals: [{value,count}], tools: [{value,count}], subjects: [{value,count}]}`
  - `GET /api/v1/activity/{id}?advanced=` → `ActivityDetail {row, children: ActivityRow[], links: [{rel,label,href}], detail: dict}`
  - `session_owner(pool, session_id) -> str | None` (async); `UNKNOWN_SESSION = "session:unknown"`
  - `pinned_principal(request, pool) -> str | None` (async): `None` = not pinned

- [ ] **Step 1: Failing API tests**

```python
LIST_PARAMS = {"kind", "status", "principal", "session", "via_tool", "subject", "since",
               "until", "parent", "q", "cursor", "limit"}


async def test_lists_newest_first_with_filters(client, seed_activity) -> None:
    seed_activity(id="render:a", principal="browser", status="succeeded", kind="render")
    seed_activity(id="render:b", principal="token:t1", status="failed", kind="render")
    page = (await client.get("/api/v1/activity", params={"status": "failed"})).json()
    assert [r["id"] for r in page["items"]] == ["render:b"]
    page = (await client.get("/api/v1/activity", params={"principal": "browser"})).json()
    assert [r["id"] for r in page["items"]] == ["render:a"]


async def test_facet_filter(client, seed_activity) -> None:
    seed_activity(id="render:a", kind="render", facets={"slug": "keychain"})
    seed_activity(id="render:b", kind="render", facets={"slug": "box"})
    page = (await client.get("/api/v1/activity", params={"kind": "render", "facet.slug": "box"})).json()
    assert [r["id"] for r in page["items"]] == ["render:b"]


async def test_unknown_param_is_400(client) -> None:
    for params in ({"stauts": "failed"}, {"kind": "render", "facet.nope": "x"}, {"facet.slug": "x"}):
        response = await client.get("/api/v1/activity", params=params)
        assert response.status_code == 400, params
        assert response.headers["content-type"].startswith("application/problem+json")


async def test_facets_from_data(client, seed_activity) -> None:
    seed_activity(id="render:a", principal="token:t1", via_tool="render_model")
    body = (await client.get("/api/v1/activity/facets")).json()
    assert {"value": "token:t1", "count": 1} in body["principals"]
    assert {"value": "render_model", "count": 1} in body["tools"]
    assert any(k["key"] == "render" for k in body["kinds"])
    assert body["statuses"] == ["queued", "running", "waiting", "succeeded", "failed", "cancelled"]


async def test_facets_without_agent_tables(client) -> None:
    assert (await client.get("/api/v1/activity/facets")).status_code == 200


async def test_headless_session_is_pinned_to_its_owner(client, seed_activity, agent_tables) -> None:
    sid = agent_tables.insert_session(owner_kind="bearer", owner_id="token:t1")
    seed_activity(id="render:a", principal="browser")
    seed_activity(id="render:b", principal="token:t1")
    page = (await client.get("/api/v1/activity", headers={"X-ScadBuddy-Agent-Session": sid})).json()
    assert [r["id"] for r in page["items"]] == ["render:b"]
    page = (await client.get("/api/v1/activity", params={"principal": "browser"},
                             headers={"X-ScadBuddy-Agent-Session": sid})).json()
    assert page["items"] == []


async def test_headless_session_cannot_read_other_detail(client, seed_activity, agent_tables) -> None:
    sid = agent_tables.insert_session(owner_kind="bearer", owner_id="token:t1")
    seed_activity(id="render:a", principal="browser")
    seed_activity(id="render:b", principal="token:t1")
    seed_activity(id="render:c", principal="browser", parent_id="render:b")
    headers = {"X-ScadBuddy-Agent-Session": sid}
    assert (await client.get("/api/v1/activity/render:a", headers=headers)).status_code == 404
    own = (await client.get("/api/v1/activity/render:b", headers=headers)).json()
    assert own["children"] == []


async def test_unknown_session_sees_nothing(client, seed_activity) -> None:
    seed_activity(id="render:a", principal="browser")
    page = (await client.get("/api/v1/activity", headers={"X-ScadBuddy-Agent-Session": str(uuid.uuid4())})).json()
    assert page["items"] == []


async def test_detail_simple_and_advanced(client, seed_activity) -> None:
    seed_activity(id="render:a", workflow_id="render-x", traceparent="00-abc-def-01")
    simple = (await client.get("/api/v1/activity/render:a")).json()
    assert "workflow_id" not in simple["row"]
    advanced = (await client.get("/api/v1/activity/render:a", params={"advanced": "1"})).json()
    assert advanced["row"]["workflow_id"] == "render-x"


async def test_paging_is_stable(client, seed_activity) -> None:
    for i in range(5):
        seed_activity(id=f"render:{i}")
    first = (await client.get("/api/v1/activity", params={"limit": 2})).json()
    second = (await client.get("/api/v1/activity", params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
```

  Put `seed_activity` (inserts via `ActivityIndex.upsert_in` + `attribute`) and
  `agent_tables` (a cut-down schema as `tests/api/test_agent_actor_grants.py`'s
  `MINI_SCHEMA` does: `ai_sessions(id uuid PRIMARY KEY, owner_kind text NOT NULL, owner_id
  text NOT NULL)`, and `ai_operations`/`ai_activity_kinds` as the agent's migrations
  create them) in `tests/api/conftest.py`. Session ids in these tests are UUIDs
  (`str(uuid.uuid4())`), not `"s1"`.

- [ ] **Step 2: Run** — FAIL (404).
- [ ] **Step 3: Implement `visibility.py`**

```python
"""Who may see what (spec 2026-10-09 §7). The browser sees everything; the agent's
headless browser sees its session owner's activity only. Headers label; none of them
authenticates, and the backend trusts no tier header."""

UNKNOWN_SESSION = "session:unknown"


async def session_owner(pool: ConnectionPool[Connection[DictRow]], session_id: str) -> str | None:
    canonical = _session_id(session_id)  # api/agent_actor.py: a UUID, or None
    if canonical is None:
        return None

    def read() -> str | None:
        with pool.connection() as conn:
            try:
                with conn.transaction():
                    row = conn.execute("SELECT owner_kind, owner_id FROM ai_sessions WHERE id = %s::uuid",
                                       (canonical,)).fetchone()
            except errors.UndefinedTable:
                return None
        if row is None:
            return None
        return "browser" if row["owner_kind"] == "browser" else row["owner_id"]
    return await asyncio.to_thread(read)


async def pinned_principal(request: Request, pool: ConnectionPool[Connection[DictRow]]) -> str | None:
    marker = request.headers.get(AGENT_ACTOR_HEADER)
    if marker is None:
        return None
    owner = await session_owner(pool, marker)
    if owner == "browser":
        return None
    return owner or UNKNOWN_SESSION
```

  In `ActivityIndex.list(filters, *, pinned, cursor, limit)`: build the `WHERE` from
  `filters` with `sql.Identifier`/placeholders; `facet.<name>` becomes
  `facets @> %s::jsonb` with `json.dumps({name: value})`; `q` is `title ILIKE '%' || %s || '%'`;
  `pinned` adds `principal = %s` (and a `principal` filter that differs from `pinned`
  yields no rows). Always `source <> ''` (hides placeholders). Order
  `created_at DESC, id DESC`; the cursor is base64 of `"<created_at iso>|<id>"` and adds
  `(created_at, id) < (%s, %s)`. `limit` default 50, max 200.

  `facets(since, pinned)`: `SELECT principal, count(*) … GROUP BY principal` and the same
  for `via_tool` and `subject`, over `created_at >= since` (default now − 7 d), plus the
  registry's kinds.

  `api/activity.py`:

```python
router = APIRouter(prefix="/activity", tags=["activity"])
FACET_PREFIX = "facet."


def _check_params(request: Request, kinds: KindRegistry, selected: list[str]) -> dict[str, str]:
    facets: dict[str, str] = {}
    for name, value in request.query_params.multi_items():
        if name in LIST_PARAMS:
            continue
        if not name.startswith(FACET_PREFIX):
            raise ApiError(400, f"unknown query parameter {name!r}", type_=UNKNOWN_PARAMETER)
        facet = name.removeprefix(FACET_PREFIX)
        if not selected or not any(kinds.facet(k, facet) and kinds.facet(k, facet).filterable for k in selected):
            raise ApiError(400, f"no selected kind declares a filterable facet {facet!r}", type_=UNKNOWN_PARAMETER)
        facets[facet] = value
    return facets
```

  with `UNKNOWN_PARAMETER = "https://scadbuddy.dev/problems/unknown-parameter"`, and the
  three routes using `component_dep(ACTIVITY)`. The detail route strips `workflow_id`,
  `run_id`, `traceparent`, `source`, `source_id`, `facets` and `detail`'s raw
  `request`/`result`/`error` unless `advanced`; it fills `links` and `detail` from the
  kind (`kind.detail(row)` when declared, else `row.detail`) and `children` from
  `parent_id = id`. The detail route pins as `list` does: with
  `pinned = await pinned_principal(request, activity.index.pool)` set, a row whose
  `principal != pinned` answers 404 (the same answer as a missing id, so an id learned
  from the WS topic or guessed reveals nothing), and `children` keeps only rows with
  `principal = pinned`. The module must not use `from __future__ import annotations`
  (`api/components.py`: FastAPI reads the dependency annotations at runtime).

  In `api/operations.py` `run_operation`, on the 202 answer add header
  `Activity-Location: /api/v1/activity/operation:<id>`.

- [ ] **Step 4: Run** `uv run --frozen pytest tests/api/test_activity.py -v` — PASS.
- [ ] **Step 5: Coverage.** The three new routes need agent tools (Task 11) or
  `NOT_A_TOOL` entries; Task 11 adds tools for list and get; add a `NOT_A_TOOL` entry for
  `GET /api/v1/activity/facets` ("the UI's filter bar; activity_list's own filters say the same") in `agent/src/tools/coverage.ts`.
- [ ] **Step 6: Commit** `feat(activity): list, facets and detail, filtered, headless sessions pinned`.

---

### Task 8: Retention and repair

**Files:**
- Modify: `backend/scadbuddy/workflows/housekeeping.py`, `backend/scadbuddy/main.py` (`_housekeeping_activities`), `backend/scadbuddy/library/settings_store.py` (`StoredSettings`, `SettingsPatch`), `backend/scadbuddy/api/settings.py` (the mirror of those fields), `backend/scadbuddy/activity/index.py` (`prune`, `repair`)
- Test: `backend/tests/test_activity_sweep.py`, `backend/tests/test_housekeeping_replay.py` (existing replay fixture must still pass)

**Interfaces:**
- Produces: sweep `housekeeping_activity`; `ACTIVITY_PATCH = "housekeeping-activity"`; stored settings `activity_ttl_seconds`, `activity_repair_window_seconds`, `activity_follow`; `ActivityIndex.prune(ttl: float, placeholder_ttl: float) -> int`, `ActivityIndex.repair(window: float) -> int`.

- [ ] **Step 1: Failing tests**

```python
async def test_prune_deletes_old_finished_only(index, seed_activity, pg_pool) -> None:
    seed_activity(id="render:old", status="succeeded")
    seed_activity(id="render:open", status="running")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE activity SET finished_at = now() - interval '40 days', created_at = now() - interval '40 days'")
    assert await index.prune(30 * 86400, 86400) == 1
    assert await index.get("render:open") is not None


async def test_prune_drops_stale_placeholders(index, pg_pool) -> None:
    await index.attribute("render:ghost", principal="browser", session=None, via_tool=None)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE activity SET created_at = now() - interval '2 days' WHERE id = 'render:ghost'")
        assert await index.prune(30 * 86400, 86400) == 1
        assert conn.execute("SELECT count(*) FROM activity WHERE id = 'render:ghost'").fetchone() == {"count": 0}


async def test_repair_fills_a_placeholder(index, operation_store_without_activity) -> None:
    # The source's own upsert failed (or never ran) and the route attributed first:
    # the placeholder must not hide the operation from repair.
    await index.attribute("operation:o8", principal="token:t1", session=None, via_tool=None)
    await operation_store_without_activity.insert("o8", kind="model_import", subject="k", operation_key="k8",
                                                  request={}, workflow_id="w", workflow_run_id="r", retention=None)
    assert await index.get("operation:o8") is None  # still a placeholder
    assert await index.repair(86400) >= 1
    got = await index.get("operation:o8")
    assert got is not None and got.principal == "token:t1"


async def test_repair_restores_a_missed_operation(index, operation_store_without_activity) -> None:
    await operation_store_without_activity.insert("o9", kind="model_import", subject="k", operation_key="k",
                                                  request={}, workflow_id="w", workflow_run_id="r", retention=None)
    assert await index.get("operation:o9") is None
    assert await index.repair(86400) >= 1
    assert await index.get("operation:o9") is not None
```

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** `prune(ttl, placeholder_ttl)`: `DELETE FROM activity WHERE finished_at < now() - make_interval(secs => %(ttl)s) OR (source = '' AND created_at < now() - make_interval(secs => %(placeholder_ttl)s))`; the sweep passes the repair window as `placeholder_ttl`, so a placeholder lives long enough for repair to fill it and no longer. `repair(window)`: for each source, select rows changed in the window that have no activity row *or only a placeholder* (`LEFT JOIN activity a ON a.id = 'render:' || r.id WHERE a.id IS NULL OR a.source = ''`), so a source whose own upsert failed after the route attributed it is still restored, with the placeholder's principal kept (`upsert_in` never writes the attribution columns); map them with that source's `row_of`, and `upsert_in` each (renders: `render_jobs`; operations: `operations`, principal from `request->'_author'` is not stored, so `browser` unless `operations` has an author column — it does not; leave principal NULL so it shows as "Unknown". Accepted limitation: an operation whose activity write failed *and* that no route attributed comes back as "Unknown"; the route's attribution normally lands first, and repair keeps it); and call `ingest_agent_operation` for `ai_operations` ids in the window with no row or a placeholder (skip on `UndefinedTable`). Add the settings as stored settings, read fresh by the sweep each run through
  `settings_store.load()`, the way `operation_retention_seconds` is
  (`library/settings_store.py` `StoredSettings` and `SettingsPatch`, mirrored in
  `api/settings.py`), not as env-seeded `core/settings.py` fields:

```python
# StoredSettings
    activity_ttl_seconds: float | None = None            # None: 30 days
    activity_repair_window_seconds: float | None = None  # None: 1 day
    activity_follow: list[str] | None = None             # None: ["browser"]
# SettingsPatch
    activity_ttl_seconds: float | None = Field(default=None, ge=86400)
    activity_repair_window_seconds: float | None = Field(default=None, ge=600, le=7 * 86400)
    activity_follow: list[str] | None = Field(default=None, max_length=50)
```

  Add `"housekeeping_activity"` to the end of `SWEEPS`, guarded in `Housekeeping.run`
  exactly as `REAP_SWEEP` is:

```python
            if sweep == ACTIVITY_SWEEP and not workflow.patched(ACTIVITY_PATCH):
                continue
```

  and add its activity to `main.py` `_housekeeping_activities` beside `sweep_claims`
  (`@activity.defn(name=ACTIVITY_SWEEP)`, wrapped in `_heartbeating` and a `_logged`
  helper as the others are), reading the two settings from `state.settings_store.load()`
  each run. `tests/test_housekeeping.py`'s `FakeSweeps` builds one activity per name in
  `SWEEPS`, so it covers the new one; add a recorded history to
  `tests/fixtures/housekeeping_histories/` with the patch, as `REAP_PATCH`'s was.
- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_activity_sweep.py tests/test_housekeeping.py tests/test_housekeeping_replay.py -v` — PASS.
- [ ] **Step 5: Commit** `feat(activity): retention and repair as a housekeeping sweep`.

---

### Task 9: `via_tool` from the agent

**Files:**
- Modify: `agent/src/tools/authorship.ts`
- Test: `agent/test/authoring.test.ts`

**Interfaces:**
- Produces: `authorHeaders(principal, session?, tool?)` adds `X-ScadBuddy-Agent-Tool`; `runJudgedByResult` passes `tool.name`.

- [ ] **Step 1: Failing test** (beside the existing author-header asserts, ~line 134):

```ts
it('names the tool that made the request', async () => {
  let seen: string | null = null
  server.use(http.post(`${BACKEND}/api/v1/models`, ({ request }) => {
    seen = request.headers.get('X-ScadBuddy-Agent-Tool')
    return HttpResponse.json(createdModel)
  }))
  await tool('create_model').execute(createArgs, ctx())
  expect(seen).toBe('create_model')
})
```

  (Reuse that file's existing `createdModel`/`createArgs`/`ctx` names; read the nearest
  existing author test and copy its request and fixtures.)
- [ ] **Step 2: Run** `cd agent && pnpm test -- authoring` — FAIL.
- [ ] **Step 3: Implement**

```ts
export const TOOL_HEADER = 'X-ScadBuddy-Agent-Tool'
export function authorHeaders(principal: Principal, session?: string, tool?: string): Record<string, string> {
  return {
    [AUTHOR_HEADER]: principalHeader(principal.id),
    ...(session && /^[A-Za-z0-9_-]{1,64}$/.test(session) ? { [AUTHOR_SESSION_HEADER]: session } : {}),
    ...(tool && /^[A-Za-z0-9_-]{1,64}$/.test(tool) ? { [TOOL_HEADER]: tool } : {}),
  }
}
```

  and in `registry.ts` `runJudgedByResult`: `authorHeaders(ctx.principal, ctx.session, tool.name)`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(agent): name the tool behind each backend request`.

---

### Task 10: Agent operations carry who, and announce themselves; agent kinds as data

**Files:**
- Create: `agent/src/db/migrations/<ts>_activity.sql`, `agent/src/activity/kinds.ts`
- Modify: `agent/src/temporal/names.ts`, `agent/src/temporal/operationActivities.ts`, `agent/src/operations/store.ts`, `agent/src/operations/run.ts`, `agent/src/routes/pluginPackages.ts`, `agent/src/main.ts`
- Test: `agent/test/operations.pg.test.ts`, `agent/test/activityKinds.pg.test.ts`, `agent/test/agentOperation.temporal.test.ts`

**Interfaces:**
- Produces:
  - `OperationInput.actor?: { principal: string; session?: string; viaTool?: string }`
  - `InsertOperation.actor?` (same shape)
  - `Commands.run(kind, request, idempotencyKey, actor?)`
  - `upsertActivityKinds(sql, kinds: ActivityKindDecl[]): Promise<void>`
  - `ActivityKindDecl = { key: string; label: string; facets: { name: string; type: 'keyword'|'number'|'bool'|'time'; label: string }[] }`
  - bus event `{ kind: 'ai_operation.changed', operation_id }`

- [ ] **Step 1: Migration**

```sql
-- Activity (spec 2026-10-09 principal jobs §4, §5): who started each agent operation, and
-- the agent's activity kinds as data the backend reads (it never writes them).
ALTER TABLE ai_operations ADD COLUMN principal text, ADD COLUMN session text, ADD COLUMN via_tool text;
CREATE TABLE ai_activity_kinds (
  key        text PRIMARY KEY CHECK (key LIKE 'agent.%'),
  label      text NOT NULL,
  facets     jsonb NOT NULL DEFAULT '[]'::jsonb,
  retired_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 2: Failing tests** in `test/operations.pg.test.ts`:

```ts
it('records who started it and announces it on the bus', async () => {
  const heard: string[] = []
  const listener = await db.sql.listen('scadbuddy_events', (payload) => heard.push(payload))
  const op = await store.insert({ ...op('a1', 'r1', 'k1'), actor: { principal: 'browser', viaTool: 'plugin_install' } })
  const [row] = await db.sql`SELECT principal, via_tool FROM ai_operations WHERE id = ${op.id}`
  expect(row).toEqual({ principal: 'browser', via_tool: 'plugin_install' })
  await until(() => heard.some((p) => JSON.parse(p).kind === 'ai_operation.changed'))
  await store.finish(op.id, { result: null })
  await until(() => heard.filter((p) => JSON.parse(p).operation_id === op.id).length === 2)
  await listener.unlisten()
})
```

  and `test/activityKinds.pg.test.ts`:

```ts
it('upserts declared kinds and retires the rest', async () => {
  await upsertActivityKinds(db.sql, [{ key: 'agent.a', label: 'A', facets: [] }, { key: 'agent.b', label: 'B', facets: [] }])
  await upsertActivityKinds(db.sql, [{ key: 'agent.a', label: 'A2', facets: [] }])
  const rows = await db.sql`SELECT key, label, retired_at IS NOT NULL AS retired FROM ai_activity_kinds ORDER BY key`
  expect(rows).toEqual([{ key: 'agent.a', label: 'A2', retired: false }, { key: 'agent.b', label: 'B', retired: true }])
})
```

  (`until` is in `test/sessionTools.pg.test.ts`; move it to `test/support/` if it is
  file-local.)
- [ ] **Step 3: Run** `SCADBUDDY_TEST_DATABASE_URL=… pnpm test -- operations.pg activityKinds` — FAIL.
- [ ] **Step 4: Implement.** In `store.ts` `insert`, add `principal`, `session`, `via_tool`
  to the `INSERT` (`${op.actor?.principal ?? null}` …), and inside the same `tx`:

```ts
await tx`SELECT pg_notify(${PG_CHANNEL}, ${JSON.stringify({
  id: randomUUID().replaceAll('-', ''), at: new Date().toISOString(), kind: 'ai_operation.changed', operation_id: inserted.id,
})})`
```

  (only when inserted). Wrap `finish` in `this.sql.begin` and notify the same way after the
  `UPDATE`. `operationActivities.ts` insert passes `actor: input.actor`. `names.ts`
  `OperationInput` gains `actor?`. `run.ts` `Commands.run(kind, request, key, actor?)`
  puts `actor` into the input. `pluginPackages.ts` lines ~195 and ~246 pass
  `{ principal: UI_ACTOR.id }`. The new optional input field does not change the
  workflow's commands: `AgentOperation` passes `{ input }` through, so no `patched()`.
  Prove it: the existing `replays its recorded history` test must still pass, and record a
  second fixture `succeeded-with-actor.json` (add a constant and a record/replay pair as
  the existing one does, `RECORD_AGENT_OPERATION_HISTORY=1`) with `actor` set.

  `activity/kinds.ts`:

```ts
import type { Sql } from 'postgres'

export type ActivityKindDecl = {
  key: string
  label: string
  facets: { name: string; type: 'keyword' | 'number' | 'bool' | 'time'; label: string }[]
}

/** The agent's activity kinds (spec 2026-10-09 §4), from its operation kinds: upserted at
 *  start, and every row not declared now retired, never deleted (old rows keep a label). */
export async function upsertActivityKinds(sql: Sql, kinds: ActivityKindDecl[]): Promise<void> {
  await sql.begin(async (tx) => {
    for (const k of kinds) {
      await tx`
        INSERT INTO ai_activity_kinds (key, label, facets) VALUES (${k.key}, ${k.label}, ${tx.json(k.facets as never)})
        ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label, facets = EXCLUDED.facets, retired_at = NULL, updated_at = now()`
    }
    await tx`UPDATE ai_activity_kinds SET retired_at = now() WHERE retired_at IS NULL AND key <> ALL(${kinds.map((k) => k.key)})`
  })
}

export function activityKindsOf(kinds: readonly { name: string; label?: string }[]): ActivityKindDecl[] {
  return kinds.map((k) => ({ key: `agent.${k.name}`, label: k.label ?? k.name.replaceAll('_', ' '), facets: [] }))
}
```

  Add an optional `label` to the agent's `OperationKind` (`src/operations/kinds.ts`) and
  set it on the two package kinds (`'Install plugin package'`, `'Re-pin plugin package'`).
  In `main.ts`, after `commandKinds` is built (~line 318):

```ts
if (database && commandKinds.length > 0) {
  void database.ready().then((ok) => ok && upsertActivityKinds(database.sql, activityKindsOf(commandKinds)))
    .catch((err: unknown) => log.warn({ err }, 'could not record the activity kinds'))
}
```

  (match `main.ts`'s logger name.)
- [ ] **Step 5: Run** the agent suite: `pnpm lint && pnpm typecheck && pnpm test` — PASS.
- [ ] **Step 6: Commit** `feat(agent): agent operations record who and announce; activity kinds as data`.

---

### Task 11: `activity_list`, `activity_get`, `activity_watch`

**Files:**
- Create: `agent/src/tools/activity.ts`
- Modify: `agent/src/tools/index.ts`, `agent/src/tools/coverage.ts`
- Test: `agent/test/activityTools.test.ts`

**Interfaces:**
- Consumes: backend `GET /api/v1/activity`, `GET /api/v1/activity/{activity_id}` (Task 7); `principalHeader` (`tools/authorship.ts`); `hasTier` (`auth/principal.ts`).
- Produces: `activityTools: Tool[]`.

- [ ] **Step 1: Failing tests** (copy `test/tools.test.ts`'s header: msw `setupServer`, `ctx()`, `BACKEND`):

```ts
it('activity_list pins a read-tier caller to itself', async () => {
  let asked: URL | undefined
  server.use(http.get(`${BACKEND}/api/v1/activity`, ({ request }) => {
    asked = new URL(request.url)
    return HttpResponse.json({ items: [], next_cursor: null })
  }))
  await tool('activity_list').execute({ principal: 'browser' }, ctx({ principal: { id: 'token:t1', kind: 'bearer', tiers: ['read'] } }))
  expect(asked?.searchParams.get('principal')).toBe('token:t1')
})

it('activity_list lets an outward caller filter anyone', async () => {
  let asked: URL | undefined
  server.use(http.get(`${BACKEND}/api/v1/activity`, ({ request }) => {
    asked = new URL(request.url)
    return HttpResponse.json({ items: [], next_cursor: null })
  }))
  await tool('activity_list').execute({ principal: 'browser' }, ctx())
  expect(asked?.searchParams.get('principal')).toBe('browser')
})

it('activity_list pins the encoded principal', async () => {
  let asked: URL | undefined
  server.use(http.get(`${BACKEND}/api/v1/activity`, ({ request }) => {
    asked = new URL(request.url)
    return HttpResponse.json({ items: [], next_cursor: null })
  }))
  const id = 'oidc:https://idp.example/#sub one'
  await tool('activity_list').execute({}, ctx({ principal: { id, kind: 'oidc', tiers: ['read'] } }))
  expect(asked?.searchParams.get('principal')).toBe(principalHeader(id))
})

it('activity_get refuses another principal’s row to a read-tier caller', async () => {
  server.use(http.get(`${BACKEND}/api/v1/activity/render%3Aa`, () =>
    HttpResponse.json({ row: { id: 'render:a', principal: 'browser' }, children: [], links: [], detail: {} })))
  const result = await runTool(tool('activity_get'), { activity_id: 'render:a' },
    ctx({ principal: { id: 'token:t1', kind: 'bearer', tiers: ['read'] } }))
  expect(result.isError).toBe(true)
})

it('activity_watch returns when the row finishes', async () => {
  let reads = 0
  server.use(http.get(`${BACKEND}/api/v1/activity/render%3Aa`, () => {
    reads += 1
    const status = reads < 3 ? 'running' : 'succeeded'
    return HttpResponse.json({ row: { id: 'render:a', principal: 'test', status }, children: [], links: [], detail: {} })
  }))
  const result = await tool('activity_watch').execute({ activity_id: 'render:a', wait_seconds: 5 },
    ctx({ pollIntervalMs: 10 }))
  expect(JSON.stringify(result.content)).toContain('succeeded')
})
```

- [ ] **Step 2: Run** `pnpm test -- activityTools` — FAIL.
- [ ] **Step 3: Implement**

```ts
import { z } from 'zod'
import { hasTier } from '../auth/principal.js'
import { principalHeader } from './authorship.js'
import { ok } from './call.js'
import { defineTool, json, ToolError, type ToolContext } from './registry.js'

const STATUS = z.enum(['queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled'])
const FINISHED = new Set(['succeeded', 'failed', 'cancelled'])
const ID = z.string().min(3).max(200).describe('An activity id, e.g. "render:<id>" or "operation:<id>"')

/** Spec 2026-10-09 §7: a caller without `outward` sees its own activity only. */
function pinned(ctx: ToolContext, asked: string | undefined): string | undefined {
  return hasTier(ctx.principal, 'outward') ? asked : principalHeader(ctx.principal.id)
}

async function read(ctx: ToolContext, activity_id: string) {
  const found = await ok(
    ctx.backend.GET('/api/v1/activity/{activity_id}', { params: { path: { activity_id } } }),
    `get activity ${activity_id}`,
  )
  const mine = principalHeader(ctx.principal.id)
  if (!hasTier(ctx.principal, 'outward') && found.row.principal !== mine) {
    throw new ToolError(`activity ${activity_id} was not started by this caller`, 404)
  }
  return found
}

export const activityTools = [
  defineTool({
    name: 'activity_list',
    description:
      'List background work (renders, operations, print runs, agent operations), newest first, with its status ' +
      'and who started it. Without the outward tier only your own.',
    input: z.object({
      kind: z.string().max(100).optional(),
      status: STATUS.optional(),
      principal: z.string().max(300).optional().describe('Only with the outward tier; otherwise yours'),
      subject: z.string().max(200).optional(),
      since: z.string().datetime().optional(),
      cursor: z.string().max(400).optional(),
      limit: z.number().int().min(1).max(200).optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/activity'],
    handler: async (args, ctx) => {
      const query = { ...args, principal: pinned(ctx, args.principal) }
      return json(await ok(ctx.backend.GET('/api/v1/activity', { params: { query } }), 'list activity'))
    },
  }),
  defineTool({
    name: 'activity_get',
    description: 'Read one piece of background work: status, stages, result or error, and what it made.',
    input: z.object({ activity_id: ID }),
    risk: 'read',
    routes: ['GET /api/v1/activity/{activity_id}'],
    handler: async ({ activity_id }, ctx) => json(await read(ctx, activity_id)),
  }),
  defineTool({
    name: 'activity_watch',
    description: 'Wait up to wait_seconds for a piece of background work to finish, then return it.',
    input: z.object({ activity_id: ID, wait_seconds: z.number().int().min(1).max(600).default(60) }),
    risk: 'read',
    routes: ['GET /api/v1/activity/{activity_id}'],
    handler: async ({ activity_id, wait_seconds }, ctx) => {
      const deadline = Date.now() + wait_seconds * 1000
      let found = await read(ctx, activity_id)
      while (!FINISHED.has(found.row.status) && Date.now() < deadline && !ctx.signal.aborted) {
        await new Promise((resolve) => setTimeout(resolve, ctx.pollIntervalMs))
        found = await read(ctx, activity_id)
        await ctx.progress(0, undefined, found.row.status)
      }
      return json(found)
    },
  }),
]
```

  Add `...activityTools` to `ALL_TOOLS` in `tools/index.ts`, and the `NOT_A_TOOL` entry for
  `GET /api/v1/activity/facets` (Task 7 Step 5). Check `ctx.progress`'s real signature in
  `registry.ts` and match it.
- [ ] **Step 4: Run** `pnpm lint && pnpm typecheck && pnpm test` — PASS, including
  `test/coverage.test.ts`.
- [ ] **Step 5: Commit** `feat(agent): activity_list, activity_get and activity_watch, pinned by tier`.

---

### Task 12: Frontend client, mocks, query state

**Files:**
- Create: `frontend/src/lib/activityQuery.ts`, `frontend/src/lib/advanced.ts`, `frontend/src/mocks/features/activity.ts`
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`, `frontend/src/pages/LibraryPage.tsx` (use `lib/advanced.ts`)
- Test: `frontend/src/lib/activityQuery.test.ts`

**Interfaces:**
- Produces:
  - types `Activity = Schemas['ActivityRow']`, `ActivityPage`, `ActivityFacets`, `ActivityDetail`
  - `api.listActivity(query: ActivityQuery, page?: { cursor?: string | null; limit?: number })`
  - `api.activityFacets(since?: string)`, `api.getActivity(id: string, advanced: boolean)`
  - `ActivityQuery = { kind: string; status: string; principal: string; tool: string; subject: string; since: string; q: string; facets: Record<string, string> }`
  - `parseActivityQuery(params: URLSearchParams): ActivityQuery`, `toActivityParams(q): URLSearchParams`, `apiParams(q): Record<string,string>` (maps `tool`→`via_tool`, `facets.x`→`facet.x`)
  - `useAdvanced(key: string): [boolean, () => void]` in `lib/advanced.ts`
  - mock helpers `seedActivity(rows)`, `setActivityFacets(f)`, `reset()`

- [ ] **Step 1: Failing test**

```ts
import { describe, expect, it } from 'vitest'
import { apiParams, parseActivityQuery, toActivityParams } from './activityQuery'

describe('activityQuery', () => {
  it('round-trips through the URL, facets included, defaults left out', () => {
    const q = parseActivityQuery(new URLSearchParams('kind=render&status=failed&facet.slug=box'))
    expect(q.facets).toEqual({ slug: 'box' })
    expect(toActivityParams(q).toString()).toBe('kind=render&status=failed&facet.slug=box')
  })
  it('maps to the API names', () => {
    const q = parseActivityQuery(new URLSearchParams('tool=render_model&facet.slug=box'))
    expect(apiParams(q)).toEqual({ via_tool: 'render_model', 'facet.slug': 'box' })
  })
})
```

- [ ] **Step 2: Run** `cd frontend && pnpm test -- activityQuery` — FAIL.
- [ ] **Step 3: Implement** `activityQuery.ts` after `lib/printsQuery.ts`'s shape (fixed keys
  `kind,status,principal,tool,subject,since,q`; every `facet.<name>` key into `facets`).
  `lib/advanced.ts` is `LibraryPage.tsx`'s `readAdvanced`/`toggleAdvanced` lifted into a
  hook keyed by a storage key; switch `LibraryPage` to `useAdvanced('scadbuddy.library.advanced')`
  (its tests must still pass unchanged). Client functions after `listPrints`:

```ts
  listActivity: (query: ActivityQuery, page: { cursor?: string | null; limit?: number } = {}) => {
    const search = new URLSearchParams(apiParams(query))
    if (page.limit !== undefined) search.set('limit', String(page.limit))
    if (page.cursor) search.set('cursor', page.cursor)
    const suffix = search.size > 0 ? `?${search.toString()}` : ''
    return request<ActivityPage>(`/activity${suffix}`)
  },
  activityFacets: (since?: string) =>
    request<ActivityFacets>(`/activity/facets${since ? `?since=${encodeURIComponent(since)}` : ''}`),
  getActivity: (id: string, advanced: boolean) =>
    request<ActivityDetail>(`/activity/${seg(id)}${advanced ? '?advanced=1' : ''}`),
```

  `mocks/features/activity.ts` exports `handlers` for the three routes over an in-memory
  list (filtering by every param the backend takes, and 400 for unknown ones as the
  backend does), plus `seedActivity`, `setActivityFacets`, `reset`. Seed defaults: three
  rows (a running render by `browser`, a failed `operation.model_import` by `token:t1` via
  `import_model`, a succeeded `agent.plugin_package_install`), and facets that include a
  kind the client has no code for (`kind: "operation.font_install"`).
- [ ] **Step 4: Run** `pnpm lint && pnpm typecheck && pnpm test` — PASS.
- [ ] **Step 5: Commit** `feat(frontend): activity client, URL query and mocks`.

---

### Task 13: The Activity page — filters from facets, live table

**Files:**
- Create: `frontend/src/pages/ActivityPage.tsx`, `frontend/src/components/activity/ActivityFilters.tsx`, `frontend/src/components/activity/ActivityTable.tsx`, `frontend/src/components/activity/labels.ts`
- Modify: `frontend/src/App.tsx`, `frontend/src/components/AppShell.tsx` (`NAV`)
- Test: `frontend/src/pages/ActivityPage.test.tsx`

**Interfaces:**
- Consumes: Task 12.
- Produces: route `/activity`; `principalLabel(p: string | null, facets: ActivityFacets): string` (`browser`→"You", `system`→"System", `null`→"Unknown", else the value), `statusLabel` reused from `components/prints/status.ts`.

- [ ] **Step 1: Failing tests**

```tsx
describe('ActivityPage', () => {
  beforeEach(() => { resetMockState(); window.localStorage.clear() })

  it('builds its filter chips from the facets, including a kind it has no code for', async () => {
    renderPage(<ActivityPage />, { route: '/activity' })
    expect(await screen.findByRole('button', { name: /Font install/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^You/ })).toBeInTheDocument()
  })

  it('filters by status and keeps it in the URL', async () => {
    const { user } = renderPage(<ActivityPage />, { route: '/activity' })
    await user.click(await screen.findByRole('button', { name: /^Failed/ }))
    await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(1))
    expect(window.location.search).toContain('status=failed') // or assert via a location probe in renderPage
  })

  it('shows a kind’s own facets once the kind is selected', async () => {
    const { user } = renderPage(<ActivityPage />, { route: '/activity' })
    expect(screen.queryByLabelText('Model')).toBeNull()
    await user.click(await screen.findByRole('button', { name: /^Render/ }))
    expect(await screen.findByLabelText('Model')).toBeInTheDocument()
  })

  it('updates live when an activity changes', async () => {
    renderPage(<ActivityPage />, { route: '/activity' })
    await screen.findAllByTestId(/^activity-row-/)
    seedActivity([{ id: 'render:new', kind: 'render', status: 'running', title: 'Render box', principal: 'browser', source: 'render_jobs', source_id: 'new' }])
    emitRealtime('activity.changed', ['activity'], { activity_id: 'render:new' })
    expect(await screen.findByText('Render box')).toBeInTheDocument()
  })
})
```

  (If `renderPage`'s `MemoryRouter` does not expose the location, add a tiny
  `LocationProbe` in the test as other page tests do — `grep -rn "useLocation" src/**/*.test.tsx`.)
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** `ActivityPage`: `useSearchParams` → `parseActivityQuery`;
  `useAsync(() => api.activityFacets(), [], ['activity'])` and
  `useAsync(() => api.listActivity(query), [toActivityParams(query).toString()], ['activity'])`
  (the `activity` topic re-reads both); "Load more" uses `next_cursor`. `ActivityFilters`
  renders chip groups for kinds (label from facets), statuses (`statusLabel`), principals
  (`principalLabel`, with count), tools and subjects, using `CatalogueFilters.tsx`'s
  `chipClass` (export it or copy it into `activity/labels.ts`), a `since` select (24 h,
  7 d, 30 d), a search box (`sb-field`), and, when exactly one kind is selected, one
  input per filterable facet of that kind labelled by `facet.label`. No list of kinds,
  statuses or facets appears in frontend code; only the six status *labels* (via
  `statusLabel`'s generic snake-case fallback) and the three principal labels. `ActivityTable`
  follows `RememberedChoicesPanel`'s table: status, title, started by, started (relative
  time), duration; rows with `parent_id` nest under their parent when both are loaded;
  each row is a button that opens the drawer (Task 14) and has `data-testid="activity-row-<id>"`.
  Add `<Route path="activity" element={<ActivityPage />} />` and
  `{ to: '/activity', label: 'Activity', end: false }` to `NAV`.
- [ ] **Step 4: Run** `pnpm lint && pnpm typecheck && pnpm test` — PASS.
- [ ] **Step 5: Commit** `feat(frontend): Activity page with filters built from facets and live rows`.

---

### Task 14: The detail drawer, Simple and Advanced

**Files:**
- Create: `frontend/src/components/activity/ActivityDrawer.tsx`
- Modify: `frontend/src/pages/ActivityPage.tsx`
- Test: `frontend/src/components/activity/ActivityDrawer.test.tsx`

**Interfaces:**
- Consumes: `api.getActivity`, `useAdvanced('scadbuddy.activity.advanced')`; settings' `temporal_ui_url` (`api.getSettings()`; check the field name in `api/types.ts`).

- [ ] **Step 1: Failing tests**

```tsx
it('Simple shows status, links and the error in words; Advanced adds ids and raw JSON', async () => {
  const { user } = renderPage(<ActivityDrawer id="operation:o1" onClose={() => {}} />)
  expect(await screen.findByText('Failed')).toBeInTheDocument()
  expect(screen.queryByText(/op-model_import/)).toBeNull()
  await user.click(screen.getByRole('switch', { name: 'Advanced' }))
  expect(await screen.findByText(/op-model_import/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Temporal/ })).toHaveAttribute('href', expect.stringContaining('op-model_import'))
})

it('closes on Escape and returns focus', async () => { /* as Dialog.test.tsx does */ })
```

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** a right-anchored panel built from `components/ui/Dialog.tsx`
  (add a `placement?: 'center' | 'right'` prop to `Dialog` that swaps the container's
  centring classes for `justify-end` and `h-full max-w-[480px] rounded-none`; keep its
  focus handling). Content: Simple — status, title, started by, timing, a stage timeline
  from `detail.stages` when present, `links` as anchors, the error's `detail` text.
  Advanced (the same switch markup as the Library page, `aria-label` "Advanced") —
  workflow and run ids, a Temporal link (`<temporal_ui_url>/namespaces/<ns>/workflows/<workflow_id>`,
  only when the setting is set), trace id, facets, `via_tool` (linking to
  `/settings#plugins` until #1953 gives a tool anchor), and `<pre>` JSON of request,
  result and error. Children listed as rows that open in the same drawer. Re-read on the
  `activity:<id>` topic.
- [ ] **Step 4: Run** `pnpm lint && pnpm typecheck && pnpm test` — PASS.
- [ ] **Step 5: Commit** `feat(frontend): activity detail drawer, Simple and Advanced`.

---

### Task 15: The bell, toasts, and who to follow

**Files:**
- Create: `frontend/src/components/activity/ActivityBell.tsx`, `frontend/src/components/ui/Toasts.tsx`
- Modify: `frontend/src/components/AppShell.tsx` (beside `LiveUpdatesIndicator`), `frontend/src/pages/SettingsPage.tsx` / `pages/settings/fields.ts` (the `activity_follow`, `activity_ttl_seconds`, `activity_repair_window_seconds` fields, the last under `advanced: true`)
- Test: `frontend/src/components/activity/ActivityBell.test.tsx`

**Interfaces:**
- Consumes: `api.listActivity({ principal: <each followed> , since: <bell opened at> })`, topic `activity`, settings `activity_follow`.
- Produces: `<Toasts />` host and `toast({ title, href })`.

- [ ] **Step 1: Failing tests**

```tsx
it('counts finished activity from followed principals and toasts on finish', async () => {
  const { user } = renderPage(<><ActivityBell /><Toasts /></>)
  seedActivity([{ id: 'render:x', kind: 'render', status: 'succeeded', title: 'Render box', principal: 'browser', source: 'render_jobs', source_id: 'x', finished_at: new Date().toISOString() }])
  emitRealtime('activity.changed', ['activity'], { activity_id: 'render:x', principal: 'browser' })
  expect(await screen.findByRole('status')).toHaveTextContent('Render box')
  expect(screen.getByTestId('activity-bell-count')).toHaveTextContent('1')
  await user.click(screen.getByRole('button', { name: /Activity, 1 finished/ }))
  expect(screen.getByTestId('activity-bell-count')).not.toBeVisible()
})

it('ignores principals not followed', async () => { /* seed principal token:t1, follow ["browser"]; no toast */ })
```

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** The bell subscribes to `activity`; it drops a signal whose
  `principal` is set and not followed, and debounces the rest (500 ms, trailing) so a
  burst of render status writes costs one re-read. It then re-reads
  `listActivity` for each followed principal since the last-seen time (kept in
  `localStorage`, `scadbuddy.activity.seen`), counts rows that finished after it, and
  toasts each new one (`role="status"`, auto-dismiss 6 s, link to `/activity?…`). Clicking
  opens a popover with those rows and a link to the page, and marks them seen. `Toasts`
  is a fixed bottom-right stack with an `aria-live="polite"` region. Settings: add the three
  stored settings to `fields.ts` the way other list/number settings are declared.
- [ ] **Step 4: Run** `pnpm lint && pnpm typecheck && pnpm test` — PASS.
- [ ] **Step 5: Commit** `feat(frontend): activity bell and toasts for followed principals`.

---

### Task 16: End to end

**Files:**
- Create: `frontend/e2e/activity.spec.ts`
- Modify: `frontend/e2e/real-backend.spec.ts` (one real-stack check)

- [ ] **Step 1: Mocked spec**

```ts
import { expect, test } from '@playwright/test'

test.describe('activity', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  test('filters, opens a row, and shows Advanced', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('link', { name: 'Activity' }).click()
    await page.getByRole('button', { name: /^Failed/ }).click()
    await expect(page).toHaveURL(/status=failed/)
    await page.getByTestId(/activity-row-operation/).first().click()
    await page.getByRole('switch', { name: 'Advanced' }).click()
    await expect(page.getByText(/op-model_import/)).toBeVisible()
  })
})
```

- [ ] **Step 2: Real stack.** In `real-backend.spec.ts`, render a model, open Activity,
  and expect a `Render <slug>` row by "You" that reaches Succeeded.
- [ ] **Step 3: Run** `pnpm exec playwright test e2e/activity.spec.ts` — PASS.
- [ ] **Step 4: Run everything CI runs** (backend ruff, format, mypy, pytest `-n auto`;
  agent lint/typecheck/test; frontend lint/typecheck/test/build; e2e).
- [ ] **Step 5: Commit** `test(activity): e2e`.

---

## Self-review notes

- Spec coverage: §3 record → T1/T2; §4 registry → T3, agent kinds T10; §5 writers →
  T4/T5/T6, failure isolation T2; §6 API → T7 (SSE replaced, deviation 2;
  `Job-Location` → `Activity-Location`, T7); §7 visibility → T7 (headless), T11 (tiers);
  §8 retention/repair → T8; §9 UI → T12–T15; §10 errors → T7; §11 testing → each task.
- #1960's review: unknown params checked explicitly (T7); agent workflow change shown
  replay-safe with a second fixture (T10); session owner columns named and verified (T7);
  `SELECT` only on `ai_*`, no `LISTEN` (deviation 4).
- Depends on #1953 only for `via_tool`'s link target (T14 falls back to Settings).
