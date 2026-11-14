"""The agent's schema migrations, which the API applies at start (#5299 D6).

The agent's `ai_*` tables were created by the agent itself until #5299: only the CRUD
service may hold SQL access (#5295). Its runner is now the agent's tests' only
(`agent/test/support/migrations.ts`). Its files moved here byte
for byte, to ``backend/scadbuddy/migrations_agent/`` (`AGENT_MIGRATIONS_DIR`), and this
module is a port of that runner, so a database it migrated reads the same to both:

- the same ledger, ``ai_migrations`` (``id``, ``version``, ``story``, ``checksum``,
  ``applied_at``), apart from the backend's ``scadbuddy_migrations``;
- the same advisory lock, `AGENT_MIGRATION_LOCK` ("SCADAGNT"), so an agent pod from
  before #5299 and this API never migrate at once during a rolling deploy;
- each file's SHA-256 of its exact bytes recorded, and an applied file whose bytes changed
  refused (`AgentMigrationChecksumError`); a row from before the checksum column (#354) is
  adopted with the file's checksum as it is now;
- the positional ledger from before #491 rewritten to file ids once
  (`AGENT_LEGACY_VERSIONS`), or refused when it holds a row main never had
  (`AgentMigrationLedgerError`);
- the same ``lock_timeout`` and ``statement_timeout``, ``SET LOCAL`` in the one
  transaction every pending file runs in, each file through the simple query protocol
  (several statements per file).

RULES FOR ADDING ONE (#491):

- Add a NEW file to ``migrations_agent/``, named ``<UTC timestamp>_<slug>.sql``
  (``date -u +%Y%m%dT%H%MZ``), and its id and SHA-256 to the agent's
  ``agent/src/db/expected-migrations.json`` (``UPDATE_EXPECTED_MIGRATIONS=1 pnpm exec
  vitest run test/schema.test.ts`` writes it), so the agent waits for it.
- Never edit, rename or remove a merged file (even whitespace counts; ``.editorconfig``
  leaves this directory's whitespace alone).
- One story per file, named in a leading comment. Prefix every table ``ai_``.
- Plain SQL, no parameters.
- Secrets are never stored in the clear: use the envelope columns of the credentials
  migration (``*_sealed bytea`` + ``dek_sealed bytea`` + ``kek_id text``, sealed by
  ``agent/src/secrets.ts``). Tokens that are only ever compared are stored hashed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from psycopg import Connection, sql
from psycopg.rows import tuple_row

from scadbuddy.render.pg_store import MIGRATION_ID, Migration

#: `pg_advisory_xact_lock` key ("SCADAGNT" in ASCII): the agent's own runner's, distinct
#: from the backend's "SCADBDDY".
AGENT_MIGRATION_LOCK = 0x5343_4144_4147_4E54

#: The agent's migration files, one per migration, named ``<yyyymmdd>T<hhmm>Z_<slug>.sql``.
AGENT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations_agent"

#: Postgres ``lock_timeout`` while migrating: bounds the wait for the advisory lock (an
#: old agent pod migrating) and for table locks. A timeout fails the API's start.
DEFAULT_LOCK_TIMEOUT_MS = 10_000
#: Postgres ``statement_timeout`` for each statement of the migration.
DEFAULT_STATEMENT_TIMEOUT_MS = 60_000

#: Before #491 the ledger recorded migrations by POSITION: entry ``n`` of that list is
#: ``AGENT_LEGACY_VERSIONS[n - 1]`` (its id and story). Their rows keep ``version`` and
#: ``story`` so an image from before #491 still reads the ledger. Frozen.
AGENT_LEGACY_VERSIONS: tuple[tuple[str, str], ...] = (
    ("20260927T2349Z_credentials_settings", "#255"),
    ("20260928T0107Z_sessions", "#300"),
)


class AgentMigrationChecksumError(RuntimeError):
    """An applied file's SQL no longer matches what was recorded: someone edited a merged
    migration. Permanent: the start fails rather than guessing."""


class AgentMigrationLedgerError(RuntimeError):
    """The ledger holds a positional row (from before #491) that main's list never had:
    this database ran a migration from a branch that did not merge as such."""


def load_agent_migrations(directory: Path = AGENT_MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """The migration files of `directory`, in timestamp order, each read as its exact
    bytes (no newline translation), so the checksum is the file's. Anything there that is
    not a well-named ``.sql`` file raises."""
    migrations: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql" or not MIGRATION_ID.fullmatch(path.stem):
            raise ValueError(
                f"{path} is not a migration: files there are named"
                " <yyyymmdd>T<hhmm>Z_<slug>.sql (UTC, slug in [a-z0-9_])"
            )
        migrations.append(Migration(path.stem, path.read_bytes().decode("utf-8")))
    return tuple(migrations)


#: Read at import, so an image without the files fails at start.
AGENT_MIGRATIONS: tuple[Migration, ...] = load_agent_migrations()


def agent_migration_checksum(migration: Migration) -> str:
    """SHA-256 (hex) of a migration's SQL as UTF-8; recorded in ``ai_migrations.checksum``."""
    return hashlib.sha256(migration.sql.encode("utf-8")).hexdigest()


def migrate_agent(
    conn: Connection[Any],
    migrations: tuple[Migration, ...] = AGENT_MIGRATIONS,
    *,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    lock_key: int = AGENT_MIGRATION_LOCK,
) -> list[str]:
    """Apply the agent's migration files this database has not seen, in timestamp order,
    in one transaction under `lock_key`; returns their ids.

    Any file not in the ledger is applied, so one with an OLDER timestamp that merges
    after newer ones ran still runs. A ledger row naming a file this build does not have
    is left alone. Only a test passes its own `lock_key`.
    """
    lock_timeout = max(1, int(lock_timeout_ms))
    statement_timeout = max(1, int(statement_timeout_ms))
    applied: list[str] = []
    with conn.transaction():
        # Integers built here, not input; LOCAL, so the pooled connection goes back
        # without them.
        conn.execute(f"SET LOCAL lock_timeout = {lock_timeout}".encode())
        conn.execute(f"SET LOCAL statement_timeout = {statement_timeout}".encode())
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        _ensure_ledger(conn)
        cur = conn.cursor(row_factory=tuple_row)
        recorded: dict[str, str | None] = {
            row[0]: row[1] for row in cur.execute("SELECT id, checksum FROM ai_migrations")
        }
        legacy_ids = [legacy for legacy, _ in AGENT_LEGACY_VERSIONS]
        for migration in sorted(migrations, key=lambda m: m.id):
            checksum = agent_migration_checksum(migration)
            if migration.id in recorded:
                was = recorded[migration.id]
                if was is None:
                    conn.execute(
                        "UPDATE ai_migrations SET checksum = %s WHERE id = %s",
                        (checksum, migration.id),
                    )
                elif was != checksum:
                    raise AgentMigrationChecksumError(
                        f"ai migration {migration.id} was applied with different SQL"
                        f" (recorded sha256 {was[:12]}…, now {checksum[:12]}…). Merged files"
                        " in backend/scadbuddy/migrations_agent/ must never be edited:"
                        " restore it and add a new file instead."
                    )
                continue
            conn.execute(migration.sql.encode("utf-8"))
            if migration.id in legacy_ids:
                position = legacy_ids.index(migration.id)
                version: int | None = position + 1
                story: str | None = AGENT_LEGACY_VERSIONS[position][1]
            else:
                version, story = None, None
            conn.execute(
                "INSERT INTO ai_migrations (id, version, story, checksum) VALUES (%s, %s, %s, %s)",
                (migration.id, version, story, checksum),
            )
            applied.append(migration.id)
    return applied


def _ensure_ledger(conn: Connection[Any]) -> None:
    """Create the ledger, or convert one from before #491 (keyed by position) to file
    ids, in `migrate_agent`'s transaction and under its lock: exactly once."""
    cur = conn.cursor(row_factory=tuple_row)
    columns = {
        row[0]
        for row in cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'ai_migrations'"
        )
    }
    if not columns:
        # `version` and `story` are kept for the AGENT_LEGACY_VERSIONS files only.
        conn.execute(
            "CREATE TABLE ai_migrations ("
            " id text PRIMARY KEY,"
            " version integer UNIQUE,"
            " story text,"
            " checksum text,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        return
    if "id" in columns:
        return
    versions = [row[0] for row in cur.execute("SELECT version FROM ai_migrations ORDER BY version")]
    unknown = [v for v in versions if not 1 <= v <= len(AGENT_LEGACY_VERSIONS)]
    if unknown:
        raise AgentMigrationLedgerError(
            f"ai_migrations records positional version(s) {', '.join(map(str, unknown))},"
            f" but main only ever had {len(AGENT_LEGACY_VERSIONS)}: this database ran a"
            " migration from a branch that has not merged as such. Recreate the database,"
            " or delete those rows and the schema they created, then start again."
        )
    pkey = cur.execute(
        "SELECT conname FROM pg_constraint"
        " WHERE conrelid = 'ai_migrations'::regclass AND contype = 'p'"
    ).fetchone()
    conn.execute("ALTER TABLE ai_migrations ADD COLUMN IF NOT EXISTS checksum text")
    conn.execute("ALTER TABLE ai_migrations ADD COLUMN id text")
    conn.execute(
        "UPDATE ai_migrations SET id = (%s::text[])[version]",
        ([legacy for legacy, _ in AGENT_LEGACY_VERSIONS],),
    )
    if pkey is not None:
        conn.execute(
            sql.SQL("ALTER TABLE ai_migrations DROP CONSTRAINT {}").format(sql.Identifier(pkey[0]))
        )
    conn.execute(
        "ALTER TABLE ai_migrations"
        " ALTER COLUMN id SET NOT NULL,"
        " ADD PRIMARY KEY (id),"
        " ADD UNIQUE (version),"
        " ALTER COLUMN version DROP NOT NULL,"
        " ALTER COLUMN story DROP NOT NULL"
    )
