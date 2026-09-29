"""The backend's schema migrations, in the database ``SCADBUDDY_DATABASE_URL`` names.

Schema changes are files in ``backend/scadbuddy/migrations/`` (`MIGRATIONS_DIR`), one
per migration, named by UTC timestamp and slug (``20260928T0612Z_settings.sql``). Add a
new file; never edit, rename or remove a merged one. `migrate` applies them in
timestamp order and each once by file id, under an advisory lock so two starting pods
cannot race each other. They include the tables of every store on the database: the
``render_jobs`` projection (`render/projection.py`), the settings, the presets and the
default-render previews' ``model_previews`` (``20260928T0721Z_model_previews.sql``).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from psycopg import Connection, sql
from psycopg.rows import tuple_row

from scadbuddy.core.events import Event

logger = logging.getLogger(__name__)

#: `pg_advisory_xact_lock` key for applying migrations ("SCADBDDY" in ASCII).
MIGRATION_LOCK = 0x5343_4144_4244_4459

#: The migration files (#491): one per migration, named ``<yyyymmdd>T<hhmm>Z_<slug>.sql``
#: (UTC). The stem is the file's id in ``scadbuddy_migrations``.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

#: ``<yyyymmdd>T<hhmm>Z_<slug>``; fixed width, so sorting ids sorts by time.
MIGRATION_ID = re.compile(r"\d{8}T\d{4}Z_[a-z0-9_]+")


class Migration(NamedTuple):
    id: str
    sql: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """The migration files of `directory`, in timestamp order. Anything there that is
    not a well-named ``.sql`` file raises, so a misnamed migration fails at start
    instead of being skipped."""
    migrations: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql" or not MIGRATION_ID.fullmatch(path.stem):
            raise ValueError(
                f"{path} is not a migration: files there are named"
                " <yyyymmdd>T<hhmm>Z_<slug>.sql (UTC, slug in [a-z0-9_])"
            )
        migrations.append(Migration(path.stem, path.read_text(encoding="utf-8")))
    return tuple(migrations)


#: Read at import, so an image without the files fails at start.
MIGRATIONS: tuple[Migration, ...] = load_migrations()

#: Before #491 the ledger recorded migrations by POSITION in a list: entry ``n`` of
#: that list is ``LEGACY_VERSIONS[n - 1]``. `migrate` rewrites positional rows to
#: these ids once, and keeps writing ``version`` for them so an image from before
#: #491 still starts on the ledger. Frozen: nothing is ever added here.
LEGACY_VERSIONS: tuple[str, ...] = (
    "20260927T2243Z_render_jobs",
    "20260928T0105Z_render_diagnostics",
    "20260928T0600Z_render_warnings",
    "20260928T0630Z_events",
)


class MigrationLedgerError(RuntimeError):
    """The ledger holds a positional row (from before #491) that main's list never
    had: this database ran a migration from a branch that did not merge as such.
    Guessing which file it was would either re-run or skip real schema."""


class TransactionalEvents(Protocol):
    """Publishes an event inside a caller's transaction (`PgNotifyEventBus`)."""

    def publish_in(self, conn: Connection[Any], event: Event) -> None: ...


def migrate(conn: Connection[Any], migrations: tuple[Migration, ...] = MIGRATIONS) -> list[str]:
    """Apply the migration files this database has not seen, in timestamp order;
    returns their ids.

    Any file not in the ledger is applied, so one with an OLDER timestamp that
    merges after newer ones ran (a branch cut earlier) still runs. A ledger row
    naming a file this build does not have (a newer image ran it) is left alone.
    """
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        _ensure_ledger(conn)
        ids = conn.cursor(row_factory=tuple_row).execute("SELECT id FROM scadbuddy_migrations")
        done = {row[0] for row in ids}
        for migration in sorted(migrations, key=lambda m: m.id):
            if migration.id in done:
                continue
            conn.execute(migration.sql.encode("utf-8"))
            version = (
                LEGACY_VERSIONS.index(migration.id) + 1 if migration.id in LEGACY_VERSIONS else None
            )
            conn.execute(
                "INSERT INTO scadbuddy_migrations (id, version) VALUES (%s, %s)",
                (migration.id, version),
            )
            applied.append(migration.id)
    return applied


def _ensure_ledger(conn: Connection[Any]) -> None:
    """Create the ledger, or convert one from before #491 (keyed by position) to file
    ids, inside `migrate`'s transaction and advisory lock: exactly once, however many
    pods start together."""
    columns = {
        row[0]
        for row in conn.cursor(row_factory=tuple_row).execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'scadbuddy_migrations'"
        )
    }
    if not columns:
        # `version` is kept for the LEGACY_VERSIONS files only.
        conn.execute(
            "CREATE TABLE scadbuddy_migrations ("
            " id text PRIMARY KEY,"
            " version integer UNIQUE,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        return
    if "id" in columns:
        return
    versions = [
        row[0]
        for row in conn.cursor(row_factory=tuple_row).execute(
            "SELECT version FROM scadbuddy_migrations ORDER BY version"
        )
    ]
    unknown = [v for v in versions if not 1 <= v <= len(LEGACY_VERSIONS)]
    if unknown:
        raise MigrationLedgerError(
            f"scadbuddy_migrations records positional version(s)"
            f" {', '.join(map(str, unknown))}, but main only ever had {len(LEGACY_VERSIONS)}:"
            " this database ran a migration from a branch that has not merged as such."
            " Recreate the database, or delete those rows and the schema they created,"
            " then start again."
        )
    pkey = (
        conn.cursor(row_factory=tuple_row)
        .execute(
            "SELECT conname FROM pg_constraint"
            " WHERE conrelid = 'scadbuddy_migrations'::regclass AND contype = 'p'"
        )
        .fetchone()
    )
    conn.execute("ALTER TABLE scadbuddy_migrations ADD COLUMN id text")
    conn.execute(
        "UPDATE scadbuddy_migrations SET id = (%s::text[])[version]", (list(LEGACY_VERSIONS),)
    )
    if pkey is not None:
        conn.execute(
            sql.SQL("ALTER TABLE scadbuddy_migrations DROP CONSTRAINT {}").format(
                sql.Identifier(pkey[0])
            )
        )
    conn.execute(
        "ALTER TABLE scadbuddy_migrations"
        " ALTER COLUMN id SET NOT NULL,"
        " ADD PRIMARY KEY (id),"
        " ADD UNIQUE (version),"
        " ALTER COLUMN version DROP NOT NULL"
    )
