"""`GET /filament/sync/status` and `POST /filament/sync/run` (#6265 Task 10, plan
Decision 19): the sync's state for a person and the agent, and "Sync now". The reset of
the Bambuddy links after the Bambuddy URL changes (#6773), which keeps them when the new
URL reaches the same Bambuddy (#6845)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.filament.bb_sync_nudge import FilamentSyncNudger, instance_for
from tests.api.test_filament import _location, _spool, _type
from tests.api.test_filament_purchasing import _supplier
from tests.api.test_send import API, BASE, configure

pytestmark = pytest.mark.requires_postgres

SYNC = "/api/v1/filament/sync"


#: The real nudge, kept before `filament_nudges` (autouse) silences it.
REAL_NUDGE = FilamentSyncNudger.nudge


class FakeTemporal:
    """The Temporal client the nudger gets: it records each start's signal args, or
    raises ``fail``."""

    def __init__(self) -> None:
        self.starts: list[list[Any]] = []
        self.fail: BaseException | None = None

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        if self.fail is not None:
            raise self.fail
        self.starts.append(kwargs["start_signal_args"])


@pytest.fixture
def temporal(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> FakeTemporal:
    """The app's real `FilamentSyncNudger.nudge`, over a fake Temporal client."""
    fake = FakeTemporal()
    monkeypatch.setattr(FilamentSyncNudger, "nudge", REAL_NUDGE)
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    monkeypatch.setattr(state.filament_sync, "client_getter", lambda: fake)
    return fake


def test_status_lists_errors_and_directions(client: TestClient, pg_conninfo: str) -> None:
    configure(client, filament_sync={"tags": "two_way", "suppliers": "sb_to_bb"})
    older, newer = uuid4(), uuid4()
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "UPDATE filament_bb_sync_state SET instance = 'abcdef012345',"
            " last_pass_at = %s, last_pass_started_at = %s, last_ok_at = %s,"
            " last_error = 'bambuddy-down', counts = %s",
            (
                datetime(2026, 11, 20, 10, tzinfo=UTC),
                datetime(2026, 11, 20, 9, 59, tzinfo=UTC),
                datetime(2026, 11, 20, 9, tzinfo=UTC),
                Jsonb({"written": 2, "failed": 1, "deferred": 0, "conflicts": 0, "unmapped_k": 0}),
            ),
        )
        conn.execute(
            "INSERT INTO filament_bb_push_errors (kind, sb_id, error, attempts, at)"
            " VALUES ('spool', %s, 'http-500', 3, %s), ('supplier', %s, 'name-taken', 1, %s)",
            (
                older,
                datetime(2026, 11, 20, 8, tzinfo=UTC),
                newer,
                datetime(2026, 11, 20, 10, tzinfo=UTC),
            ),
        )

    got = client.get(f"{SYNC}/status")

    assert got.status_code == 200, got.text
    body = got.json()
    assert body["instance"] == "abcdef012345"
    assert body["last_error"] == "bambuddy-down"
    assert body["last_pass_at"].startswith("2026-11-20T10:00")
    assert body["last_pass_started_at"].startswith("2026-11-20T09:59")
    assert body["last_ok_at"].startswith("2026-11-20T09:00")
    assert body["counts"] == {
        "written": 2,
        "failed": 1,
        "deferred": 0,
        "conflicts": 0,
        "unmapped_k": 0,
    }
    assert [(e["kind"], e["sb_id"], e["error"], e["attempts"]) for e in body["errors"]] == [
        ("supplier", str(newer), "name-taken", 1),
        ("spool", str(older), "http-500", 3),
    ]
    assert body["directions"] == {
        "spools": "bb_to_sb",
        "weights": "bb_to_sb",
        "tags": "two_way",
        "presets": "bb_to_sb",
        "suppliers": "sb_to_bb",
        "locations": "off",
    }
    assert body["skipped"] is None


def test_status_labels_each_error_row(client: TestClient, pg_conninfo: str) -> None:
    """R-T11-1: each error row names its item as the UI shows it, a spool by its `#N`, a
    supplier by its name; a row whose item is gone has no label."""
    spool = _spool(client)
    supplier = _supplier(client, "Micro Center")
    gone = uuid4()
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "INSERT INTO filament_bb_push_errors (kind, sb_id, error, attempts, at)"
            " VALUES ('spool', %s, 'http-500', 1, %s), ('supplier', %s, 'name_taken', 1, %s),"
            " ('location', %s, 'http-500', 1, %s)",
            (
                spool["id"],
                datetime(2026, 11, 20, 10, tzinfo=UTC),
                supplier,
                datetime(2026, 11, 20, 9, tzinfo=UTC),
                gone,
                datetime(2026, 11, 20, 8, tzinfo=UTC),
            ),
        )

    got = client.get(f"{SYNC}/status")

    assert got.status_code == 200, got.text
    assert [(e["kind"], e["label"]) for e in got.json()["errors"]] == [
        ("spool", f"#{spool['number']}"),
        ("supplier", "Micro Center"),
        ("location", None),
    ]


def test_status_says_why_no_pass_runs(client: TestClient) -> None:
    """R-T8-2: the nudge starts nothing with no Bambuddy, or when nothing pushes; the
    status names which, in the pass's own ``skipped`` words."""
    fresh = client.get(f"{SYNC}/status").json()
    assert fresh["skipped"] == "no_bambuddy"
    assert fresh["instance"] is None
    assert fresh["last_pass_at"] is None
    assert fresh["errors"] == []

    configure(client)
    assert client.get(f"{SYNC}/status").json()["skipped"] == "nothing_pushes"

    # The engine never pushes tags (Decision 6): tags alone is still nothing.
    configure(client, filament_sync={"tags": "two_way"})
    assert client.get(f"{SYNC}/status").json()["skipped"] == "nothing_pushes"

    configure(client, filament_sync={"locations": "two_way"})
    assert client.get(f"{SYNC}/status").json()["skipped"] is None


def test_run_nudges_full(
    client: TestClient, filament_nudges: list[bool], temporal: FakeTemporal
) -> None:
    configure(client, filament_sync={"spools": "two_way"})
    filament_nudges.clear()

    before = datetime.now(UTC)
    ran = client.post(f"{SYNC}/run")
    after = datetime.now(UTC)

    assert ran.status_code == 202, ran.text
    body = ran.json()
    assert body["started"] is True
    # #6836: the server's own time, taken before it asked for the pass.
    assert before <= datetime.fromisoformat(body["requested_at"]) <= after
    assert temporal.starts == [[True]]
    # The route nudges itself: `nudge_after_write` adds no plain nudge after it.
    assert filament_nudges == []


def test_run_that_cannot_reach_temporal_is_503(
    client: TestClient, filament_nudges: list[bool], temporal: FakeTemporal
) -> None:
    """R-T10-2: no 202 when nothing started; the detail is the label, never the message."""
    configure(client, filament_sync={"spools": "two_way"})
    filament_nudges.clear()
    temporal.fail = RuntimeError("connection refused to 10.0.0.9:7233")

    ran = client.post(f"{SYNC}/run")

    assert ran.status_code == 503, ran.text
    assert ran.json()["detail"] == "could not ask the sync workflow for a pass: internal"
    assert "10.0.0.9" not in ran.text
    assert filament_nudges == []


def test_run_without_bambuddy_is_409(
    client: TestClient, filament_nudges: list[bool], temporal: FakeTemporal
) -> None:
    ran = client.post(f"{SYNC}/run")

    assert ran.status_code == 409, ran.text
    assert ran.json()["code"] == "no_bambuddy"
    assert temporal.starts == []
    assert filament_nudges == []


@pytest.mark.parametrize("sync", [{}, {"tags": "two_way"}], ids=["defaults", "tags-only"])
def test_run_with_nothing_pushing_is_409(
    client: TestClient,
    filament_nudges: list[bool],
    temporal: FakeTemporal,
    sync: dict[str, str],
) -> None:
    configure(client, filament_sync=sync)
    filament_nudges.clear()

    ran = client.post(f"{SYNC}/run")

    assert ran.status_code == 409, ran.text
    assert ran.json()["code"] == "nothing_pushes"
    assert temporal.starts == []
    assert filament_nudges == []


#: The links' Bambuddy in the reset tests: not the configured one (`configure`'s BASE).
OLD = "0ld0ld0ld0ld"
RESET = f"{SYNC}/reset-links"
#: The SB rows a reset must leave alone, with the columns that are their own data.
SB_ROWS = {
    "filament_spools": "id, number, note, archived_at, location_id, type_id",
    "filament_spool_types": "id, code, rgba, archived_at",
    "filament_suppliers": "id, name, archived_at, updated_at",
    "filament_locations": "id, name, parent_id",
    "filament_insert_types": "id, name, empty_weight_g, archived_at, updated_at",
}
#: One of each kind, as `_linked` makes them.
ALL_ONE = {
    "spools": 1,
    "suppliers": 1,
    "locations": 1,
    "insert_types": 1,
    "filaments": 1,
    "intents": 1,
    "errors": 1,
    "ignored": 1,
}
#: When the links' Bambuddy last changed spool 11, to the microsecond.
SEEN_AT = datetime(2026, 11, 20, 10, 0, 0, 123456, tzinfo=UTC)


def _bb_spool(bb_id: int, updated_at: datetime) -> dict[str, Any]:
    """A Bambuddy spool row, its time naive UTC as Bambuddy sends it."""
    return {
        "id": bb_id,
        "material": "PLA",
        "rgba": "00FF00",
        "label_weight": 1000,
        "weight_used": 0.0,
        "updated_at": updated_at.replace(tzinfo=None).isoformat(),
    }


def _listing(*rows: dict[str, Any]) -> respx.Route:
    """The configured Bambuddy's spools, archived ones too, as the reset reads them."""
    return respx.get(f"{API}/inventory/spools", params={"include_archived": "true"}).mock(
        return_value=httpx.Response(200, json=list(rows))
    )


def _sb_rows(conn: psycopg.Connection[Any]) -> dict[str, list[Any]]:
    return {
        table: conn.execute(f"SELECT {cols} FROM {table} ORDER BY id").fetchall()
        for table, cols in SB_ROWS.items()
    }


def _linked(client: TestClient, pg_conninfo: str, instance: str = OLD) -> None:
    """One row of each link kind, with hashes and a seen copy, an intent, an error row
    and an ignored BB spool, all made against ``instance``."""
    kind = _type(client)
    spool = _spool(client, type_id=kind["id"])
    other = _spool(client)
    supplier = _supplier(client, "Old Shop")
    location = _location(client, "Old Shelf")
    insert = client.post(
        "/api/v1/filament/catalog/insert-types", json={"name": "Old Reel", "empty_weight_g": 250}
    )
    assert insert.status_code == 201, insert.text
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, pushed_fields, bb_seen)"
            " VALUES (%s, 11, %s, %s)",
            (spool["id"], Jsonb({"note": "abc"}), Jsonb({"note": "x"})),
        )
        for table, column, row_id in (
            ("filament_suppliers", "bb_supplier_id", supplier),
            ("filament_locations", "bb_location_id", location["id"]),
            ("filament_spool_types", "bb_filament_id", kind["id"]),
            ("filament_insert_types", "bb_catalog_id", insert.json()["id"]),
        ):
            conn.execute(
                f"UPDATE {table} SET {column} = 21, bb_pushed = %s, bb_seen = %s,"
                " bb_seen_at = now() WHERE id = %s",
                (Jsonb({"name": "h"}), Jsonb({"name": "x"}), row_id),
            )
        conn.execute(
            "INSERT INTO filament_bb_creates (kind, sb_id) VALUES ('spool', %s)", (other["id"],)
        )
        conn.execute(
            "INSERT INTO filament_bb_push_errors (kind, sb_id, error)"
            " VALUES ('spool', %s, 'timeout')",
            (other["id"],),
        )
        conn.execute(
            "INSERT INTO filament_bb_ignored (bb_spool_id, reason) VALUES (41, 'rfid_duplicate')"
        )
        conn.execute("UPDATE filament_bb_sync_state SET instance = %s", (instance,))


def _left(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """What the sync still keeps of a Bambuddy: each kind's rows with a link, a hash or a
    seen copy, the intents, error rows, ignored BB spools and the recorded instance."""

    def count(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        assert row is not None
        return int(row[0])

    def kept(table: str, column: str) -> int:
        return count(
            f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL OR bb_seen IS NOT NULL"
            " OR bb_seen_at IS NOT NULL OR bb_pushed <> '{}'"
        )

    return {
        "spools": count("SELECT count(*) FROM filament_bb_links"),
        "suppliers": kept("filament_suppliers", "bb_supplier_id"),
        "locations": kept("filament_locations", "bb_location_id"),
        "insert_types": kept("filament_insert_types", "bb_catalog_id"),
        "filaments": kept("filament_spool_types", "bb_filament_id"),
        "intents": count("SELECT count(*) FROM filament_bb_creates"),
        "errors": count("SELECT count(*) FROM filament_bb_push_errors"),
        "ignored": count("SELECT count(*) FROM filament_bb_ignored"),
        "instance": count("SELECT count(*) FROM filament_bb_sync_state WHERE instance IS NOT NULL"),
    }


def test_status_reports_links_of_another_bambuddy(client: TestClient, pg_conninfo: str) -> None:
    """#6773: the status says the links belong to another Bambuddy than the configured
    one, which is when Settings offers the reset."""
    configure(client, filament_sync={"spools": "two_way"})
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("UPDATE filament_bb_sync_state SET instance = %s", (OLD,))
    assert client.get(f"{SYNC}/status").json()["instance_changed"] is True
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("UPDATE filament_bb_sync_state SET instance = %s", (instance_for(BASE),))
    assert client.get(f"{SYNC}/status").json()["instance_changed"] is False


@respx.mock
def test_the_dry_run_counts_what_a_reset_deletes(client: TestClient, pg_conninfo: str) -> None:
    configure(client)
    _linked(client, pg_conninfo)
    _listing()

    got = client.get(RESET)

    assert got.status_code == 200, got.text
    assert got.json() == {
        "instance": OLD,
        "configured_instance": instance_for(BASE),
        "counts": ALL_ONE,
        "same_bambuddy": False,
        "changed_since_pull": False,
        "bambuddy_unreadable": False,
    }


@respx.mock
def test_a_reset_deletes_the_links_and_no_sb_row(
    client: TestClient, pg_conninfo: str, filament_nudges: list[bool]
) -> None:
    """#6773: one transaction deletes every link, hash, seen copy, intent, error row,
    ignored BB spool and the recorded instance; every SB row stays as it was; then a full
    pass is nudged, and no plain nudge after it. The configured Bambuddy does not hold
    the linked spool, so it is another Bambuddy (#6845); the confirmed delete
    (`expect_kept` false) does not read it (Decision 198)."""
    configure(client, filament_sync={"spools": "two_way"})
    _linked(client, pg_conninfo)
    spools = _listing(_bb_spool(12, SEEN_AT))
    with psycopg.connect(pg_conninfo) as conn:
        before = _sb_rows(conn)
    filament_nudges.clear()

    reset = client.post(RESET, json={"confirm_instance": OLD})

    assert reset.status_code == 200, reset.text
    assert reset.json() == {
        "instance": OLD,
        "kept": False,
        "configured_instance": None,
        "deleted": ALL_ONE,
    }
    assert not spools.called
    with psycopg.connect(pg_conninfo) as conn:
        left = _left(conn)
        assert left == dict.fromkeys(left, 0)
        assert _sb_rows(conn) == before
    assert filament_nudges == [True]
    assert client.get(f"{SYNC}/status").json()["instance_changed"] is False


@respx.mock
def test_a_reset_with_a_wrong_instance_is_409(
    client: TestClient, pg_conninfo: str, filament_nudges: list[bool]
) -> None:
    """A stale page names another instance than the links': nothing is deleted."""
    configure(client)
    _linked(client, pg_conninfo)
    filament_nudges.clear()

    reset = client.post(RESET, json={"confirm_instance": "abcabcabcabc"})

    assert reset.status_code == 409, reset.text
    assert reset.json()["code"] == "confirm_mismatch"
    with psycopg.connect(pg_conninfo) as conn:
        left = _left(conn)
    assert left == dict.fromkeys(left, 1)
    assert filament_nudges == []


@respx.mock
def test_a_reset_with_nothing_to_reset_is_409(
    client: TestClient, pg_conninfo: str, filament_nudges: list[bool]
) -> None:
    """No recorded instance (no links), or links of the configured Bambuddy: 409, and
    nothing is deleted or nudged."""
    configure(client)
    filament_nudges.clear()

    none = client.post(RESET, json={"confirm_instance": OLD})
    assert none.status_code == 409, none.text
    assert none.json()["code"] == "nothing_to_reset"

    _linked(client, pg_conninfo, instance=instance_for(BASE))
    filament_nudges.clear()
    same = client.post(RESET, json={"confirm_instance": instance_for(BASE)})
    assert same.status_code == 409, same.text
    assert same.json()["code"] == "same_instance"
    with psycopg.connect(pg_conninfo) as conn:
        assert _left(conn)["spools"] == 1
    assert filament_nudges == []


@respx.mock
def test_a_reset_waits_for_an_old_instances_result_and_deletes_what_it_wrote(
    client: TestClient, pg_conninfo: str
) -> None:
    """#6773 (Decision 165): `apply_results` and the reset both take the state row's
    `FOR UPDATE` lock, so a result of the old Bambuddy's pass in flight cannot land after
    the reset. One connection holds that lock and writes a link, as `apply_results` does;
    the reset blocks on the lock until it commits, then deletes that link too."""
    configure(client)
    _linked(client, pg_conninfo)
    answer: dict[str, Any] = {}

    def reset() -> None:
        answer["response"] = client.post(RESET, json={"confirm_instance": OLD})

    with psycopg.connect(pg_conninfo) as holder:
        holder.execute("SELECT instance FROM filament_bb_sync_state FOR UPDATE")
        holder.execute(
            "INSERT INTO filament_bb_ignored (bb_spool_id, reason) VALUES (42, 'rfid_duplicate')"
        )
        thread = threading.Thread(target=reset, name="reset waits for the state row")
        thread.start()
        try:
            with psycopg.connect(pg_conninfo, autocommit=True) as watcher:
                deadline = time.monotonic() + 20
                while not watcher.execute(
                    "SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                    " AND query LIKE '%filament_bb_sync_state%FOR UPDATE%'"
                    " AND pid <> pg_backend_pid()"
                ).fetchone():
                    assert time.monotonic() < deadline, "the reset never waited for the lock"
                    time.sleep(0.05)
            assert "response" not in answer
        finally:
            holder.commit()
            thread.join(timeout=20)
    assert not thread.is_alive()
    response = answer["response"]
    assert response.status_code == 200, response.text
    assert response.json() == {
        "instance": OLD,
        "kept": False,
        "configured_instance": None,
        "deleted": {**ALL_ONE, "ignored": 2},
    }
    with psycopg.connect(pg_conninfo) as conn:
        left = _left(conn)
    assert left == dict.fromkeys(left, 0)


@respx.mock
def test_a_reset_to_the_same_bambuddy_keeps_the_links(
    client: TestClient, pg_conninfo: str, filament_nudges: list[bool]
) -> None:
    """#6845 R-6845-3: the new URL reaches the Bambuddy the links were made against (it
    holds linked spool 11 as last seen), so the reset deletes nothing: every link, hash,
    seen copy, intent, error row and ignored spool stays, the configured instance is
    recorded, and a full pass is nudged."""
    configure(client, filament_sync={"spools": "two_way"})
    _linked(client, pg_conninfo)
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("UPDATE filament_bb_links SET bb_updated_at = %s", (SEEN_AT,))
        before = _sb_rows(conn)
    _listing(_bb_spool(11, SEEN_AT), _bb_spool(12, SEEN_AT))

    preview = client.get(RESET)
    assert preview.status_code == 200, preview.text
    assert preview.json()["same_bambuddy"] is True
    assert preview.json()["counts"] == ALL_ONE
    filament_nudges.clear()

    reset = client.post(RESET, json={"confirm_instance": OLD, "expect_kept": True})

    assert reset.status_code == 200, reset.text
    assert reset.json() == {
        "instance": OLD,
        "kept": True,
        "configured_instance": instance_for(BASE),
        "deleted": dict.fromkeys(ALL_ONE, 0),
    }
    with psycopg.connect(pg_conninfo) as conn:
        left = _left(conn)
        assert left == dict.fromkeys(left, 1)
        assert _sb_rows(conn) == before
        state = conn.execute("SELECT instance FROM filament_bb_sync_state").fetchone()
    assert state == (instance_for(BASE),)
    assert filament_nudges == [True]
    assert client.get(f"{SYNC}/status").json()["instance_changed"] is False


@respx.mock
def test_the_preview_says_when_every_linked_spool_changed_since_the_pull(
    client: TestClient, pg_conninfo: str
) -> None:
    """#6845 review: Bambuddy holds the linked spool 11, not older than its link, but it
    was edited since the last pull, so no row matches exactly: no keep is offered
    and `changed_since_pull` says why."""
    configure(client)
    _linked(client, pg_conninfo)
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("UPDATE filament_bb_links SET bb_updated_at = %s", (SEEN_AT,))
    later = SEEN_AT + timedelta(hours=1)
    _listing(_bb_spool(11, later))

    preview = client.get(RESET)

    assert preview.status_code == 200, preview.text
    assert preview.json()["same_bambuddy"] is False
    assert preview.json()["changed_since_pull"] is True
    assert preview.json()["bambuddy_unreadable"] is False


@respx.mock
def test_the_preview_of_a_row_older_than_its_link_has_no_proof_and_no_reason(
    client: TestClient, pg_conninfo: str
) -> None:
    """Bambuddy holds every linked id, but its row is older than its link (an older copy
    or another Bambuddy): no keep, and not the "changed since the pull" reason."""
    configure(client)
    _linked(client, pg_conninfo)
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("UPDATE filament_bb_links SET bb_updated_at = %s", (SEEN_AT,))
    _listing(_bb_spool(11, SEEN_AT - timedelta(hours=1)))

    preview = client.get(RESET)

    assert preview.status_code == 200, preview.text
    assert preview.json()["same_bambuddy"] is False
    assert preview.json()["changed_since_pull"] is False
    assert preview.json()["bambuddy_unreadable"] is False


@respx.mock
def test_a_kept_reset_whose_proof_changed_is_409(
    client: TestClient, pg_conninfo: str, filament_nudges: list[bool]
) -> None:
    """#6845 R-6845-6 (Decision 198): the preview proved the same Bambuddy, then its
    listing changed before the POST (linked spool 11 is gone). The POST that expects the
    links kept reads it again, answers 409 `proof_changed`, and deletes nothing."""
    configure(client)
    _linked(client, pg_conninfo)
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("UPDATE filament_bb_links SET bb_updated_at = %s", (SEEN_AT,))
    route = _listing(_bb_spool(11, SEEN_AT))
    assert client.get(RESET).json()["same_bambuddy"] is True
    route.mock(return_value=httpx.Response(200, json=[_bb_spool(12, SEEN_AT)]))
    filament_nudges.clear()

    reset = client.post(RESET, json={"confirm_instance": OLD, "expect_kept": True})

    assert reset.status_code == 409, reset.text
    assert reset.json()["code"] == "proof_changed"
    with psycopg.connect(pg_conninfo) as conn:
        left = _left(conn)
        state = conn.execute("SELECT instance FROM filament_bb_sync_state").fetchone()
    assert left == dict.fromkeys(left, 1)
    assert state == (OLD,)
    assert filament_nudges == []


@respx.mock
def test_a_kept_reset_that_cannot_read_bambuddy_is_409(
    client: TestClient, pg_conninfo: str, filament_nudges: list[bool]
) -> None:
    """#6845 R-6845-6: a POST that expects the links kept reads Bambuddy before it
    writes; a failed read is 409 `proof_changed`, and nothing is deleted or nudged."""
    configure(client)
    _linked(client, pg_conninfo)
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(503))
    filament_nudges.clear()

    reset = client.post(RESET, json={"confirm_instance": OLD, "expect_kept": True})

    assert reset.status_code == 409, reset.text
    assert reset.json()["code"] == "proof_changed"
    with psycopg.connect(pg_conninfo) as conn:
        left = _left(conn)
    assert left == dict.fromkeys(left, 1)
    assert filament_nudges == []


@respx.mock
def test_the_preview_of_an_unreadable_bambuddy_offers_the_delete(
    client: TestClient, pg_conninfo: str, filament_nudges: list[bool]
) -> None:
    """#6845 R-6845-6: the preview answers 200 when Bambuddy cannot be read, with
    `same_bambuddy` false and `bambuddy_unreadable` true; the confirmed delete then
    deletes without reading Bambuddy."""
    configure(client)
    _linked(client, pg_conninfo)
    spools = respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(503))

    preview = client.get(RESET)

    assert preview.status_code == 200, preview.text
    assert preview.json()["same_bambuddy"] is False
    assert preview.json()["bambuddy_unreadable"] is True
    assert preview.json()["counts"] == ALL_ONE
    spools.reset()
    filament_nudges.clear()

    reset = client.post(RESET, json={"confirm_instance": OLD, "expect_kept": False})

    assert reset.status_code == 200, reset.text
    assert reset.json() == {
        "instance": OLD,
        "kept": False,
        "configured_instance": None,
        "deleted": ALL_ONE,
    }
    assert not spools.called
    with psycopg.connect(pg_conninfo) as conn:
        left = _left(conn)
    assert left == dict.fromkeys(left, 0)
    assert filament_nudges == [True]


@respx.mock
def test_the_import_of_another_bambuddy_is_409(client: TestClient, pg_conninfo: str) -> None:
    """#6845 R-6845-5: while the links belong to another Bambuddy, the import reads no
    spool and answers 409 `instance_changed`."""
    configure(client)
    _linked(client, pg_conninfo)
    spools = respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[]))

    refused = client.post("/api/v1/filament/bambuddy/import")

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "instance_changed"
    assert not spools.called
