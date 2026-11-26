"""The agent's plugin package mirrors behind its CRUD routes (#6935, area
``package-mirrors``, `api/crud_agent_packages.py`): only the ``agent`` credential reaches
them, and a mirror is read back only by a package whose pin names it. And the strict pending
CHECKs of the packages' migrations (#7191). Area ``plugin-packages`` is Go's (#7667), tested in
``tools/go/core/internal/packages``."""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from scadbuddy.render.agent_migrations import AGENT_MIGRATIONS, migrate_agent
from tests.api.test_crud_agent_cost import token

pytestmark = [pytest.mark.requires_postgres, pytest.mark.agent_migrations]

C1 = "a" * 40
C2 = "b" * 40
H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64


@pytest.fixture
def agent(client: TestClient) -> dict[str, str]:
    return token(client)


@pytest.fixture
def db(pg_conninfo: str) -> Any:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        yield conn


def _review(name: str = "greeter", **extra: Any) -> dict[str, Any]:
    review: dict[str, Any] = {
        "name": name,
        "description": None,
        "version": None,
        "skills": [f"{name}:hello"],
        "commands": [],
        "agents": [],
        "hooks": [{"event": "Stop", "type": "prompt", "id": "0123456789abcdef"}],
        "mcp_servers": [{"name": "mem", "type": "http", "url": "https://mcp.example/mcp/"}],
        "local_servers": [],
        "files": [],
        "refused": [],
        "builtin_tools": [],
        "described": {"skill:hello": None},
    }
    review.update(extra)
    return review


# Mirrors (#6935, spec 2026-11-21 §6.5, §6.6).
MIRRORS = "/api/v1/internal/crud/agent/package-mirrors"
ZIP = b"PK\x05\x06" + b"\0" * 18
ZIP_SHA = hashlib.sha256(ZIP).hexdigest()
OTHER = b"another archive"
OTHER_SHA = hashlib.sha256(OTHER).hexdigest()
OCTETS = {"content-type": "application/octet-stream"}


def _mirror(client: TestClient, agent: dict[str, str], data: bytes, name: str = "greeter") -> str:
    sha = hashlib.sha256(data).hexdigest()
    sent = client.post(f"{MIRRORS}/{name}", content=data, headers={**agent, **OCTETS})
    assert sent.status_code == 200, sent.text
    assert sent.json() == {"bytes_sha256": sha}
    return sha


def _held(db: Any) -> list[str]:
    rows = db.execute(
        "SELECT holder_id FROM blob_refs WHERE holder_kind = 'plugin_package_mirror'"
    ).fetchall()
    return sorted(r[0] for r in rows)


def _bytes_row(db: Any, name: str = "greeter", pending: str | None = None) -> None:
    """An archive package pinned to ``ZIP_SHA``, as the install route stores it; with
    ``pending``, a bytes re-pin to that sha256 under review."""
    db.execute(
        "INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref,"
        " fetch_url, fetch_path, pin_kind, bytes_sha256, content_hash, files, review)"
        " VALUES (%s, 'archive', %s, '', %s, 'greeter-1.0', 'bytes', %s, %s, %s, %s)",
        (
            name,
            f"https://files.test/{name}.zip",
            f"https://files.test/{name}.zip",
            ZIP_SHA,
            H1,
            Jsonb({}),
            Jsonb(_review(name)),
        ),
    )
    if pending is not None:
        db.execute(
            "UPDATE ai_plugin_packages SET pending_ref = '', pending_fetch_url = fetch_url,"
            " pending_fetch_path = fetch_path, pending_pin_kind = 'bytes',"
            " pending_bytes_sha256 = %s, pending_content_hash = %s, pending_files = %s,"
            " pending_review = %s WHERE name = %s",
            (pending, H2, Jsonb({}), Jsonb(_review(name)), name),
        )


def test_a_mirror_is_read_back_only_by_a_package_that_names_it(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    _mirror(client, agent, ZIP)
    assert _held(db) == [f"greeter/{ZIP_SHA}"]
    # Kept, but no package names it yet.
    unnamed = client.get(f"{MIRRORS}/greeter/{ZIP_SHA}", headers=agent)
    assert (unnamed.status_code, unnamed.json()["reason"]) == (404, "mirror-not-found")
    _bytes_row(db)
    got = client.get(f"{MIRRORS}/greeter/{ZIP_SHA}", headers=agent)
    assert (got.status_code, got.content) == (200, ZIP)
    # Another package's name, or bytes the package does not name, are not served.
    assert client.get(f"{MIRRORS}/other/{ZIP_SHA}", headers=agent).status_code == 404
    _mirror(client, agent, OTHER)
    assert client.get(f"{MIRRORS}/greeter/{OTHER_SHA}", headers=agent).status_code == 404
    assert (
        client.post(f"{MIRRORS}/-bad", content=ZIP, headers={**agent, **OCTETS}).status_code == 422
    )
    for caller in ("print", "projects"):
        other = token(client, caller)
        assert client.get(f"{MIRRORS}/greeter/{ZIP_SHA}", headers=other).status_code == 403
        sent = client.post(f"{MIRRORS}/greeter", content=ZIP, headers={**other, **OCTETS})
        assert sent.status_code == 403
    # Gone with the package, though the blob may still be in the store.
    db.execute("DELETE FROM ai_plugin_packages WHERE name = 'greeter'")
    assert client.get(f"{MIRRORS}/greeter/{ZIP_SHA}", headers=agent).status_code == 404


def test_a_pending_repin_reads_its_mirror(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    _mirror(client, agent, ZIP)
    _mirror(client, agent, OTHER)
    _bytes_row(db, pending=OTHER_SHA)
    for sha, data in ((ZIP_SHA, ZIP), (OTHER_SHA, OTHER)):
        got = client.get(f"{MIRRORS}/greeter/{sha}", headers=agent)
        assert (got.status_code, got.content) == (200, data)


# A commit pin's mirror (#7907): a zip of its files, recorded on the row.


def test_a_commit_pins_recorded_mirror_is_read_back(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    # #7907: Go's POST …/plugin-packages/{name}/mirror records a commit pin's zip as
    # mirror_sha256 (and a re-pin's as pending_mirror_sha256); this route serves either.
    _mirror(client, agent, ZIP)
    _mirror(client, agent, OTHER)
    _insert(db, "greeter")
    for sha in (ZIP_SHA, OTHER_SHA):
        assert client.get(f"{MIRRORS}/greeter/{sha}", headers=agent).status_code == 404
    db.execute(
        "UPDATE ai_plugin_packages SET mirror_sha256 = %s WHERE name = 'greeter'", (ZIP_SHA,)
    )
    got = client.get(f"{MIRRORS}/greeter/{ZIP_SHA}", headers=agent)
    assert (got.status_code, got.content) == (200, ZIP)
    _insert(db, "repinned", pin_kind="commit", commit_sha=C2, content_hash=H2)
    _mirror(client, agent, OTHER, name="repinned")
    db.execute(
        "UPDATE ai_plugin_packages SET pending_mirror_sha256 = %s WHERE name = 'repinned'",
        (OTHER_SHA,),
    )
    staged = client.get(f"{MIRRORS}/repinned/{OTHER_SHA}", headers=agent)
    assert (staged.status_code, staged.content) == (200, OTHER)
    # Another package's recorded mirror is not served for this one.
    assert client.get(f"{MIRRORS}/greeter/{OTHER_SHA}", headers=agent).status_code == 404


# #7191: the strict pending CHECKs of 20261123T1756Z_plugin_package_pending_kind_strict.sql,
# and its backfill of the rows that the looser CHECKs of 20261122T0323Z let in.
STRICT = "20261123T1756Z_plugin_package_pending_kind_strict"
Z1 = "c" * 64
Z2 = "d" * 64
PENDING_COLUMNS = (
    "pending_ref",
    "pending_fetch_url",
    "pending_fetch_path",
    "pending_pin_kind",
    "pending_commit_sha",
    "pending_bytes_sha256",
    "pending_version",
    "pending_content_hash",
    "pending_files",
    "pending_review",
)


def _insert(conn: Any, name: str, source_kind: str = "git", **pending: Any) -> None:
    """A commit-pinned row of ``source_kind``. ``pending`` sets re-pin columns (without the
    ``pending_`` prefix); with a ``content_hash`` the re-pin's place, files and review too."""
    columns: dict[str, Any] = {
        "name": name,
        "source_kind": source_kind,
        "source_url": f"https://git.test/{name}.git",
        "source_ref": "HEAD",
        "marketplace_entry": name if source_kind.startswith("marketplace") else None,
        "fetch_url": f"https://git.test/{name}.git",
        "fetch_path": "",
        "pin_kind": "commit",
        "commit_sha": C1,
        "content_hash": H1,
        "files": Jsonb({}),
        "review": Jsonb(_review(name)),
    }
    if "content_hash" in pending:
        columns.update(
            pending_ref="main",
            pending_fetch_url=f"https://git.test/{name}.git",
            pending_fetch_path="",
            pending_files=Jsonb({}),
            pending_review=Jsonb(_review(name)),
        )
    columns.update({f"pending_{k}": v for k, v in pending.items()})
    names = ", ".join(columns)
    marks = ", ".join(["%s"] * len(columns))
    conn.execute(
        f"INSERT INTO ai_plugin_packages ({names}) VALUES ({marks})".encode(),
        tuple(columns.values()),
    )


def _pending_of(conn: Any, name: str) -> dict[str, Any]:
    cols = ", ".join(PENDING_COLUMNS)
    row = conn.execute(
        f"SELECT {cols} FROM ai_plugin_packages WHERE name = %s".encode(), (name,)
    ).fetchone()
    return dict(zip(PENDING_COLUMNS, row, strict=True))


def test_the_backfill_makes_every_loose_row_strict(pg_conninfo: str) -> None:
    before = tuple(m for m in AGENT_MIGRATIONS if m.id != STRICT)
    assert len(before) == len(AGENT_MIGRATIONS) - 1
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate_agent(conn, before)
        # What the looser CHECKs let in: an old pod's commit re-pin with no kind, and a
        # kind that it left behind with no re-pin.
        _insert(conn, "old-repin", commit_sha=C2, content_hash=H2)
        _insert(conn, "stale-kind", pin_kind="bytes")
        # Rows that are strict already.
        _insert(
            conn, "strict", "marketplace-url", pin_kind="bytes", bytes_sha256=Z1, content_hash=H2
        )
        _insert(conn, "plain")
        # Re-pins that neither CHECK lets in: written only with both CHECKs off.
        conn.execute(
            "ALTER TABLE ai_plugin_packages"
            " DROP CONSTRAINT ai_plugin_packages_pending_check,"
            " DROP CONSTRAINT ai_plugin_packages_pending_source_pin_check"
        )
        _insert(conn, "no-kind", content_hash=H2)
        _insert(
            conn, "two-pins", pin_kind="commit", commit_sha=C2, bytes_sha256=Z2, content_hash=H2
        )
        _insert(conn, "wrong-source", pin_kind="bytes", bytes_sha256=Z2, content_hash=H2)
        conn.execute(
            "ALTER TABLE ai_plugin_packages"
            " ADD CONSTRAINT ai_plugin_packages_pending_check CHECK (true),"
            " ADD CONSTRAINT ai_plugin_packages_pending_source_pin_check CHECK (true)"
        )
        assert migrate_agent(conn) == [STRICT]

        old = _pending_of(conn, "old-repin")
        assert (old["pending_pin_kind"], old["pending_commit_sha"]) == ("commit", C2)
        assert old["pending_content_hash"] == H2
        assert set(_pending_of(conn, "stale-kind").values()) == {None}
        strict = _pending_of(conn, "strict")
        assert (strict["pending_pin_kind"], strict["pending_bytes_sha256"]) == ("bytes", Z1)
        assert set(_pending_of(conn, "plain").values()) == {None}
        # A proposal that the strict CHECKs refuse is discarded whole; the pin stays.
        for name in ("no-kind", "two-pins", "wrong-source"):
            assert set(_pending_of(conn, name).values()) == {None}, name
            pin = conn.execute(
                "SELECT pin_kind, commit_sha FROM ai_plugin_packages WHERE name = %s", (name,)
            ).fetchone()
            assert pin == ("commit", C1), name


@pytest.mark.parametrize(
    ("change", "constraint"),
    [
        # A re-pin with no kind: the previous build's commit re-pin.
        ("pending_pin_kind = NULL", "ai_plugin_packages_pending_check"),
        # A kind with no re-pin: the previous build's discard and approve.
        (
            "pending_commit_sha = NULL, pending_content_hash = NULL, pending_files = NULL,"
            " pending_review = NULL, pending_ref = NULL, pending_fetch_url = NULL,"
            " pending_fetch_path = NULL",
            "ai_plugin_packages_pending_check",
        ),
        # A pin column that is not the kind's, or a second one.
        ("pending_pin_kind = 'bytes'", "ai_plugin_packages_pending_check"),
        (f"pending_bytes_sha256 = '{Z1}'", "ai_plugin_packages_pending_check"),
        # A kind that the source cannot have.
        (
            f"pending_pin_kind = 'bytes', pending_commit_sha = NULL, pending_bytes_sha256 = '{Z1}'",
            "ai_plugin_packages_pending_source_pin_check",
        ),
    ],
)
def test_the_strict_checks_refuse_the_loose_forms(
    pg_conninfo: str, change: str, constraint: str
) -> None:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate_agent(conn)
        _insert(conn, "greeter", pin_kind="commit", commit_sha=C2, content_hash=H2)
        with pytest.raises(psycopg.errors.CheckViolation) as refused:
            conn.execute(f"UPDATE ai_plugin_packages SET {change} WHERE name = 'greeter'".encode())
        assert refused.value.diag.constraint_name == constraint
