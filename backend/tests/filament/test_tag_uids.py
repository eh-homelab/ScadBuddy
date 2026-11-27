"""Every UID of a tag (#8148, spec 2026-11-26-bambu-tag-scan-design.md G3): the table
`filament_tag_uids`, its trigger, and the lookups that go through it."""

from __future__ import annotations

from uuid import UUID

import psycopg
import pytest

from scadbuddy.filament import tags
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.tags import TagRead
from scadbuddy.spools.models import SeenTray
from tests.conftest import PgPool
from tests.filament.test_tags import _TRAY, DEFAULTS, _count, _spool

pytestmark = pytest.mark.requires_postgres

# The owner's spool of two tags, read 2026-11-26 (#7905).
SIDE_A, SIDE_B = "9CC69CE300000100", "DCAE1CE100000100"
TRAY = "6DB40AFAF762423DBD73428BDA79DCD0"


def test_a_second_uid_of_a_tag_finds_the_tag_and_its_spool(pg_pool: PgPool) -> None:
    """A Bambu spool's two sides are one tag with two UIDs. A phone read of either side opens
    the spool and makes no row."""
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    store.bind_tag(spool, tag_uid=SIDE_A, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    with pg_pool.connection() as conn:
        tag = conn.execute("SELECT id FROM filament_tags").fetchone()
        assert tag is not None
        conn.execute(
            "INSERT INTO filament_tag_uids (uid, tag_id, source) VALUES (%s, %s, 'app')",
            (SIDE_B, tag["id"]),
        )
    result = tags.read(
        store, TagRead(source="phone", uid="DCAE1CE1"), actor="ann", defaults=DEFAULTS
    )
    assert result.tag is not None and result.tag.id == tag["id"]
    assert result.tag.tag_uid == SIDE_A
    assert result.spool is not None and result.spool.id == spool
    assert _count(pg_pool, "filament_tags") == 1
    with store.transaction() as conn:
        assert tags.holder_by_keys(conn, None, SIDE_B) == spool
        seen = SeenTray(ams_id=0, tray_id=0, tray_uuid=None, tag_uid=SIDE_B)
        assert FilamentInventory.holder_on(conn, UUID(int=1), seen) == (spool, "tag")


def test_a_uid_found_by_its_tray_is_added_to_the_tag(pg_pool: PgPool) -> None:
    """A read of a spool's second side with its tray UUID names the tray's tag: the side's
    UID becomes one more UID of it, and the tag keeps its first `tag_uid`."""
    store = FilamentSpoolStore(pg_pool)
    store.bind_tag(
        _spool(store, pg_pool), tag_uid=SIDE_A, tray_uuid=_TRAY, kind="bambu_mifare", actor="ann"
    )
    result = tags.read(
        store,
        TagRead(source="agent", uid="DCAE1CE1", tray_uuid=_TRAY),
        actor="ann",
        defaults=DEFAULTS,
    )
    assert result.tag is not None and result.tag.tag_uid == SIDE_A
    with pg_pool.connection() as conn:
        uids = conn.execute(
            "SELECT uid FROM filament_tag_uids WHERE tag_id = %s ORDER BY uid", (result.tag.id,)
        ).fetchall()
    assert [r["uid"] for r in uids] == [SIDE_A, SIDE_B]


def test_a_tag_uid_another_tag_holds_is_refused(pg_pool: PgPool) -> None:
    """The trigger keeps each tag's first UID in `filament_tag_uids`: a `tag_uid` that names
    another tag's second UID is a unique violation, as the unique index on `tag_uid` was."""
    with pg_pool.connection() as conn:
        tag = conn.execute(
            "INSERT INTO filament_tags (kind, tag_uid) VALUES ('other', 'AA') RETURNING id"
        ).fetchone()
        assert tag is not None
        conn.execute(
            "INSERT INTO filament_tag_uids (uid, tag_id, source) VALUES ('BB', %s, 'app')",
            (tag["id"],),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO filament_tags (kind, tag_uid) VALUES ('other', 'BB')")


def test_a_history_row_moves_with_a_merge_and_is_never_changed(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        a = conn.execute(
            "INSERT INTO filament_tags (kind, tag_uid) VALUES ('other', 'AA') RETURNING id"
        ).fetchone()
        b = conn.execute(
            "INSERT INTO filament_tags (kind, tag_uid) VALUES ('other', 'BB') RETURNING id"
        ).fetchone()
        assert a is not None and b is not None
        row = conn.execute(
            "INSERT INTO filament_tag_reads (tag_id, source, uid) VALUES (%s, 'phone', 'AA')"
            " RETURNING id",
            (a["id"],),
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE filament_tag_reads SET tag_id = %s WHERE id = %s", (b["id"], row["id"])
        )
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE filament_tag_reads SET uid = 'CC' WHERE id = %s", (row["id"],))
    with pg_pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM filament_tag_reads")
