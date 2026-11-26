"""Put the second models repository in the place of the first (#7314 step 5; spec
``docs/superpowers/specs/2026-11-24-models-repository-lfs-design.md``, "Switch"; runbook
``docs/models-switch-runbook.md``).

``python -m scadbuddy.library.models_switch preflight`` reads only: it says what ``run``
would change, and every reason to refuse. ``… run --dump <dir>`` does the switch, and
``… rollback --dump <dir>`` undoes it. They run in the API's image with its own database
credential, while no API runs.

``run`` does these steps, in this order:

1. The preflight again, then a dump of every row and ``meta.json`` that the next steps
   change.
2. ``models`` to ``models-old``, then ``models-next`` (`models_next` made it) to ``models``.
3. The new ``model_version`` in each mapped output's ``meta.json``.
4. One transaction: every row that holds an old commit id (`COLUMNS`), the recorded HEAD,
   no row in ``templates`` and ``template_copy_runs`` (the start copy makes them again), and
   no ``models`` row in ``git_remote_pushes``. It compares the rows with the dump first. Last
   it looks for an old id and for a pin that does not resolve, and then it commits.

A step that was done is seen and not done again. So a second ``run`` with the same dump
finishes one that stopped. Nothing here removes a repository, an output or its file.

``rollback`` renames back, puts the dumped ``meta.json`` files and columns back, and sets
the recorded HEAD. It refuses when one of them holds something that neither the dump nor
``run`` put there.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, NamedTuple

import psycopg
from psycopg import sql
from pydantic import ValidationError

from scadbuddy.core.gitrepo import GitError
from scadbuddy.core.paths import DataPaths, model_path
from scadbuddy.library.models_next import (
    _COMMIT,
    MAP_NAME,
    PLACES,
    RefusedError,
    _copy_digest,
    _held,
    _is_derived,
    _may_replace,
    _Objects,
    _Repo,
    _templates,
    derived_map,
)
from scadbuddy.library.outputs import META_NAME, OutputMeta
from scadbuddy.library.repo_lock import LOCK_KEY as MODELS_LOCK
from scadbuddy.render.inputs import inputs_key
from scadbuddy.render.job_models import render_key

#: The names beside ``models``. `models_next` builds in the first.
NEXT_NAME = "models-next"
OLD_NAME = "models-old"
APPLICATION = "scadbuddy-models-switch"
#: One switch at a time.
SWITCH_LOCK = "scadbuddy:models-switch"

#: The columns that are changed, by table, with the key of its rows.
COLUMNS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "order_items": ("id", ("commit", "render_key")),
    "order_item_revisions": ("id", ("commit", "render_key")),
    # Not `updated_at`: it is in `variant_key`, and another would drop every price.
    "storefront_listings": ("id", ("commit",)),
    "outputs": ("output_id", ("meta",)),
    "analyzer_decisions": ("id", ("scope_key", "body")),
}
#: The columns that are read and written as Postgres's own text, so that a number keeps
#: its digits.
JSONB = frozenset({"meta", "body"})
#: The tables that are emptied. The API's start copy makes their rows again.
EMPTIED = ("templates", "template_copy_runs")
HEAD_TABLE = "models_repo_head"
PUSH_TABLE = "git_remote_pushes"
#: The ``git_remote_pushes`` row of the models repository.
PUSHED = "models"

#: What may not be running, as it would go on with an old id: a table, its key and a state.
RUNNING: Final = (
    ("render_jobs", "id", "state IN ('pending', 'running')"),
    ("operations", "id", "status = 'running'"),
    ("storefront_slice_prices", "price_key", "status = 'running'"),
    ("outputs", "output_id", "backfill IS NOT NULL AND backfill->>'error' IS NULL"),
)
#: The map as a table, from the three arrays of `_mapped`.
MAP: Final = "unnest(%s::text[], %s::text[], %s::text[]) AS m(slug, old, new)"

#: The dump's files.
MANIFEST = "manifest.json"
ROWS = "rows.jsonl"
FILES = "files.jsonl"
TABLES = "tables.jsonl"
MAP_COPY = "map.json"
FORMAT = 1

#: How many of a list a message names.
NAMED = 10

Conn = psycopg.Connection[tuple[Any, ...]]
Ids = dict[tuple[str, str], str]
Values = dict[str, str | None]


class SwitchRefusedError(RuntimeError):
    """Why it did not go on. Each reason is one to settle before it is tried again."""

    def __init__(self, *reasons: str) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


class SwitchFailedError(RuntimeError):
    """A step found what it did not expect, and stopped."""


@dataclass(frozen=True)
class Row:
    """A row to change, with the columns' values before and after."""

    table: str
    id: str
    old: Values
    new: Values

    def __str__(self) -> str:
        return f"{self.table} {self.id}"


@dataclass(frozen=True)
class File:
    """A ``meta.json`` to change, by its path in the data directory."""

    path: str
    old_sha256: str
    new_sha256: str
    mtime_ns: int

    def __str__(self) -> str:
        return self.path


class Where(NamedTuple):
    """Where the two repositories are."""

    #: How many of the two renames are done.
    renames: int
    old: Path
    new: Path


@dataclass
class Found:
    """What a preflight found."""

    refused: list[str] = field(default_factory=list)
    #: What a switch leaves, and a person should know of.
    reports: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    where: Where | None = None
    old_head: str = ""
    new_head: str = ""
    ids: Ids = field(default_factory=dict)
    #: The templates at the old HEAD.
    slugs: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    files: list[File] = field(default_factory=list)


@dataclass(frozen=True)
class Dump:
    directory: Path
    old_head: str
    new_head: str
    ids: Ids
    slugs: list[str]
    rows: list[Row]
    files: list[File]
    #: The rows of `EMPTIED`, the recorded HEAD and the push, as JSON text.
    tables: dict[str, list[str]]

    def kept(self, file: File) -> Path:
        """The copy of ``file``."""
        return self.directory / "files" / file.path


def _say(line: str) -> None:
    print(line, flush=True)


def _named(names: Sequence[object]) -> str:
    shown = ", ".join(str(name) for name in names[:NAMED])
    return f"{shown} and {len(names) - NAMED} more" if len(names) > NAMED else shown


# ── files ─────────────────────────────────────────────────────────────────────


def _sync(directory: Path) -> None:
    handle = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _write(path: Path, data: bytes) -> None:
    """A new file, on the disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _put(path: Path, data: bytes, mtime_ns: int | None = None) -> None:
    """``data`` at ``path``, whole or not at all (as `outputs._replace`), on the disk."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        _write(temporary, data)
        if mtime_ns is not None:
            os.utime(temporary, ns=(mtime_ns, mtime_ns))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    _sync(path.parent)


def _rename(source: Path, target: Path) -> None:
    os.rename(source, target)
    _sync(target.parent)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _with_version(data: bytes, version: str) -> bytes:
    """A ``meta.json`` with another ``model_version``, in the form `OutputStore.rename`
    writes. A key that this build does not know stays."""
    body = {**json.loads(data), "model_version": version}
    return (json.dumps(body, indent=2) + "\n").encode()


# ── the two repositories ──────────────────────────────────────────────────────


def _beside(paths: DataPaths, name: str) -> Path:
    return paths.models.with_name(name)


def _where(paths: DataPaths) -> Where:
    live, following, former = paths.models, _beside(paths, NEXT_NAME), _beside(paths, OLD_NAME)
    for place in (live, following, former):
        if place.is_symlink():
            raise SwitchRefusedError(f"{place} is a link")
    there = (live.is_dir(), following.is_dir(), os.path.lexists(former))
    if there == (True, True, False):
        return Where(0, live, following)
    if there == (False, True, True):
        return Where(1, former, following)
    if there == (True, False, True):
        return Where(2, former, live)
    raise SwitchRefusedError(_standing(paths))


def _standing(paths: DataPaths) -> str:
    """Which of the three directories are there."""
    names = (paths.models.name, NEXT_NAME, OLD_NAME)
    found = [name for name in names if os.path.lexists(_beside(paths, name))]
    missing = [name for name in names if name not in found]
    return (
        f"in {paths.root} there is {', '.join(found) or 'nothing'},"
        f" and no {', '.join(missing) or 'other'}"
    )


def _changed(repo: _Repo) -> list[str]:
    """Each path that is not as HEAD has it."""
    args = ("status", "--porcelain", "-z", "--untracked-files=all", "--no-renames")
    return [line[3:] for line in os.fsdecode(repo.bytes_out(*args)).split("\0") if line]


def _data_files(repo: _Repo) -> list[str]:
    """The ignored files that are data, as the builder copies them."""
    args = ("ls-files", "-z", "--others", "--ignored", "--exclude-standard")
    listed = os.fsdecode(repo.bytes_out(*args)).split("\0")
    return [name for name in listed if name and not _is_derived(name)]


def _check_repositories(found: Found) -> None:
    """That the new one is what the builder made of the old one as it is now."""
    assert found.where is not None
    old, new = _Repo(found.where.old), _Repo(found.where.new)
    heads = []
    for repo in (old, new):
        head = repo.head() if repo.available else None
        if head is None:
            raise SwitchRefusedError(f"{repo.root} is not a repository with a commit")
        heads.append(head)
    found.old_head, found.new_head = heads
    try:
        _may_replace(new.root)
    except RefusedError as error:
        found.refused.append(str(error))
    held = _held(new)
    if held.get("old_head") != found.old_head:
        found.refused.append(
            f"{new.root} was made from {held.get('old_head')}, and the HEAD of {old.root} is"
            f" {found.old_head}: build it again"
        )
    if changed := _changed(old):
        found.refused.append(f"{old.root} has changes that are not committed: {_named(changed)}")
    _check_data_files(found, old, new, held)
    found.ids = derived_map(new.root)
    entries = held.get("map")
    listed = {
        (str(entry.get("slug")), str(entry.get("old"))): str(entry.get("new"))
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict)
    }
    if listed != found.ids:
        found.refused.append(f"{MAP_NAME} in {new.root} is not what its commits' trailers say")
    with _Objects(old) as before, _Objects(new) as after:
        found.slugs = sorted(_templates(before, found.old_head))
        there = _templates(after, found.new_head)
    if missing := [slug for slug in found.slugs if slug not in there]:
        found.refused.append(f"{new.root} has no {_named(missing)}")


def _check_data_files(found: Found, old: _Repo, new: _Repo, held: Mapping[str, Any]) -> None:
    """A video moves no HEAD: one added since the build shows here only."""
    copied = held.get("copied", {})
    names = _data_files(old)
    for name in names:
        digest, copy = _copy_digest(old.root / name), new.root / name
        there = os.path.lexists(copy) and _copy_digest(copy) == digest
        if copied.get(name) != digest or not there:
            found.refused.append(f"{name} in {new.root} is not as it is in {old.root}")
    if gone := [name for name in copied if name not in names]:
        found.refused.append(f"{_named(gone)} in {new.root} is no longer in {old.root}")


def _check_new(where: Where, dump: Dump) -> None:
    """After the renames: the repository in place is the new one, whole."""
    repo = _Repo(where.new)
    head = repo.head() if repo.available else None
    if head != dump.new_head:
        raise SwitchFailedError(f"the HEAD of {where.new} is {head}, and not {dump.new_head}")
    if missing := [slug for slug in dump.slugs if not (where.new / model_path(slug)).is_dir()]:
        raise SwitchFailedError(f"{where.new} has no {_named(missing)}")


# ── the rows ──────────────────────────────────────────────────────────────────


def _mapped(ids: Ids) -> tuple[list[str], list[str], list[str]]:
    keys = sorted(ids)
    return [slug for slug, _ in keys], [old for _, old in keys], [ids[key] for key in keys]


def _plan_rows(conn: Conn, ids: Ids) -> tuple[list[Row], list[str]]:
    """Each row with an old id, and what there is to report of them."""
    rows: list[Row] = []
    reports: list[str] = []
    arrays = _mapped(ids)
    for table in ("order_items", "order_item_revisions"):
        query = sql.SQL(
            "SELECT t.id, t.slug, t.commit, m.new, t.inputs, t.render_key FROM {} t JOIN "
            + MAP
            + " ON m.slug = t.slug AND m.old = t.commit ORDER BY t.id"
        ).format(sql.Identifier(table))
        for key, slug, old, new, inputs, held in conn.execute(query, arrays):
            moved = _moved_key(slug, inputs, old, new, held)
            if moved is None:
                reports.append(
                    f"{table} {key}: its render_key is neither a pipeline's nor a plain one at"
                    f" {old}; it stays, and the commit is changed"
                )
            before: Values = {"commit": old, "render_key": held}
            rows.append(Row(table, key, before, {"commit": new, "render_key": moved or held}))
    listings = conn.execute(
        "SELECT t.id, t.commit, m.new FROM storefront_listings t JOIN "
        + MAP
        + " ON m.slug = t.slug AND m.old = t.commit ORDER BY t.id",
        arrays,
    )
    for key, old, new in listings:
        rows.append(Row("storefront_listings", key, {"commit": old}, {"commit": new}))
    outputs = conn.execute(
        "SELECT t.output_id, t.meta::text,"
        " jsonb_set(t.meta, '{model_version}', to_jsonb(m.new))::text FROM outputs t JOIN "
        + MAP
        + " ON m.slug = t.slug AND m.old = t.meta->>'model_version' ORDER BY t.output_id",
        arrays,
    )
    for key, old, new in outputs:
        rows.append(Row("outputs", key, {"meta": old}, {"meta": new}))
    decisions = conn.execute(
        "SELECT t.id, t.scope_key, m.slug || '@' || m.new, t.body::text,"
        " jsonb_set(t.body, '{scope,key}', to_jsonb(m.slug || '@' || m.new))::text"
        " FROM analyzer_decisions t JOIN " + MAP + " ON t.scope_key = m.slug || '@' || m.old"
        " WHERE t.scope_kind = 'template_version' ORDER BY t.id",
        arrays,
    )
    for key, old, new, was, body in decisions:
        before = {"scope_key": old, "body": was}
        rows.append(Row("analyzer_decisions", key, before, {"scope_key": new, "body": body}))
    return rows, reports


def _moved_key(slug: str, inputs: Mapping[str, Any], old: str, new: str, held: str) -> str | None:
    """``held`` at the new id, by the rule of `orders.reference.render_key_of`; None where
    it is neither key."""
    if held == inputs_key(slug, inputs, old):
        return inputs_key(slug, inputs, new)
    params = inputs.get("params", {})
    plain = params if isinstance(params, dict) else {}
    if held == render_key(slug, plain, old):
        return render_key(slug, plain, new)
    return None


def _pins(conn: Conn) -> list[tuple[str, str, str, str]]:
    """Each (table, row, slug, commit) that pins a version."""
    each = sql.SQL(" UNION ALL ").join(
        sql.SQL("SELECT {}, id, slug, commit FROM {} WHERE commit IS NOT NULL").format(
            sql.Literal(place), sql.Identifier(place)
        )
        for place in PLACES
    )
    return conn.execute(each + sql.SQL(" ORDER BY 1, 2")).fetchall()


def _outputs(conn: Conn) -> list[tuple[str, str, str | None]]:
    """Each output's id, template and version."""
    return conn.execute(
        "SELECT output_id, slug, meta->>'model_version' FROM outputs ORDER BY output_id"
    ).fetchall()


def _check_pins(conn: Conn, found: Found) -> None:
    """That each id has a new one, or is in the new repository already."""
    assert found.where is not None
    old, new = _Repo(found.where.old), _Repo(found.where.new)
    with _Objects(old) as before, _Objects(new) as after:

        def moves(slug: str, commit: str) -> bool:
            return (slug, commit) in found.ids or after.tree(commit, slug) is not None

        for table, key, slug, commit in _pins(conn):
            if not moves(slug, commit):
                found.refused.append(f"{table} {key}: {slug} at {commit} has no new id")
        for key, slug, version in _outputs(conn):
            if version is None or not _COMMIT.fullmatch(version) or moves(slug, version):
                continue
            if before.tree(version, slug) is not None:
                found.refused.append(
                    f"outputs {key}: {slug} at {version} is in {old.root}, and has no new id:"
                    " build it again"
                )
            else:
                found.reports.append(
                    f"outputs {key}: {slug} at {version} is in no repository; left"
                )


def _check_outputs(conn: Conn, found: Found, files: Mapping[str, tuple[str, str | None]]) -> None:
    """That each row says what its file says. ``files`` is what `_plan_files` read."""

    def moved(slug: str, version: str | None) -> tuple[str, str | None]:
        return slug, found.ids.get((slug, version or ""), version)

    rows = _outputs(conn)
    for key, slug, version in rows:
        there = files.get(key)
        if there is None:
            if (slug, version or "") in found.ids:
                found.reports.append(
                    f"outputs {key}: no {META_NAME} that can be read; the row is changed"
                )
        elif moved(*there) != moved(slug, version):
            found.refused.append(
                f"outputs {key}: its row says {slug} at {version}, and its {META_NAME} says"
                f" {there[0]} at {there[1]}"
            )
    known = {key for key, _, _ in rows}
    for file in found.files:
        if Path(file.path).parent.name not in known:
            found.reports.append(f"{file}: the output has no row; the file is changed")


def _check_decisions(conn: Conn, found: Found) -> None:
    """That no two decisions are of one version after it, as the unique index asks."""
    moved = {
        row.id: row.new["scope_key"] for row in found.rows if row.table == "analyzer_decisions"
    }
    taken: dict[tuple[str | None, str, str], str] = {}
    for key, scope, inner, rule, instance in conn.execute(
        "SELECT id, scope_key, body->'scope'->>'key', diagnostic_id, instance"
        " FROM analyzer_decisions WHERE scope_kind = 'template_version' ORDER BY id"
    ):
        if key in moved and inner != scope:
            found.refused.append(
                f"analyzer_decisions {key}: its scope_key is {scope}, and its body says {inner}"
            )
        target = (moved.get(key, scope), rule, instance)
        if target in taken:
            found.refused.append(
                f"analyzer_decisions {taken[target]} and {key} would both be {rule} of {target[0]}"
            )
        taken[target] = key


def _sessions(conn: Conn) -> list[str]:
    """The other sessions of this command's role. Only the API has that role's password,
    and a person with it; a pool's ``application_name`` says which. Some of the API's own
    connections have no name, so the name does not decide."""
    conn.execute("SELECT pg_stat_clear_snapshot()")
    found = conn.execute(
        "SELECT pid, application_name, client_addr::text, backend_start::text"
        " FROM pg_stat_activity WHERE datname = current_database() AND usename = current_user"
        " AND backend_type = 'client backend' AND pid <> pg_backend_pid() ORDER BY pid"
    )
    return [
        f"a session of the API's role is open: pid {pid}, {name or 'no name'}, from"
        f" {address or 'a local socket'}, since {since}"
        for pid, name, address, since in found
    ]


def _refuse_sessions(conn: Conn) -> None:
    if there := _sessions(conn):
        raise SwitchRefusedError(*there)


def _running(conn: Conn) -> list[str]:
    found = []
    for table, key, state in RUNNING:
        query = sql.SQL("SELECT {} FROM {} WHERE " + state + " ORDER BY 1").format(
            sql.Identifier(key), sql.Identifier(table)
        )
        if names := [row[0] for row in conn.execute(query)]:
            found.append(f"{table} where {state}: {_named(names)}")
    return found


def _recorded_head(conn: Conn) -> str | None:
    row = conn.execute(
        sql.SQL("SELECT head FROM {} WHERE id").format(sql.Identifier(HEAD_TABLE))
    ).fetchone()
    return None if row is None else str(row[0])


def _kept_rows(conn: Conn, table: str) -> list[str]:
    """Each row of a table that is not in `COLUMNS`, as JSON text: of `PUSH_TABLE`, the one
    that is removed."""
    query = sql.SQL("SELECT to_jsonb(t)::text FROM {} t ORDER BY 1").format(sql.Identifier(table))
    rows = [str(row[0]) for row in conn.execute(query)]
    if table != PUSH_TABLE:
        return rows
    return [row for row in rows if json.loads(row)["repository"] == PUSHED]


# ── the meta.json files ───────────────────────────────────────────────────────


def _plan_files(paths: DataPaths, found: Found) -> dict[str, tuple[str, str | None]]:
    """Plan each ``meta.json`` with an old id. Each readable one's template and version, by
    its output."""
    read: dict[str, tuple[str, str | None]] = {}
    for path in sorted(paths.outputs.glob(f"*/*/{META_NAME}")):
        slug, key = path.parent.parent.name, path.parent.name
        data = path.read_bytes()
        try:
            meta = OutputMeta.model_validate_json(data)
        except ValidationError:
            meta = None
        # As `outputs._row_from_folder` reads one.
        if meta is None or (meta.slug, meta.id) != (slug, key):
            found.reports.append(f"outputs/{slug}/{key}: its {META_NAME} cannot be read; left")
            continue
        read[key] = (slug, meta.model_version)
        new = found.ids.get((slug, meta.model_version or ""))
        if new is not None:
            changed = _sha256(_with_version(data, new))
            name = path.relative_to(paths.root).as_posix()
            found.files.append(File(name, _sha256(data), changed, path.stat().st_mtime_ns))
    return read


# ── the preflight ─────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _connected(conninfo: str) -> Iterator[Conn]:
    """The API's own client. The switch's lock keeps a second one out. The models
    repository's keeps out the writes of an API that starts."""
    with psycopg.connect(conninfo, autocommit=True, application_name=APPLICATION) as conn:
        for key in (SWITCH_LOCK, MODELS_LOCK):
            row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,)
            ).fetchone()
            if row is None or not row[0]:
                raise SwitchRefusedError(f"another session holds the lock {key}")
        yield conn


def preflight(paths: DataPaths, conninfo: str) -> Found:
    """What a switch would change, and why it would refuse. It only reads."""
    try:
        with _connected(conninfo) as conn:
            conn.execute("SET default_transaction_read_only = on")
            return _survey(conn, paths, begun=False)
    except SwitchRefusedError as error:
        return Found(refused=error.reasons)


def _survey(conn: Conn, paths: DataPaths, *, begun: bool) -> Found:
    """``begun``: a whole dump is there, so the renames may be done."""
    found = Found()
    found.refused += _sessions(conn)
    found.refused += _running(conn)
    try:
        found.where = _where(paths)
        if found.where.renames and not begun:
            raise SwitchRefusedError(
                f"{OLD_NAME} is there: a switch has begun, and only `run` or `rollback` with its"
                f" own dump goes on ({_standing(paths)})"
            )
        _check_repositories(found)
    except SwitchRefusedError as error:
        found.refused += error.reasons
        return found
    read = _plan_files(paths, found)
    found.rows, reports = _plan_rows(conn, found.ids)
    found.reports += reports
    _check_pins(conn, found)
    _check_outputs(conn, found, read)
    _check_decisions(conn, found)
    recorded = _recorded_head(conn)
    if recorded not in (None, found.old_head) and not (begun and recorded == found.new_head):
        found.refused.append(
            f"{HEAD_TABLE} says {recorded}, and the HEAD of {found.where.old} is {found.old_head}"
        )
    found.counts = {table: sum(row.table == table for row in found.rows) for table in COLUMNS}
    found.counts[META_NAME] = len(found.files)
    found.counts |= {table: len(_kept_rows(conn, table)) for table in (*EMPTIED, PUSH_TABLE)}
    return found


# ── the dump ──────────────────────────────────────────────────────────────────


def _check_place(paths: DataPaths, directory: Path) -> Path:
    target = directory.resolve()
    homes = [_beside(paths, name) for name in (paths.models.name, NEXT_NAME, OLD_NAME)]
    for home in (*homes, paths.outputs):
        if target == home.resolve() or home.resolve() in target.parents:
            raise SwitchRefusedError(f"the dump {directory} is in {home}")
    if not target.parent.is_dir():
        raise SwitchRefusedError(f"the dump's parent {target.parent} is not a directory")
    if os.path.lexists(target) and not target.is_dir():
        raise SwitchRefusedError(f"the dump {directory} is not a directory")
    return target


def _lines(entries: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(f"{json.dumps(entry)}\n" for entry in entries).encode()


def _make_dump(conn: Conn, paths: DataPaths, directory: Path, found: Found) -> Dump:
    """Write the dump beside its place, and then give it its name. So one that has its name
    is whole, and one that a stop left half done is in the way of nothing."""
    partial = directory.with_name(f"{directory.name}.partial-{uuid.uuid4().hex}")
    partial.mkdir()
    ids = [[slug, old, new] for (slug, old), new in sorted(found.ids.items())]
    tables = [
        {"table": table, "row": row}
        for table in (*EMPTIED, HEAD_TABLE, PUSH_TABLE)
        for row in _kept_rows(conn, table)
    ]
    bodies = {
        ROWS: _lines([vars(row) for row in found.rows]),
        FILES: _lines([vars(file) for file in found.files]),
        TABLES: _lines(tables),
        MAP_COPY: (json.dumps(ids, indent=1) + "\n").encode(),
    }
    for file in found.files:
        data = (paths.root / file.path).read_bytes()
        if _sha256(data) != file.old_sha256:
            raise SwitchFailedError(f"{file} changed while it was dumped")
        bodies[f"files/{file.path}"] = data
    for name, data in bodies.items():
        _write(partial / name, data)
    manifest = {
        "format": FORMAT,
        "old_head": found.old_head,
        "new_head": found.new_head,
        "slugs": found.slugs,
        "files": {name: _sha256(data) for name, data in sorted(bodies.items())},
    }
    body = (json.dumps(manifest, indent=1) + "\n").encode()
    # Its own hash is its last line, so one that was cut short is seen.
    _write(partial / MANIFEST, body + f"{_sha256(body)}\n".encode())
    for at, _, _ in os.walk(partial):
        _sync(Path(at))
    _rename(partial, directory)
    made = _load_dump(directory)
    assert made is not None
    return made


def _load_dump(directory: Path) -> Dump | None:
    """The dump, whole; None where there is none yet."""
    if not directory.exists() or not any(directory.iterdir()):
        return None
    damaged = SwitchRefusedError(
        f"the dump directory {directory} is not empty, and not a whole dump of a switch"
    )
    try:
        lines = (directory / MANIFEST).read_bytes().splitlines(keepends=True)
        body = b"".join(lines[:-1])
        if not lines or lines[-1] != f"{_sha256(body)}\n".encode():
            raise damaged
        manifest = json.loads(body)
        held = {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file() and path != directory / MANIFEST
        }
        if manifest["format"] != FORMAT or manifest["files"] != {
            name: _sha256(data) for name, data in held.items()
        }:
            raise damaged
    except (OSError, ValueError) as error:
        raise damaged from error
    tables: dict[str, list[str]] = {}
    for entry in map(json.loads, held[TABLES].splitlines()):
        tables.setdefault(entry["table"], []).append(entry["row"])
    return Dump(
        directory,
        manifest["old_head"],
        manifest["new_head"],
        {(slug, old): new for slug, old, new in json.loads(held[MAP_COPY])},
        manifest["slugs"],
        [Row(**json.loads(line)) for line in held[ROWS].splitlines()],
        [File(**json.loads(line)) for line in held[FILES].splitlines()],
        tables,
    )


def _open_dump(paths: DataPaths, directory: Path) -> Dump | None:
    """`_load_dump`, and only if each file it names is an output's ``meta.json``. Its hash
    shows a dump that was cut short, and not one that was changed: a path in it is where
    `rollback` writes."""
    dump = _load_dump(directory)
    outputs = paths.outputs.resolve()
    for file in [] if dump is None else dump.files:
        parts = Path(file.path).parts
        inside = (paths.root / file.path).resolve().parent.parent.parent == outputs
        if len(parts) != 4 or parts[0] != outputs.name or parts[3] != META_NAME or not inside:
            raise SwitchRefusedError(f"the dump names {file.path}, which is not an output's")
    return dump


def _check_dump(paths: DataPaths, found: Found, dump: Dump, recorded: str | None) -> None:
    """That the dump is of this switch, and of the rows and files that are not changed."""
    if (dump.old_head, dump.new_head, dump.ids) != (found.old_head, found.new_head, found.ids):
        raise SwitchRefusedError(f"the dump {dump.directory} is of another switch")
    assert found.where is not None
    begun = found.where.renames == 2
    what = "roll back, and run again with another dump" if begun else "use another dump directory"
    done = begun and not found.rows and recorded == dump.new_head
    if found.rows != dump.rows and not done:
        raise SwitchRefusedError(f"the rows are not those of the dump {dump.directory}: {what}")
    planned = {file.path: file for file in found.files}
    if any(file not in dump.files for file in found.files):
        raise SwitchRefusedError(
            f"the {META_NAME} files are not those of the dump {dump.directory}: {what}"
        )
    for file in dump.files:
        if file.path not in planned and not (begun and _is_new(paths, file)):
            raise SwitchRefusedError(
                f"{file} is not as the dump {dump.directory} has it, and not as a switch writes"
                f" it: {what}"
            )


def _is_new(paths: DataPaths, file: File) -> bool:
    try:
        return _sha256((paths.root / file.path).read_bytes()) == file.new_sha256
    except FileNotFoundError:
        return False


# ── the switch ────────────────────────────────────────────────────────────────


def run(paths: DataPaths, conninfo: str, directory: Path) -> bool:
    """Do the switch, or finish one that stopped. False where it was done already."""
    directory = _check_place(paths, directory)
    dump = _open_dump(paths, directory)
    with _connected(conninfo) as conn:
        found = _survey(conn, paths, begun=dump is not None)
        if found.refused:
            raise SwitchRefusedError(*found.refused)
        assert found.where is not None
        for line in found.reports:
            _say(f"note: {line}")
        if dump is None:
            dump = _make_dump(conn, paths, directory, found)
            _say(f"dumped {len(dump.rows)} rows and {len(dump.files)} files to {directory}")
        _check_dump(paths, found, dump, _recorded_head(conn))
        # No API may start while `models` is missing: its sweep would delete outputs (#7649).
        _refuse_sessions(conn)
        if found.where.renames < 1:
            _rename(paths.models, _beside(paths, OLD_NAME))
            _say(f"renamed {paths.models.name} to {OLD_NAME}")
        if found.where.renames < 2:
            _rename(_beside(paths, NEXT_NAME), paths.models)
            _say(f"renamed {NEXT_NAME} to {paths.models.name}")
        where = _where(paths)
        _check_new(where, dump)
        written = _put_files(paths, dump)
        _say(f"{META_NAME}: {written} of {len(dump.files)} are written now")
        with conn.transaction():
            changed = _apply(conn, dump)
            if changed:
                _verify(conn, dump, where)
        if changed:
            _say(f"{len(dump.rows)} rows are changed")
    return changed


def _put_files(paths: DataPaths, dump: Dump) -> int:
    """Write each ``meta.json`` that is not written yet; how many those were."""
    written = 0
    for file in dump.files:
        path = paths.root / file.path
        if _is_new(paths, file):
            continue
        if not path.exists():
            # Nothing deletes one while no API runs: a write would make an orphan folder.
            raise SwitchFailedError(f"{file}: its output is gone since the dump")
        old = dump.kept(file).read_bytes()
        version = json.loads(old)["model_version"]
        data = _with_version(old, dump.ids[path.parent.parent.name, version])
        if _sha256(data) != file.new_sha256:
            raise SwitchFailedError(f"{file}: not what was planned")
        _put(path, data)
        written += 1
    return written


def _lock_tables(conn: Conn) -> None:
    """No other session writes one of them until this transaction ends, and none is open."""
    conn.execute("SET LOCAL lock_timeout = '10s'")
    names = (*COLUMNS, *EMPTIED, HEAD_TABLE, PUSH_TABLE)
    tables = sql.SQL(", ").join(map(sql.Identifier, names))
    conn.execute(sql.SQL("LOCK TABLE {} IN EXCLUSIVE MODE").format(tables))
    _refuse_sessions(conn)


def _update(conn: Conn, row: Row, values: Values) -> None:
    """Set ``values`` in the one row."""
    key, columns = COLUMNS[row.table]
    sets = sql.SQL(", ").join(
        sql.SQL("{} = %s::jsonb" if name in JSONB else "{} = %s").format(sql.Identifier(name))
        for name in columns
    )
    query = sql.SQL("UPDATE {} SET {} WHERE {} = %s").format(
        sql.Identifier(row.table), sets, sql.Identifier(key)
    )
    done = conn.execute(query, [*(values[name] for name in columns), row.id]).rowcount
    if done != 1:
        raise SwitchFailedError(f"{row}: {done} rows were changed, and not 1")


def _empty(conn: Conn, table: str) -> None:
    there = len(_kept_rows(conn, table))
    done = conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table))).rowcount
    if done != there:
        raise SwitchFailedError(f"{table}: {done} rows were removed, and not {there}")


def _apply(conn: Conn, dump: Dump) -> bool:
    """Change the rows. False where they are changed already."""
    _lock_tables(conn)
    planned, _ = _plan_rows(conn, dump.ids)
    if not planned and _recorded_head(conn) == dump.new_head:
        return False
    if planned != dump.rows:
        differ = [row for row in planned if row not in dump.rows]
        differ += [row for row in dump.rows if row not in planned]
        raise SwitchFailedError(f"not the rows of the dump: {_named(differ)}")
    for row in dump.rows:
        _update(conn, row, row.new)
    _set_head(conn, dump.new_head)
    for table in EMPTIED:
        _empty(conn, table)
    done = conn.execute(
        sql.SQL("DELETE FROM {} WHERE repository = %s").format(sql.Identifier(PUSH_TABLE)),
        (PUSHED,),
    ).rowcount
    if done != (there := len(dump.tables.get(PUSH_TABLE, []))):
        raise SwitchFailedError(f"{PUSH_TABLE}: {done} rows were removed, and not {there}")
    return True


def _set_head(conn: Conn, head: str) -> None:
    """Record ``head``, as `repo_lock` does."""
    conn.execute(
        sql.SQL(
            "INSERT INTO {} (id, head) VALUES (true, %s)"
            " ON CONFLICT (id) DO UPDATE SET head = excluded.head, updated_at = now()"
        ).format(sql.Identifier(HEAD_TABLE)),
        (head,),
    )


def _verify(conn: Conn, dump: Dump, where: Where) -> None:
    """Before the commit: no old id is left, and each pin resolves."""
    left, _ = _plan_rows(conn, dump.ids)
    if left:
        raise SwitchFailedError(f"an old id is left in {_named(left)}")
    with _Objects(_Repo(where.new)) as objects:
        lost = [pin for pin in _pins(conn) if objects.tree(pin[3], pin[2]) is None]
    if lost:
        names = [f"{table} {key}" for table, key, _, _ in lost]
        raise SwitchFailedError(f"a pin does not resolve in {where.new}: {_named(names)}")


# ── the way back ──────────────────────────────────────────────────────────────


def rollback(paths: DataPaths, conninfo: str, directory: Path) -> None:
    """Put back what the dump holds. It removes no file, and no row but those that the API
    makes again."""
    directory = _check_place(paths, directory)
    dump = _open_dump(paths, directory)
    if dump is None:
        raise SwitchRefusedError(f"the dump {directory} is not there")
    with _connected(conninfo) as conn:
        _refuse_sessions(conn)
        where = _where(paths)
        since = _check_back(where, dump)
        rows, gone, refused = _rows_back(conn, dump)
        files, missing, others = _files_back(paths, dump)
        if refused or others:
            raise SwitchRefusedError(*refused, *others)
        for line in [*gone, *missing]:
            _say(f"note: {line}")
        if where.renames == 2:
            _rename(paths.models, _beside(paths, NEXT_NAME))
            _say(f"renamed {paths.models.name} to {NEXT_NAME}")
        if where.renames >= 1:
            _rename(_beside(paths, OLD_NAME), paths.models)
            _say(f"renamed {OLD_NAME} to {paths.models.name}")
        for file in files:
            _put(paths.root / file.path, dump.kept(file).read_bytes(), file.mtime_ns)
        _say(f"{META_NAME}: {len(files)} of {len(dump.files)} are put back now")
        with conn.transaction():
            _undo_rows(conn, dump)
        _say(f"{len(rows)} of {len(dump.rows)} rows are put back now")
        for line in _left_new(conn, paths):
            _say(f"note: {line}")
        if since:
            _say(
                f"note: {NEXT_NAME} keeps {len(since)} commits that were made since the switch,"
                f" and {paths.models.name} does not have them: {_named(since)}"
            )


def _check_back(where: Where, dump: Dump) -> list[str]:
    """That the two are the dump's; the commits that were made on the new one since."""
    old, new = _Repo(where.old), _Repo(where.new)
    head = old.head() if old.available else None
    if head != dump.old_head:
        raise SwitchRefusedError(
            f"the HEAD of {where.old} is {head}, and not {dump.old_head} of the dump"
        )
    if not new.available or _held(new).get("head") != dump.new_head:
        raise SwitchRefusedError(f"{where.new} is not the new repository of the dump")
    return new._out("rev-list", f"{dump.new_head}..HEAD").split()


def _stored(conn: Conn, row: Row) -> Values | None:
    key, columns = COLUMNS[row.table]
    shown = sql.SQL(", ").join(
        sql.SQL("{}::text" if name in JSONB else "{}").format(sql.Identifier(name))
        for name in columns
    )
    query = sql.SQL("SELECT {} FROM {} WHERE {} = %s").format(
        shown, sql.Identifier(row.table), sql.Identifier(key)
    )
    found = conn.execute(query, (row.id,)).fetchone()
    return None if found is None else dict(zip(columns, found, strict=True))


def _same(now: Values, then: Values) -> bool:
    """Whether a row holds these values. A start copy of the outputs (#7255, retired by
    #7257) made an output's meta again from its file, with the defaults that the file
    leaves out."""
    return all(
        now[name] == value or (name == "meta" and _meta(now[name]) == _meta(value))
        for name, value in then.items()
    )


def _meta(text: str | None) -> OutputMeta | str | None:
    try:
        return OutputMeta.model_validate_json(text or "")
    except ValidationError:
        return text


def _rows_back(conn: Conn, dump: Dump) -> tuple[list[Row], list[str], list[str]]:
    """The rows to put back, the ones that are gone, and the ones that stop it."""
    back: list[Row] = []
    gone: list[str] = []
    refused: list[str] = []
    for row in dump.rows:
        now = _stored(conn, row)
        if now is None:
            gone.append(f"{row} is gone")
        elif now == row.old:
            continue
        elif _same(now, row.new) or _same(now, row.old):
            back.append(row)
        else:
            refused.append(f"{row} holds neither what the dump has nor what the switch wrote")
    return back, gone, refused


def _files_back(paths: DataPaths, dump: Dump) -> tuple[list[File], list[str], list[str]]:
    """As `_rows_back`, of the files."""
    back: list[File] = []
    gone: list[str] = []
    refused: list[str] = []
    for file in dump.files:
        path = paths.root / file.path
        try:
            digest = _sha256(path.read_bytes())
        except FileNotFoundError:
            gone.append(f"{file} is gone")
            continue
        if digest == file.old_sha256 and path.stat().st_mtime_ns == file.mtime_ns:
            continue
        if digest in (file.old_sha256, file.new_sha256):
            back.append(file)
        else:
            refused.append(f"{file} holds neither what the dump has nor what the switch wrote")
    return back, gone, refused


def _undo_rows(conn: Conn, dump: Dump) -> None:
    _lock_tables(conn)
    rows, _, refused = _rows_back(conn, dump)
    if refused:
        raise SwitchRefusedError(*refused)
    for row in rows:
        _update(conn, row, row.old)
    for table in EMPTIED:
        _empty(conn, table)
    _set_head(conn, dump.old_head)
    for table, key in ((HEAD_TABLE, "id"), (PUSH_TABLE, "repository")):
        for body in dump.tables.get(table, []):
            _put_row(conn, table, key, body)


def _put_row(conn: Conn, table: str, key: str, body: str) -> None:
    """A dumped row as it was, over what is there."""
    sets = sql.SQL(", ").join(
        sql.SQL("{0} = excluded.{0}").format(sql.Identifier(name)) for name in json.loads(body)
    )
    query = sql.SQL(
        "INSERT INTO {0} SELECT * FROM jsonb_populate_record(NULL::{0}, %s::jsonb)"
        " ON CONFLICT ({1}) DO UPDATE SET {2}"
    ).format(sql.Identifier(table), sql.Identifier(key), sets)
    conn.execute(query, (body,))


def _left_new(conn: Conn, paths: DataPaths) -> list[str]:
    """The rows that were made since the switch: they name a commit that the old one does
    not have."""
    where = _where(paths)
    places = [(f"{table} {key}", slug, commit) for table, key, slug, commit in _pins(conn)]
    places += [
        (f"outputs {key}", slug, version) for key, slug, version in _outputs(conn) if version
    ]
    with _Objects(_Repo(where.old)) as before, _Objects(_Repo(where.new)) as after:
        return [
            f"{place} names {slug} at {commit}, which only {NEXT_NAME} has"
            for place, slug, commit in places
            if before.tree(commit, slug) is None and after.tree(commit, slug) is not None
        ]


# ── the commands ──────────────────────────────────────────────────────────────


def main(argv: Sequence[str]) -> int:
    from scadbuddy.core.settings import Settings

    parser = argparse.ArgumentParser(prog="python -m scadbuddy.library.models_switch")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="say what a switch would change; it only reads")
    for name, text in (
        ("run", "put the new repository in place, or finish a run that stopped"),
        ("rollback", "put the old repository and what the dump holds back"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--dump", type=Path, required=True, help="the dump's directory")
    options = parser.parse_args(argv)
    settings = Settings()
    paths = DataPaths(settings.data_dir)
    try:
        if options.command == "preflight":
            return _say_found(preflight(paths, settings.database_url))
        if options.command == "run":
            changed = run(paths, settings.database_url, options.dump)
            _say(
                f"{'done' if changed else 'already done'}: {paths.models} is the new repository,"
                f" at {_Repo(paths.models).head()}; the old one is {_beside(paths, OLD_NAME)},"
                " and it stays"
            )
        else:
            rollback(paths, settings.database_url, options.dump)
            _say(
                f"done: {paths.models} is the old repository, at {_Repo(paths.models).head()};"
                f" the new one is {_beside(paths, NEXT_NAME)}"
            )
        return 0
    except SwitchRefusedError as error:
        for reason in error.reasons:
            print(f"refused: {reason}", file=sys.stderr)
    except (SwitchFailedError, RefusedError, GitError, OSError, psycopg.Error) as error:
        print(f"stopped: {error}", file=sys.stderr)
    print(
        f"{_standing(paths)}. To go on, give the same `run` again; to go back, `rollback` with"
        " the same dump. Where models-old is there, start no API before one of them is done.",
        file=sys.stderr,
    )
    return 2


def _say_found(found: Found) -> int:
    for name, count in found.counts.items():
        _say(f"{name}: {count}")
    for line in found.reports:
        _say(f"note: {line}")
    for reason in found.refused:
        print(f"refused: {reason}", file=sys.stderr)
    if found.refused:
        return 2
    _say(f"ready: {found.old_head} to {found.new_head}, {len(found.ids)} ids")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
