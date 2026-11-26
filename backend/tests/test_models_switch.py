"""The switch to the second models repository (#7314 step 5): each row and file it changes,
what it leaves, what makes it refuse, a stop at each point, and the way back."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from scadbuddy.analyzers.decisions import PostgresDecisionStore
from scadbuddy.analyzers.model import ScopeRef
from scadbuddy.core.paths import DataPaths
from scadbuddy.library import models_switch
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.media_store import PostgresMediaStore
from scadbuddy.library.models_next import MAP_NAME, Pin, build
from scadbuddy.library.models_switch import (
    SwitchFailedError,
    SwitchRefusedError,
    preflight,
    rollback,
    run,
)
from scadbuddy.library.output_rows import OutputRows
from scadbuddy.library.outputs import META_NAME, OutputMeta, OutputStore
from scadbuddy.library.repo_lock import LOCK_KEY, PgRepoCoordinator
from scadbuddy.library.template_copy import TemplateCopyResult, copy_templates
from scadbuddy.library.template_refresh import TemplateRefresh
from scadbuddy.library.template_rows import TemplateRows
from scadbuddy.orders.models import Actor, Quote
from scadbuddy.orders.printing import _stands
from scadbuddy.orders.rendering import reference_matches
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.inputs import ResolvedChoice
from scadbuddy.orders.storefront.listings import ListingStore
from scadbuddy.orders.storefront.renders import variant_key
from scadbuddy.render.inputs import inputs_key
from scadbuddy.render.job_models import render_key
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.conftest import PgPool, open_pg_pool
from tests.test_models_next import ONE, Old, git

pytestmark = [
    pytest.mark.requires_git,
    pytest.mark.requires_postgres,
    pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs is not on PATH"),
]

AT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
INPUTS: dict[str, Any] = {"params": {"w": 3, "r": 0.5}, "v": 0}
#: A pipeline's own state is in its key too.
WHOLE: dict[str, Any] = {"params": {"w": 3}, "v": 2, "text": "Élan"}
BARE: dict[str, Any] = {"params": {}, "v": 0}
CONTENT_HASH = "c" * 64
VIDEO = b"video" * 100
PUSH = {"repository": "models", "url": "https://example.test/o/r", "error": "GH001"}
#: The tables whose rows may differ after a switch, and the columns that may.
COLUMNS = {
    "order_items": ("commit", "render_key"),
    "order_item_revisions": ("commit", "render_key"),
    "storefront_listings": ("commit",),
    "outputs": ("meta",),
    "analyzer_decisions": ("scope_key", "body"),
}
REBUILT = ("templates", "template_copy_runs")
CHANGED = {*COLUMNS, *REBUILT, "models_repo_head", "git_remote_pushes"}
NAMES = ("models", "models-next", "models-old")


class Killed(BaseException):
    """The process is gone."""


def output_id(number: int) -> str:
    return f"{number:032x}"


@dataclass
class World:
    data: Path
    conninfo: str
    old: Old
    #: The old commits, by name.
    commits: dict[str, str]
    #: (slug, old id) to new id.
    ids: dict[tuple[str, str], str]
    dump: Path
    #: The database, the outputs and the repositories as they were.
    before: dict[str, Any] = field(default_factory=dict)

    @property
    def paths(self) -> DataPaths:
        return DataPaths(self.data)

    def new(self, slug: str, name: str) -> str:
        return self.ids[slug, self.commits[name]]

    def run(self) -> None:
        run(self.paths, self.conninfo, self.dump)

    def rollback(self) -> None:
        rollback(self.paths, self.conninfo, self.dump)

    def meta_path(self, slug: str, number: int) -> Path:
        return self.paths.output_dir(slug, output_id(number)) / META_NAME

    def sql(self, query: str, *args: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.conninfo, autocommit=True) as conn:
            cursor = conn.execute(query.encode(), args)
            return cursor.fetchall() if cursor.description is not None else []

    def column(self, table: str, key: str, *columns: str) -> dict[str, tuple[Any, ...]]:
        found = self.sql(f"SELECT {key}, {', '.join(columns)} FROM {table}")
        return {row[0]: row[1:] for row in found}

    @contextmanager
    def pool(self) -> Iterator[PgPool]:
        opened = open_pg_pool(self.conninfo)
        try:
            yield opened
        finally:
            opened.close()

    @contextmanager
    def outputs(self) -> Iterator[tuple[OutputStore, PgPool]]:
        with self.pool() as pool:
            yield OutputStore(self.paths, rows=OutputRows(pool), bytes=None), pool


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").decode().strip()


def database(world: World) -> dict[str, list[str]]:
    """Every row of every table, as text."""
    named = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    with psycopg.connect(world.conninfo, autocommit=True) as conn:
        return {
            table: [row[0] for row in conn.execute(f'SELECT t::text FROM "{table}" t ORDER BY 1')]
            for (table,) in conn.execute(named).fetchall()
        }


def others(world: World) -> dict[str, list[tuple[Any, ...]]]:
    """Each row of a table that changes, without the columns that do."""
    found = {}
    for table, columns in COLUMNS.items():
        without = " - ".join(f"'{name}'" for name in columns)
        kept = f"(to_jsonb(t) - {without})::text"
        found[table] = world.sql(f"SELECT {kept} FROM {table} t ORDER BY 1")
    return found


def tree(root: Path) -> dict[str, tuple[str, int]]:
    """Every file under ``root``: its bytes and its time."""
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def snapshot(world: World) -> dict[str, Any]:
    there = [name for name in NAMES if (world.data / name).is_dir()]
    return {
        "database": database(world),
        "outputs": tree(world.paths.outputs),
        "repositories": {name: tree(world.data / name) for name in there},
    }


def insert(conn: psycopg.Connection[Any], table: str, row: dict[str, Any]) -> None:
    names = ", ".join(row)
    values = ", ".join(f"%({name})s" for name in row)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({values})".encode(), row)


def item(conn: psycopg.Connection[Any], name: str, position: int, **more: Any) -> None:
    row = {
        "id": name,
        "order_id": "o",
        "position": position,
        "quantity": 1,
        "title": name,
        "kind": "model",
        "template_name": "t",
        "created_at": AT,
        "updated_at": AT,
    }
    insert(conn, "order_items", {**row, **more})


def revision(conn: psycopg.Connection[Any], name: str, **more: Any) -> None:
    row = {"id": f"r-{name}", "item_id": name, "number": 1, "template_name": "t"}
    fixed = {"created_at": AT, "created_by": Jsonb({})}
    insert(conn, "order_item_revisions", {**row, **fixed, **more})


def listing(conn: psycopg.Connection[Any], name: str, **more: Any) -> None:
    row = {"id": name, "title": name, "position": 1, "created_at": AT, "updated_at": AT}
    insert(conn, "storefront_listings", {**row, **more})


def decision(conn: psycopg.Connection[Any], name: str, kind: str, key: str) -> None:
    body = {
        "id": name,
        "diagnostic_id": "SB1",
        "kind": "ignore",
        "scope": {"kind": kind, "key": key},
        "created_at": AT.isoformat(),
    }
    row: dict[str, Any] = {"id": name, "diagnostic_id": "SB1", "scope_kind": kind, "scope_key": key}
    row |= {"kind": "ignore", "created_at": AT}
    insert(conn, "analyzer_decisions", {**row, "body": Jsonb(body)})


def output(paths: DataPaths, slug: str, number: int, version: str | None, inputs: Any) -> None:
    """An output's files, as a save leaves them."""
    body = {
        "id": output_id(number),
        "slug": slug,
        "model_version": version,
        "name": f"Größe {number}",
        "job_id": f"job{number}",
        "created_at": AT.isoformat(),
        "bbox_mm": {"min": [0, 0, 0], "max": [1, 1, 1.5], "size": [1, 1, 1.5]},
        "colors": ["#ff0000"],
    }
    meta = OutputMeta.model_validate(body)
    directory = paths.output_dir(slug, meta.id)
    directory.mkdir(parents=True)
    text = json.dumps(meta.model_dump(mode="json"), indent=2) + "\n"
    (directory / META_NAME).write_text(text, encoding="utf-8")
    (directory / "params.json").write_text(json.dumps(inputs["params"]), encoding="utf-8")
    (directory / "inputs.json").write_text(json.dumps(inputs), encoding="utf-8")


def seed(world: World) -> None:
    c = world.commits
    plain = render_key("a", INPUTS["params"], c["a1"])
    whole = inputs_key("p", WHOLE, c["p1"])
    bare = render_key("builtin:b", {}, c["b1"])
    a = {"slug": "a", "inputs": Jsonb(INPUTS)}
    p = {"slug": "p", "inputs": Jsonb(WHOLE)}
    b = {"slug": "builtin:b", "inputs": Jsonb(BARE)}
    with psycopg.connect(world.conninfo, autocommit=True) as conn:
        order = {"id": "o", "currency": "USD", "created_by": Jsonb({}), "source": "storefront"}
        insert(conn, "orders", order)
        item(conn, "plain", 1, **a, commit=c["a1"], render_key=plain, output_id=output_id(1))
        item(conn, "pipeline", 2, **p, commit=c["p1"], render_key=whole, output_id=output_id(2))
        item(conn, "neither", 3, **a, commit=c["a2"], render_key="k" * 64)
        item(conn, "builtin", 4, **b, commit=c["b1"], render_key=bare)
        item(conn, "custom", 5, kind="custom", requires_printing=False, template_name=None)
        second = render_key("a", INPUTS["params"], c["a2"])
        revision(conn, "plain", **a, commit=c["a2"], render_key=second)
        revision(conn, "pipeline", **p, commit=c["p1"], render_key=whole)
        revision(conn, "neither", **a, commit=c["a3"], render_key="n" * 64)
        shown = {
            "schema": {"parameters": []},
            "pipeline": False,
            "template_name": "a",
            "libraries": [],
            "snapshot_key": f"snapshot-a-{c['a1']}",
        }
        held = {"published": True, "snapshot": Jsonb(shown)}
        listing(conn, "shown", slug="a", commit=c["a1"], **held)
        listing(conn, "draft", slug="builtin:b", commit=c["b1"])
        file = Jsonb({"id": 1, "library": "l", "filename": "f.3mf"})
        listing(conn, "file", source="library_file", library_file=file)
        hold = {"key": shown["snapshot_key"], "holder_kind": "storefront_listing"}
        insert(conn, "blob_refs", {**hold, "holder_id": "shown"})
        decision(conn, "d-mapped", "template_version", f"a@{c['a1']}")
        decision(conn, "d-builtin", "template_version", f"builtin:b@{c['b1']}")
        decision(conn, "d-hash", "template_version", f"a@{CONTENT_HASH}")
        decision(conn, "d-template", "template", "a")
        # An id that is another template's, and the right one in another kind of scope.
        decision(conn, "d-other", "template_version", f"gone@{c['a3']}")
        decision(conn, "d-print", "print", f"a@{c['a1']}")
        insert(conn, "models_repo_head", {"head": head(world.data / "models")})
        insert(conn, "git_remote_pushes", PUSH)
        insert(conn, "git_remote_pushes", {**PUSH, "repository": "flows"})
        # Commits of other repositories, and records of what was, with an id of ours.
        insert(conn, "flows_repo_head", {"head": c["a1"]})
        flow = {"id": "w", "name": "w", "version": 1, "created_by": Jsonb({})}
        insert(conn, "workflow_definitions", {**flow, "commit": c["a1"]})
        lease = {"token": "0" * 8 + "-0000-0000-0000-" + "0" * 12, "holder": "h", "library": "a"}
        insert(conn, "library_leases", {**lease, "commit": c["a1"], "expires_at": AT})
        job = {"id": "j", "slug": "a", "state": "done", "created_at": AT, "render_key": plain}
        insert(conn, "render_jobs", {**job, "model_version": c["a1"]})
        done: dict[str, Any] = {
            "kind": "k",
            "subject": "a",
            "operation_key": "k",
            "status": "succeeded",
        }
        done |= {"workflow_id": "w", "workflow_run_id": "r", "request": Jsonb({"commit": c["a1"]})}
        insert(conn, "operations", {"id": "op", **done})
        price = {"price_key": "x", "model_sha256": "m", "status": "failed", "failure": "f"}
        insert(conn, "storefront_slice_prices", price)
        media = {"template_id": "a", "id": "m", "position": 1, "file": "m.png", "kind": "image"}
        insert(conn, "template_media", {**media, "source_commit": c["a1"]})


@dataclass(frozen=True)
class Made:
    """The files of a world, made once: each test has a copy."""

    data: Path
    commits: dict[str, str]
    ids: dict[tuple[str, str], str]


@pytest.fixture(scope="module")
def made(tmp_path_factory: pytest.TempPathFactory) -> Made:
    old = Old(tmp_path_factory.mktemp("switch") / "data")
    c: dict[str, str] = {}
    c["a1"] = old.save("a", scad="cube(1);\n", meta='{"name": "A"}\n', mesh=ONE)
    c["p1"] = old.save("p", scad="cube(1);\n", meta='{"name": "P"}\n')
    c["b1"] = old.save("_builtin/b", scad="cube(1);\n", meta='{"name": "B"}\n')
    c["a2"] = old.save("a", scad="cube(2);\n")
    c["g1"] = old.save("gone", scad="cube(1);\n")
    old.delete("gone")
    c["a3"] = old.save("a", scad="cube(3);\n")
    old.write("a", {"media/clip.mp4": VIDEO})
    pins = [("a", "a1"), ("a", "a2"), ("a", "a3"), ("p", "p1"), ("builtin:b", "b1"), ("gone", "g1")]
    # At this commit, another's, `a` is as it was at a1: two old ids and one new.
    pins.append(("a", "p1"))
    dest = old.data / "models-next"
    build(old.root, dest, [Pin(slug, c[name]) for slug, name in pins], live=old.root)
    held = json.loads((dest / ".git" / MAP_NAME).read_text())
    ids = {(entry["slug"], entry["old"]): entry["new"] for entry in held["map"]}
    assert ids["a", c["a1"]] == ids["a", c["p1"]]
    paths = DataPaths(old.data)
    paths.ensure()
    output(paths, "a", 1, c["a1"], INPUTS)
    output(paths, "p", 2, c["p1"], WHOLE)
    output(paths, "a", 3, c["p1"], INPUTS)
    output(paths, "a", 4, None, INPUTS)
    output(paths, "a", 5, CONTENT_HASH, INPUTS)
    # A commit that the old repository has lost.
    output(paths, "p", 6, "0" * 39 + "1", INPUTS)
    output(paths, "gone", 7, c["g1"], INPUTS)
    output(paths, "builtin:b", 8, c["b1"], BARE)
    return Made(old.data, c, ids)


@pytest.fixture
def world(made: Made, tmp_path: Path, pg_conninfo: str) -> World:
    data = tmp_path / "data"
    shutil.copytree(made.data, data, symlinks=True)
    old = Old.__new__(Old)
    old.data, old.root, old.clock = data, data / "models", 1_800_000_000
    copy = World(data, pg_conninfo, old, made.commits, made.ids, data / "switch-1")
    seed(copy)
    rows_from_files(copy)
    start_copies(copy)
    copy.before = {**snapshot(copy), "others": others(copy)}
    return copy


def catalogue(world: World, pool: PgPool) -> tuple[Catalogue, TemplateRows, TemplateRefresh]:
    """The app's own, on the repository in place."""
    history = ModelHistory(
        world.paths.models,
        wrapper_prefix=WRAPPER_PREFIX,
        timeout=10.0,
        coordinator=PgRepoCoordinator(world.conninfo),
    )
    found = Catalogue(
        world.paths, history, media_store=PostgresMediaStore(pool), wrapper_prefix=WRAPPER_PREFIX
    )
    rows = TemplateRows(pool)
    refresh = TemplateRefresh(found, rows)
    found.refresh = refresh
    history.template_rows = refresh
    return found, rows, refresh


def rows_from_files(world: World) -> None:
    """Each output's row written from its files, and a finished copy run recorded, as
    release A's copy of the outputs left the database (#7255). Release B retired that
    copy (#7257)."""
    with world.outputs() as (store, _):
        assert store.rows is not None
        for slug in sorted(path.name for path in world.paths.outputs.iterdir()):
            store.restore_rows(slug)
        run = store.rows.start_run()
        store.rows.finish_run(run, copied=0, refreshed=0, dropped=0, not_copied=[], damaged=[])


def start_copies(world: World) -> TemplateCopyResult:
    """The copy of an API's start, with the app's own functions: the templates' only,
    since release B retired the copy of the outputs (#7257)."""
    with world.pool() as pool:
        found, rows, refresh = catalogue(world, pool)
        templates = copy_templates(found, rows, refresh, pool)
    assert templates is not None
    return templates


# ── the end state ─────────────────────────────────────────────────────────────


def assert_switched(world: World) -> None:
    """The whole end state, whichever way the switch got there."""
    before, new = world.before, world.new
    now = snapshot(world)
    assert now["repositories"] == {
        "models": before["repositories"]["models-next"],
        "models-old": before["repositories"]["models"],
    }
    a1, p1, b1 = new("a", "a1"), new("p", "p1"), new("builtin:b", "b1")
    plain = render_key("a", INPUTS["params"], a1)
    whole = inputs_key("p", WHOLE, p1)
    assert world.column("order_items", "id", "commit", "render_key") == {
        "plain": (a1, plain),
        "pipeline": (p1, whole),
        "neither": (new("a", "a2"), "k" * 64),
        "builtin": (b1, render_key("builtin:b", {}, b1)),
        "custom": (None, None),
    }
    assert world.column("order_item_revisions", "id", "commit", "render_key") == {
        "r-plain": (new("a", "a2"), render_key("a", INPUTS["params"], new("a", "a2"))),
        "r-pipeline": (p1, whole),
        "r-neither": (new("a", "a3"), "n" * 64),
    }
    assert world.column("storefront_listings", "id", "commit") == {
        "shown": (a1,),
        "draft": (b1,),
        "file": (None,),
    }
    assert world.sql("SELECT DISTINCT updated_at FROM storefront_listings") == [(AT,)]
    assert world.sql("SELECT DISTINCT updated_at FROM order_items") == [(AT,)]
    assert_outputs(world, now)
    keys = {
        "d-mapped": f"a@{a1}",
        "d-builtin": f"builtin:b@{b1}",
        "d-hash": f"a@{CONTENT_HASH}",
        "d-template": "a",
        "d-other": f"gone@{world.commits['a3']}",
        "d-print": f"a@{world.commits['a1']}",
    }
    decisions = world.column("analyzer_decisions", "id", "scope_key", "body")
    assert {name: row[0] for name, row in decisions.items()} == keys
    assert {name: row[1]["scope"]["key"] for name, row in decisions.items()} == keys
    assert world.sql("SELECT head FROM models_repo_head") == [(head(world.data / "models"),)]
    assert world.sql("SELECT repository FROM git_remote_pushes") == [("flows",)]
    for table in REBUILT:
        assert now["database"][table] == []
    was = before["database"]
    assert {table for table in was if was[table] != now["database"][table]} == CHANGED
    assert others(world) == before["others"]


def assert_outputs(world: World, now: dict[str, Any]) -> None:
    changed = {
        ("a", 1): world.new("a", "a1"),
        ("p", 2): world.new("p", "p1"),
        ("a", 3): world.new("a", "a1"),
        ("gone", 7): world.new("gone", "g1"),
        ("builtin:b", 8): world.new("builtin:b", "b1"),
    }
    metas = dict(world.sql("SELECT output_id, meta FROM outputs"))
    names = set()
    for (slug, number), version in changed.items():
        path = world.meta_path(slug, number)
        names.add(path.relative_to(world.paths.outputs).as_posix())
        meta = OutputMeta.model_validate_json(path.read_text(encoding="utf-8"))
        assert (meta.model_version, meta.name) == (version, f"Größe {number}")
        # As `OutputStore.rename` writes it, and the row has the same.
        body = meta.model_dump(mode="json")
        assert path.read_text(encoding="utf-8") == json.dumps(body, indent=2) + "\n"
        assert metas[output_id(number)] == body
    was = world.before["outputs"]
    assert set(now["outputs"]) == set(was)
    assert {name for name in was if was[name] != now["outputs"][name]} == names


def test_each_row_and_file_names_the_new_id(world: World) -> None:
    world.run()
    assert_switched(world)


def test_a_second_run_changes_nothing(world: World) -> None:
    world.run()
    first = snapshot(world)
    world.run()
    assert snapshot(world) == first


def test_without_rows_to_change(tmp_path: Path, pg_conninfo: str) -> None:
    old = Old(tmp_path / "data")
    old.save("a", scad="cube(1);\n")
    dest = old.data / "models-next"
    build(old.root, dest, [], live=old.root)
    world = World(old.data, pg_conninfo, old, {}, {}, old.data / "switch-1")
    world.paths.ensure()
    expected = head(dest)
    world.run()
    world.run()
    # There was no recorded HEAD.
    assert world.sql("SELECT head FROM models_repo_head") == [(expected,)]
    world.rollback()
    assert world.sql("SELECT head FROM models_repo_head") == [(head(world.data / "models"),)]
    assert head(dest) == expected


def test_the_dump_holds_what_was(world: World) -> None:
    metas = {name: entry[0] for name, entry in world.before["outputs"].items()}
    world.run()
    rows = [json.loads(line) for line in (world.dump / "rows.jsonl").read_text().splitlines()]
    was = {(row["table"], row["id"]): row["old"] for row in rows}
    assert was["order_items", "plain"] == {
        "commit": world.commits["a1"],
        "render_key": render_key("a", INPUTS["params"], world.commits["a1"]),
    }
    assert len([row for row in rows if row["table"] == "outputs"]) == 5
    tables = [json.loads(line) for line in (world.dump / "tables.jsonl").read_text().splitlines()]
    assert {row["table"] for row in tables} >= {*REBUILT, "models_repo_head", "git_remote_pushes"}
    held = tree(world.dump / "files/outputs")
    assert len(held) == 5
    assert all(metas[name] == entry[0] for name, entry in held.items())


# ── what the app makes of it ──────────────────────────────────────────────────


def test_the_start_copies_leave_what_was_written(world: World) -> None:
    world.run()
    written = world.column("outputs", "output_id", "meta")
    files = tree(world.paths.outputs)
    templates = start_copies(world)
    assert world.column("outputs", "output_id", "meta") == written
    assert tree(world.paths.outputs) == files
    assert (templates.added, templates.incomplete) == (3, False)
    with world.pool() as pool:
        assert TemplateRows(pool).copied()
        versions = {name: row[1] for name, row in TemplateRows(pool).stamps().items()}
    assert versions == {
        "a": world.new("a", "a3"),
        "p": world.new("p", "p1"),
        "builtin:b": world.new("builtin:b", "b1"),
    }
    start_copies(world)
    assert world.column("outputs", "output_id", "meta") == written


def answers(world: World) -> dict[str, Any]:
    """What the order checks say of each pinned item."""
    actor = Actor(kind="browser")
    quote = Quote(
        id="q",
        order_item_id="i",
        created_at=AT,
        source="manual",
        accepted=True,
        sliced_sha256=None,
        sliced_blob_key=None,
        plates=[],
        lines=[],
        unit_cost_minor=1,
        unit_price_minor=1,
        quantity=1,
        total_minor=1,
        created_by=actor,
    )
    found: dict[str, Any] = {}
    with world.outputs() as (store, pool):
        for name in ("plain", "pipeline"):
            row = OrderStore(pool).item(name)
            assert row is not None and row.reference is not None and row.output_id is not None
            reference = row.reference
            blobs: Any = None
            found[name] = {
                "matches": reference_matches(store, row.output_id, reference) is not None,
                "stands": asyncio.run(
                    _stands(store, blobs, row, reference, quote, 1, None),
                ),
                # `orders/rendering.py` tells a pipeline so.
                "pipeline": reference.render_key
                == inputs_key(reference.slug, reference.inputs, reference.commit),
            }
        # A key that is neither kind stays: it is read as a plain item's, as before.
        neither = OrderStore(pool).item("neither")
        assert neither is not None and neither.reference is not None
        found["neither"] = (neither.reference.commit, neither.reference.render_key) == (
            neither.reference.commit,
            inputs_key(neither.reference.slug, neither.reference.inputs, neither.reference.commit),
        )
        shown = ListingStore(pool).get("shown")
        assert shown is not None and shown.template_snapshot is not None
        found["variant"] = variant_key(shown, ResolvedChoice("PLA", {"w": 3}, []), {0: "f"})
        found["snapshot"] = shown.template_snapshot.snapshot_key
    return found


def test_the_order_checks_answer_as_before(world: World) -> None:
    before = answers(world)
    assert before["plain"]["matches"] and before["pipeline"]["matches"]
    assert (before["plain"]["pipeline"], before["pipeline"]["pipeline"]) == (False, True)
    assert before["neither"] is False
    world.run()
    assert answers(world) == before
    start_copies(world)
    assert answers(world) == before
    assert world.sql("SELECT key FROM blob_refs WHERE holder_id = 'shown'") == [
        (before["snapshot"],)
    ]


def test_the_app_finds_the_decision_and_can_save(world: World) -> None:
    world.run()
    start_copies(world)
    scope = ScopeRef(kind="template_version", key=f"a@{world.new('a', 'a1')}")
    store = PostgresDecisionStore(world.conninfo)
    try:
        assert [found.id for found in store.list(scopes=[scope])] == ["d-mapped"]
    finally:
        store.close()
    with world.pool() as pool:
        found, _, _ = catalogue(world, pool)
        history = found.history
        assert history is not None
        (world.data / "models/a/model.scad").write_text("cube(9);\n")
        # The recorded HEAD is one that this repository has.
        assert history.commit("Save", "a") == history.head()


# ── a stop at each point ──────────────────────────────────────────────────────


def kill_on_call(monkeypatch: pytest.MonkeyPatch, target: object, name: str, call: int) -> None:
    """The process ends before the ``call``th call."""
    real = getattr(target, name)
    calls = 0

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == call:
            raise Killed
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapped)


def kill_after(monkeypatch: pytest.MonkeyPatch, words: str) -> None:
    """The process ends once it has said ``words``."""
    real = models_switch._say

    def said(line: str) -> None:
        real(line)
        if words in line:
            raise Killed

    monkeypatch.setattr(models_switch, "_say", said)


STOPS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "in the dump's first file": lambda m: kill_on_call(m, models_switch, "_write", 1),
    "in the dump's third file": lambda m: kill_on_call(m, models_switch, "_write", 3),
    "before the dump has its name": lambda m: kill_on_call(m, models_switch, "_rename", 1),
    "after the dump": lambda m: kill_after(m, "dumped"),
    "before the first rename": lambda m: kill_on_call(m, models_switch, "_rename", 2),
    "between the renames": lambda m: kill_on_call(m, models_switch, "_rename", 3),
    "after the renames": lambda m: kill_after(m, "renamed models-next"),
    "before the first meta.json": lambda m: kill_on_call(m, models_switch, "_put", 1),
    "before the fourth meta.json": lambda m: kill_on_call(m, models_switch, "_put", 4),
    "after the meta.json files": lambda m: kill_after(m, "meta.json"),
    "in the transaction": lambda m: kill_on_call(m, models_switch, "_verify", 1),
    "after the transaction": lambda m: kill_after(m, "rows are changed"),
}


@pytest.mark.parametrize("stop", list(STOPS))
def test_a_run_after_a_stop_finishes_it(world: World, stop: str) -> None:
    with pytest.MonkeyPatch.context() as patch:
        STOPS[stop](patch)
        with pytest.raises(Killed):
            world.run()
    world.run()
    assert_switched(world)


@pytest.mark.parametrize("stop", list(STOPS))
def test_a_rollback_after_a_stop(world: World, stop: str) -> None:
    with pytest.MonkeyPatch.context() as patch:
        STOPS[stop](patch)
        with pytest.raises(Killed):
            world.run()
    if not world.dump.exists():
        with pytest.raises(SwitchRefusedError, match="dump"):
            world.rollback()
        assert_untouched(world, emptied=False)
        return
    world.rollback()
    assert_untouched(world)


def assert_untouched(world: World, *, emptied: bool = True) -> None:
    """All as it was before the switch, byte for byte; after a rollback the tables that the
    API makes again are empty."""
    now, was = snapshot(world), {**world.before["database"]}
    if emptied:
        was |= {table: [] for table in REBUILT}
    assert now["database"] == was
    assert now["outputs"] == world.before["outputs"]
    assert now["repositories"] == world.before["repositories"]


# ── the way back ──────────────────────────────────────────────────────────────


def test_a_rollback_gives_back_what_was(world: World) -> None:
    world.run()
    world.rollback()
    assert_untouched(world)
    world.rollback()
    assert_untouched(world)
    # And the switch can be done again.
    world.run()
    assert_switched(world)


@pytest.mark.parametrize(
    ("name", "call"),
    [("_rename", 1), ("_rename", 2), ("_put", 1), ("_put", 3), ("_undo_rows", 1)],
)
def test_a_rollback_after_one_that_stopped(world: World, name: str, call: int) -> None:
    world.run()
    with pytest.MonkeyPatch.context() as patch:
        kill_on_call(patch, models_switch, name, call)
        with pytest.raises(Killed):
            world.rollback()
    world.rollback()
    assert_untouched(world)


def test_a_rollback_after_an_api_has_started(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    metas = world.column("outputs", "output_id", "meta")
    world.run()
    start_copies(world)
    (world.data / "models/a/model.scad").write_text("cube(9);\n")
    with world.pool() as pool:
        found, _, _ = catalogue(world, pool)
        assert found.history is not None
        since = found.history.commit("Save", "a")
    assert since is not None
    # The push that the save started.
    world.sql("INSERT INTO git_remote_pushes (repository, url, pushed_commit) VALUES"
              " ('models', 'https://example.test/o/r', %s)", since)  # fmt: skip
    with psycopg.connect(world.conninfo, autocommit=True) as conn:
        inputs = Jsonb(INPUTS)
        item(conn, "since", 9, slug="a", inputs=inputs, commit=since, render_key="s" * 64)
    capsys.readouterr()
    world.rollback()
    said = capsys.readouterr().out
    assert f"order_items since names a at {since}" in said
    assert "keeps 1 commits" in said
    assert head(world.data / "models-next") == since
    now = snapshot(world)
    assert now["outputs"] == world.before["outputs"]
    assert now["repositories"]["models"] == world.before["repositories"]["models"]
    assert world.column("outputs", "output_id", "meta") == metas
    for table in ("order_item_revisions", "storefront_listings", "analyzer_decisions"):
        assert now["database"][table] == world.before["database"][table]
    for table in ("models_repo_head", "git_remote_pushes"):
        assert now["database"][table] == world.before["database"][table]
    for table in REBUILT:
        assert now["database"][table] == []
    templates = start_copies(world)
    assert not templates.incomplete
    assert world.column("outputs", "output_id", "meta") == metas


def test_a_rollback_of_a_row_that_the_start_copy_made_again(world: World) -> None:
    """A file of an older build has not every key, and the copy wrote each default: a
    row written again from its files (`restore_rows`, as release A's copy did) still
    rolls back (`models_switch._same`)."""
    path = world.meta_path("a", 1)
    body = json.loads(path.read_text())
    del body["sent_to_bambuddy"]
    path.write_text(json.dumps(body, indent=2) + "\n")
    query = "SELECT meta::text FROM outputs WHERE output_id = %s"
    world.sql("UPDATE outputs SET meta = meta - 'sent_to_bambuddy'")
    was = world.sql(query, output_id(1))
    world.run()
    with world.outputs() as (store, _):
        store.restore_rows("a")
    assert "sent_to_bambuddy" in world.sql(query, output_id(1))[0][0]
    world.rollback()
    assert world.sql(query, output_id(1)) == was


def test_a_rollback_where_no_push_was_recorded(world: World) -> None:
    world.sql("DELETE FROM git_remote_pushes WHERE repository = 'models'")
    world.run()
    world.sql("INSERT INTO git_remote_pushes (repository, url) VALUES ('models', 'u')")
    world.rollback()
    # It never deletes a row that it did not put there.
    assert ("models",) in world.sql("SELECT repository FROM git_remote_pushes")


GONE: dict[str, str] = {
    "order_items": "DELETE FROM order_items WHERE id = 'plain'",
    "storefront_listings": "DELETE FROM storefront_listings WHERE id = 'shown'",
    "outputs": f"DELETE FROM outputs WHERE output_id = '{output_id(1)}'",
    "analyzer_decisions": "DELETE FROM analyzer_decisions WHERE id = 'd-mapped'",
}


@pytest.mark.parametrize("table", list(GONE))
def test_a_rollback_reports_a_row_that_is_gone(
    world: World, table: str, capsys: pytest.CaptureFixture[str]
) -> None:
    world.run()
    world.sql(GONE[table])
    world.rollback()
    assert table in capsys.readouterr().out
    kept = {t for t in COLUMNS if t not in (table, "order_item_revisions")}
    now = database(world)
    assert {t: now[t] for t in kept} == {t: world.before["database"][t] for t in kept}
    assert tree(world.paths.outputs) == world.before["outputs"]


def test_a_rollback_reports_a_file_that_is_gone(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.run()
    shutil.rmtree(world.meta_path("a", 1).parent)
    world.rollback()
    assert output_id(1) in capsys.readouterr().out
    assert not world.meta_path("a", 1).exists()


OTHER: dict[str, str] = {
    "order_items": "UPDATE order_items SET commit = repeat('9', 40) WHERE id = 'plain'",
    "render_key": "UPDATE order_item_revisions SET render_key = 'z' WHERE id = 'r-plain'",
    "storefront_listings": "UPDATE storefront_listings SET commit = repeat('9', 40)"
    " WHERE id = 'shown'",
    "outputs": "UPDATE outputs SET meta = jsonb_set(meta, '{name}', '\"other\"')"
    f" WHERE output_id = '{output_id(1)}'",
    "analyzer_decisions": 'UPDATE analyzer_decisions SET body = body || \'{"reason": "r"}\''
    " WHERE id = 'd-mapped'",
}


@pytest.mark.parametrize("name", list(OTHER))
def test_a_rollback_refuses_a_row_that_is_neither(world: World, name: str) -> None:
    world.run()
    world.sql(OTHER[name])
    before = snapshot(world)
    with pytest.raises(SwitchRefusedError, match="neither"):
        world.rollback()
    assert snapshot(world) == before


def test_a_rollback_refuses_a_file_that_is_neither(world: World) -> None:
    world.run()
    path = world.meta_path("a", 1)
    path.write_text(path.read_text().replace("job1", "job2"))
    before = snapshot(world)
    with pytest.raises(SwitchRefusedError, match="neither"):
        world.rollback()
    assert snapshot(world) == before


def test_a_rollback_refuses_a_live_session(world: World) -> None:
    world.run()
    before = snapshot(world)
    with (
        psycopg.connect(world.conninfo, application_name="scadbuddy-api"),
        pytest.raises(SwitchRefusedError, match="scadbuddy-api"),
    ):
        world.rollback()
    assert snapshot(world) == before


def test_a_rollback_refuses_an_old_one_that_went_on(world: World) -> None:
    """Rolled back, and the API ran since: the recorded HEAD is not the dump's to set."""
    world.run()
    world.rollback()
    world.old.save("a", scad="cube(7);\n")
    before = snapshot(world)
    with pytest.raises(SwitchRefusedError, match="HEAD"):
        world.rollback()
    assert snapshot(world) == before


# ── what makes it refuse ──────────────────────────────────────────────────────


def _forge(world: World, change: Callable[[dict[str, Any]], object]) -> None:
    path = world.data / "models-next/.git" / MAP_NAME
    held = json.loads(path.read_text())
    change(held)
    path.write_text(json.dumps(held))


def _drop_template(world: World) -> None:
    root = world.data / "models-next"
    git(root, "rm", "-q", "-r", "p")
    git(root, "commit", "-q", "-m", "Damage")
    _forge(world, lambda held: held.update(head=head(root)))


def _wrong_map(held: dict[str, Any]) -> None:
    held["map"][0]["new"] = held["map"][-1]["new"]


def _pin(world: World, table: str, **more: Any) -> None:
    commit = {"slug": "a", "commit": "9" * 40}
    with psycopg.connect(world.conninfo, autocommit=True) as conn:
        if table == "storefront_listings":
            listing(conn, "x", **commit)
            return
        fixed = {**commit, "inputs": Jsonb(INPUTS), "render_key": "k"}
        if table == "order_items":
            item(conn, "x", 8, **fixed)
        else:
            revision(conn, "builtin", **fixed)


def _job(world: World, state: str) -> None:
    job = {"id": "x", "slug": "a", "created_at": AT, "render_key": "x", "state": state}
    with psycopg.connect(world.conninfo, autocommit=True) as conn:
        insert(conn, "render_jobs", job)


def _collision(world: World) -> None:
    with psycopg.connect(world.conninfo, autocommit=True) as conn:
        decision(conn, "d-twin", "template_version", f"a@{world.commits['p1']}")


def _unmapped_output(world: World) -> None:
    """One that the builder should have kept: the old repository has its version."""
    lost = world.old.save("a", scad="cube(8);\n")
    git(world.old.root, "reset", "-q", "--hard", "HEAD~1")
    output(world.paths, "a", 9, lost, INPUTS)
    with world.outputs() as (store, _):
        store.restore_rows("a")


def _row_is_not_file(world: World) -> None:
    path = world.meta_path("a", 1)
    path.write_text(path.read_text().replace(world.commits["a1"], world.commits["a2"]))


def _new_video(world: World) -> None:
    world.old.write("a", {"media/second.mp4": VIDEO})


def _changed_video(world: World) -> None:
    world.old.write("a", {"media/clip.mp4": VIDEO + b"."})


def _link(world: World) -> None:
    (world.data / "models-next").rename(world.data / "elsewhere")
    (world.data / "models-next").symlink_to("elsewhere")


BAD: dict[str, tuple[Callable[[World], object], str]] = {
    "a changed new one": (
        lambda w: (w.data / "models-next/a/model.scad").write_text("cube(0);\n"),
        "model.scad",
    ),
    "a new one that went on": (
        lambda w: git(w.data / "models-next", "commit", "-q", "--allow-empty", "-m", "x"),
        "models-next",
    ),
    "a forged map": (lambda w: _forge(w, _wrong_map), "trailers"),
    "a map without its entries": (lambda w: _forge(w, lambda h: h.pop("map")), "trailers"),
    "an old one that went on": (lambda w: w.old.save("a", scad="cube(7);\n"), "was made from"),
    "an uncommitted change": (
        lambda w: (w.old.root / "a/model.scad").write_text("cube(7);\n"),
        "a/model.scad",
    ),
    "an uncommitted template": (lambda w: w.old.write("q", {"model.scad": "x"}), "q/model.scad"),
    "a video since": (_new_video, "second.mp4"),
    "a video changed since": (_changed_video, "clip.mp4"),
    "a template that is missing": (_drop_template, "p"),
    "no new one": (lambda w: (w.data / "models-next").rename(w.data / "x"), "models-next"),
    "a link": (_link, "link"),
    "a models-old": (lambda w: (w.data / "models-old").mkdir(), "models-old"),
    "an item's pin": (lambda w: _pin(w, "order_items"), "order_items"),
    "a revision's pin": (lambda w: _pin(w, "order_item_revisions"), "order_item_revisions"),
    "a listing's pin": (lambda w: _pin(w, "storefront_listings"), "storefront_listings"),
    "an output's version": (_unmapped_output, output_id(9)),
    "a pending render": (lambda w: _job(w, "pending"), "render_jobs"),
    "a running render": (lambda w: _job(w, "running"), "render_jobs"),
    "a running operation": (
        lambda w: w.sql("UPDATE operations SET status = 'running'"),
        "operations",
    ),
    "a running slice": (
        lambda w: w.sql("UPDATE storefront_slice_prices SET status = 'running'"),
        "storefront_slice_prices",
    ),
    "a backfill": (
        lambda w: w.sql('UPDATE outputs SET backfill = \'{"job_id": "j"}\''),
        "backfill",
    ),
    "another recorded HEAD": (
        lambda w: w.sql("UPDATE models_repo_head SET head = repeat('9', 40)"),
        "models_repo_head",
    ),
    "two decisions that would be one": (_collision, "d-twin"),
    "a body that is of another scope": (
        lambda w: w.sql(
            "UPDATE analyzer_decisions SET body = jsonb_set(body, '{scope,key}', '\"x\"')"
            " WHERE id = 'd-mapped'"
        ),
        "d-mapped",
    ),
    "a row that is not its file": (_row_is_not_file, output_id(1)),
}


@pytest.mark.parametrize("kind", list(BAD))
def test_a_preflight_refuses_and_nothing_changes(world: World, kind: str) -> None:
    change, word = BAD[kind]
    change(world)
    before = snapshot(world)
    found = preflight(world.paths, world.conninfo)
    assert any(word in reason for reason in found.refused), found.refused
    with pytest.raises(SwitchRefusedError):
        world.run()
    assert snapshot(world) == before
    assert not world.dump.exists()


def test_a_live_session_is_refused(world: World) -> None:
    before = snapshot(world)
    with psycopg.connect(world.conninfo, application_name="scadbuddy-jobs"):
        found = preflight(world.paths, world.conninfo)
        assert len(found.refused) == 1 and "scadbuddy-jobs" in found.refused[0]
        with pytest.raises(SwitchRefusedError, match="scadbuddy-jobs"):
            world.run()
    assert snapshot(world) == before


def test_one_that_starts_in_the_middle_stops_the_rows(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the renames, an API came up."""
    real, opened = models_switch._put_files, []

    def then_connect(*args: Any) -> Any:
        done = real(*args)
        opened.append(psycopg.connect(world.conninfo, application_name="scadbuddy-jobs"))
        return done

    monkeypatch.setattr(models_switch, "_put_files", then_connect)
    with pytest.raises(SwitchRefusedError, match="scadbuddy-jobs"):
        world.run()
    for conn in opened:
        conn.close()
    assert database(world) == world.before["database"]
    monkeypatch.undo()
    world.run()
    assert_switched(world)


def test_one_that_starts_while_it_dumps_stops_the_renames(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real, opened = models_switch._say, []

    def then_connect(line: str) -> None:
        real(line)
        if "dumped" in line:
            opened.append(psycopg.connect(world.conninfo, application_name="scadbuddy-jobs"))

    monkeypatch.setattr(models_switch, "_say", then_connect)
    with pytest.raises(SwitchRefusedError, match="scadbuddy-jobs"):
        world.run()
    for conn in opened:
        conn.close()
    assert_untouched(world, emptied=False)
    monkeypatch.undo()
    world.run()
    assert_switched(world)


def test_a_backfill_that_failed_and_a_job_that_ended_are_not_waited_for(world: World) -> None:
    world.sql('UPDATE outputs SET backfill = \'{"job_id": "j", "error": "e"}\'')
    _job(world, "cancelled")
    assert preflight(world.paths, world.conninfo).refused == []


def test_the_preflight_only_reads_and_says_what_it_would_change(world: World) -> None:
    found = preflight(world.paths, world.conninfo)
    assert snapshot(world) == {k: world.before[k] for k in ("database", "outputs", "repositories")}
    assert found.refused == []
    assert found.counts == {
        "order_items": 4,
        "order_item_revisions": 3,
        "storefront_listings": 2,
        "outputs": 5,
        "analyzer_decisions": 2,
        "meta.json": 5,
        "templates": 3,
        "template_copy_runs": 1,
        "git_remote_pushes": 1,
    }
    # The keys that are neither, and the version that no repository has.
    reported = "\n".join(found.reports)
    assert "order_items neither" in reported and "order_item_revisions r-neither" in reported
    assert output_id(6) in reported
    assert output_id(4) not in reported and output_id(5) not in reported


def test_an_output_with_no_row_or_no_folder_is_reported(world: World) -> None:
    world.sql("DELETE FROM outputs WHERE output_id = %s", output_id(1))
    shutil.rmtree(world.meta_path("p", 2).parent)
    found = preflight(world.paths, world.conninfo)
    assert found.refused == []
    reported = "\n".join(found.reports)
    assert output_id(1) in reported and output_id(2) in reported
    world.run()
    # The file is changed; with no row, the database has nothing to change.
    body = json.loads(world.meta_path("a", 1).read_text())
    assert body["model_version"] == world.new("a", "a1")
    assert world.sql("SELECT meta FROM outputs WHERE output_id = %s", output_id(1)) == []


def test_a_meta_json_that_cannot_be_read_is_left_and_reported(world: World) -> None:
    path = world.meta_path("a", 1)
    path.write_text("{half")
    world.sql("DELETE FROM outputs WHERE output_id = %s", output_id(1))
    found = preflight(world.paths, world.conninfo)
    assert found.refused == [] and output_id(1) in "\n".join(found.reports)
    world.run()
    assert path.read_text() == "{half"


def test_a_number_keeps_its_digits_and_an_unknown_key_stays(world: World) -> None:
    world.sql("UPDATE outputs SET meta = meta || '{\"later\": 1.10}' WHERE output_id = %s",
              output_id(1))  # fmt: skip
    path = world.meta_path("a", 1)
    body = {**json.loads(path.read_text()), "later": 1.1}
    path.write_text(json.dumps(body, indent=2) + "\n")
    before = world.sql("SELECT meta::text FROM outputs WHERE output_id = %s", output_id(1))
    world.run()
    now = world.sql("SELECT meta::text FROM outputs WHERE output_id = %s", output_id(1))
    assert '"later": 1.10' in now[0][0]
    assert json.loads(path.read_text()) == {**body, "model_version": world.new("a", "a1")}
    world.rollback()
    assert world.sql("SELECT meta::text FROM outputs WHERE output_id = %s", output_id(1)) == before


# ── the dump directory ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path", ["outputs/../../escape/meta.json", "/tmp/escape/meta.json", "models/a/model.json"]
)
def test_a_dump_that_names_a_file_outside_the_outputs_is_refused(world: World, path: str) -> None:
    world.run()
    files = world.dump / "files.jsonl"
    lines = files.read_bytes().splitlines(keepends=True)
    first = json.loads(lines[0])
    first["path"] = path
    files.write_bytes(json.dumps(first).encode() + b"\n" + b"".join(lines[1:]))
    manifest = world.dump / "manifest.json"
    body = json.loads(b"".join(manifest.read_bytes().splitlines(keepends=True)[:-1]))
    body["files"]["files.jsonl"] = hashlib.sha256(files.read_bytes()).hexdigest()
    signed = (json.dumps(body, indent=1) + "\n").encode()
    manifest.write_bytes(signed + f"{hashlib.sha256(signed).hexdigest()}\n".encode())
    before = snapshot(world)
    with pytest.raises(SwitchRefusedError, match="not an output's"):
        world.rollback()
    with pytest.raises(SwitchRefusedError, match="not an output's"):
        world.run()
    assert snapshot(world) == before
    assert not (world.paths.root.parent / "escape").exists()


def test_a_dump_directory_that_holds_something_else_is_refused(world: World) -> None:
    world.dump.mkdir()
    (world.dump / "x").write_text("x")
    before = snapshot(world)
    with pytest.raises(SwitchRefusedError, match="dump"):
        world.run()
    assert snapshot(world) == before
    assert (world.dump / "x").read_text() == "x"


def test_an_empty_dump_directory_is_used(world: World) -> None:
    world.dump.mkdir()
    world.run()
    assert_switched(world)


@pytest.mark.parametrize("name", ["models/x", "models-next/x", "models-old/x", "outputs/x", "no/x"])
def test_a_dump_directory_in_a_bad_place_is_refused(world: World, name: str) -> None:
    world.dump = world.data / name
    before = snapshot(world)
    with pytest.raises(SwitchRefusedError, match="dump"):
        world.run()
    assert snapshot(world) == before


@pytest.mark.parametrize("name", ["rows.jsonl", "files.jsonl", "manifest.json", "map.json"])
def test_a_damaged_dump_is_refused(world: World, name: str) -> None:
    with pytest.MonkeyPatch.context() as patch:
        kill_after(patch, "dumped")
        with pytest.raises(Killed):
            world.run()
    path = world.dump / name
    path.write_text(path.read_text()[:-3])
    for command in (world.run, world.rollback):
        with pytest.raises(SwitchRefusedError, match="dump"):
            command()
    assert_untouched(world, emptied=False)


def test_a_dump_of_rows_that_changed_since_is_refused(world: World) -> None:
    with pytest.MonkeyPatch.context() as patch:
        kill_after(patch, "dumped")
        with pytest.raises(Killed):
            world.run()
    world.sql("UPDATE order_items SET commit = %s WHERE id = 'plain'", world.commits["a3"])
    with pytest.raises(SwitchRefusedError, match="another dump"):
        world.run()
    assert (world.data / "models-next").is_dir()


def test_a_switch_that_stopped_needs_its_own_dump(world: World) -> None:
    with pytest.MonkeyPatch.context() as patch:
        kill_on_call(patch, models_switch, "_rename", 3)
        with pytest.raises(Killed):
            world.run()
    world.dump = world.data / "switch-2"
    with pytest.raises(SwitchRefusedError, match="models-old"):
        world.run()
    with pytest.raises(SwitchRefusedError, match="dump"):
        world.rollback()
    assert not (world.data / "models").exists()


def test_a_dump_of_another_switch_is_refused(world: World) -> None:
    with pytest.MonkeyPatch.context() as patch:
        kill_after(patch, "dumped")
        with pytest.raises(Killed):
            world.run()
    world.old.save("a", scad="cube(6);\n")
    dest = world.data / "models-next"
    pins = [Pin(slug, commit) for slug, commit in world.ids]
    build(world.old.root, dest, pins, live=world.old.root)
    world.sql("UPDATE models_repo_head SET head = %s", head(world.old.root))
    with pytest.raises(SwitchRefusedError, match="another switch"):
        world.run()


# ── what it finds in the middle ───────────────────────────────────────────────


def _commit_to_next(world: World) -> None:
    git(world.data / "models-next", "commit", "-q", "--allow-empty", "-m", "x")


def _lose_a_template(world: World) -> None:
    shutil.rmtree(world.data / "models-next/p")


@pytest.mark.parametrize(
    ("change", "word"), [(_commit_to_next, "the HEAD of"), (_lose_a_template, "has no p")]
)
def test_another_new_one_after_the_renames_stops_it(
    world: World, monkeypatch: pytest.MonkeyPatch, change: Callable[[World], None], word: str
) -> None:
    real = models_switch._say

    def then_change(line: str) -> None:
        real(line)
        if "dumped" in line:
            change(world)

    monkeypatch.setattr(models_switch, "_say", then_change)
    with pytest.raises(SwitchFailedError, match=word):
        world.run()
    assert database(world) == world.before["database"]
    assert tree(world.paths.outputs) == world.before["outputs"]


def test_an_output_whose_folder_went_is_not_made_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = models_switch._put_files
    folder = world.meta_path("a", 1).parent

    def then_put(*args: Any) -> Any:
        shutil.rmtree(folder)
        return real(*args)

    monkeypatch.setattr(models_switch, "_put_files", then_put)
    with pytest.raises(SwitchFailedError, match=output_id(1)):
        world.run()
    assert not folder.exists()


def test_a_row_made_in_the_middle_stops_it(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    real = models_switch._put_files

    def then_order(*args: Any) -> Any:
        done = real(*args)
        with psycopg.connect(world.conninfo, autocommit=True) as conn:
            commit = {"slug": "a", "commit": world.commits["a1"], "render_key": "k"}
            item(conn, "late", 9, inputs=Jsonb(INPUTS), **commit)
        return done

    monkeypatch.setattr(models_switch, "_put_files", then_order)
    with pytest.raises(SwitchFailedError, match="not the rows of the dump: order_items late"):
        world.run()
    now, was = database(world), world.before["database"]
    # An item's row writes its status change.
    assert {t for t in was if was[t] != now[t]} == {"order_items", "order_status_changes"}
    monkeypatch.undo()
    with pytest.raises(SwitchRefusedError):
        world.run()
    # The row is in the way of going on, and not of going back.
    world.rollback()
    assert world.column("order_items", "id", "commit")["late"] == (world.commits["a1"],)
    assert world.column("order_items", "id", "commit")["plain"] == (world.commits["a1"],)


SURPRISES = {
    "count": "0 rows were changed",
    "pin": "a pin does not resolve",
    "left": "an old id is left in storefront_listings shown",
    "left in an output": f"an old id is left in outputs {output_id(1)}",
    "left in a decision": "an old id is left in analyzer_decisions d-mapped",
}


@pytest.mark.parametrize("what", list(SURPRISES))
def test_a_surprise_in_the_transaction_changes_no_row(
    world: World, monkeypatch: pytest.MonkeyPatch, what: str
) -> None:
    real = models_switch._apply

    def damaged(conn: Any, dump: Any) -> Any:
        if what == "count":
            # A listing that a trigger takes away again.
            conn.execute(
                "CREATE FUNCTION skip() RETURNS trigger LANGUAGE plpgsql AS"
                " 'BEGIN RETURN NULL; END'"
            )
            conn.execute(
                "CREATE TRIGGER skip BEFORE UPDATE ON storefront_listings"
                " FOR EACH ROW EXECUTE FUNCTION skip()"
            )
            return real(conn, dump)
        done = real(conn, dump)
        commit = world.commits["a1"]
        if what == "pin":
            conn.execute("UPDATE order_items SET commit = repeat('9', 40) WHERE id = 'plain'")
        elif what == "left":
            conn.execute("UPDATE storefront_listings SET commit = %s WHERE id = 'shown'", (commit,))
        elif what == "left in an output":
            conn.execute(
                "UPDATE outputs SET meta = jsonb_set(meta, '{model_version}', to_jsonb(%s::text))"
                " WHERE output_id = %s",
                (commit, output_id(1)),
            )
        else:
            conn.execute(
                "UPDATE analyzer_decisions SET scope_key = %s WHERE id = 'd-mapped'",
                (f"a@{commit}",),
            )
        return done

    monkeypatch.setattr(models_switch, "_apply", damaged)
    with pytest.raises(SwitchFailedError, match=SURPRISES[what]):
        world.run()
    assert database(world) == world.before["database"]
    monkeypatch.undo()
    world.run()
    assert_switched(world)


@pytest.mark.parametrize("key", [models_switch.SWITCH_LOCK, LOCK_KEY])
def test_two_at_once_are_refused(world: World, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    with pytest.MonkeyPatch.context() as patch:
        kill_after(patch, "dumped")
        with pytest.raises(Killed):
            world.run()
    # The other one's session is what the check of sessions sees too.
    monkeypatch.setattr(models_switch, "_sessions", lambda conn: [])
    with psycopg.connect(world.conninfo, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
        for command in (world.run, world.rollback):
            with pytest.raises(SwitchRefusedError, match="lock"):
                command()
    assert_untouched(world, emptied=False)


# ── the command ───────────────────────────────────────────────────────────────


@pytest.fixture
def environment(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_DATABASE_URL", world.conninfo)
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_ADDRESS", "127.0.0.1:1")
    monkeypatch.setenv("SCADBUDDY_DATA_DIR", str(world.data))


@pytest.mark.usefixtures("environment")
def test_the_commands(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    dump = ["--dump", str(world.dump)]
    assert models_switch.main(["preflight"]) == 0
    out = capsys.readouterr().out
    assert "order_items: 4" in out and "order_items neither" in out
    assert models_switch.main(["run", *dump]) == 0
    out = capsys.readouterr().out
    assert "done" in out.splitlines()[-1] and "models-old" in out.splitlines()[-1]
    assert models_switch.main(["run", *dump]) == 0
    assert "already" in capsys.readouterr().out
    assert models_switch.main(["preflight"]) == 2
    assert "refused" in capsys.readouterr().err
    assert models_switch.main(["rollback", *dump]) == 0
    assert "done" in capsys.readouterr().out.splitlines()[-1]
    with pytest.raises(SystemExit):
        models_switch.main(["run"])


@pytest.mark.usefixtures("environment")
def test_a_stop_says_where_it_stands(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(*_: object) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(models_switch, "_put", full)
    assert models_switch.main(["run", "--dump", str(world.dump)]) == 2
    err = capsys.readouterr().err
    assert "No space left" in err and "models-old" in err and "run" in err and "rollback" in err


def test_a_file_is_whole_or_not_there(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: object) -> None:
        raise OSError("gone")

    path = world.meta_path("a", 1)
    was = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", broken)
        with pytest.raises(OSError, match="gone"):
            models_switch._put(path, b"new")
    assert path.read_bytes() == was
    assert {p.name for p in path.parent.iterdir()} == {META_NAME, "params.json", "inputs.json"}
