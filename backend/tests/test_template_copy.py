"""The copy of the templates' files to rows at every API start and in every housekeeping
sweep (#7317, model records spec §5.3, plan Task 5): it fills, repairs and drops rows,
under its own advisory lock, and takes the models write lock one batch at a time."""

from __future__ import annotations

import errno
import json
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

from scadbuddy.core.gitrepo import GitError, GitTimeoutError
from scadbuddy.core.paths import DataPaths
from scadbuddy.library import template_copy
from scadbuddy.library.catalogue import Catalogue, ModelMeta
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.media_store import PostgresMediaStore
from scadbuddy.library.repo_lock import PgRepoCoordinator
from scadbuddy.library.template_copy import COPY_LOCK, TemplateCopyResult, copy_templates
from scadbuddy.library.template_refresh import TemplateRefresh
from scadbuddy.library.template_rows import TemplateRow, TemplateRows
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.conftest import PgPool, open_pg_pool

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

SOURCE = 'colour = "red"; // color\ncube(1);\n'


@dataclass
class World:
    catalogue: Catalogue
    history: ModelHistory | None
    rows: TemplateRows
    refresh: TemplateRefresh
    pool: PgPool
    conninfo: str


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[PgPool]:
    opened = open_pg_pool(pg_conninfo)
    try:
        yield opened
    finally:
        opened.close()


def _bundle(root: Path, slug: str, source: str) -> None:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.scad").write_text(source, encoding="utf-8")
    (directory / "model.json").write_text(json.dumps({"name": slug}), encoding="utf-8")


def _world(tmp_path: Path, pool: PgPool, conninfo: str, *, git: bool = True) -> World:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    history: ModelHistory | None = None
    if git:
        history = ModelHistory(
            paths.models,
            wrapper_prefix=WRAPPER_PREFIX,
            timeout=10.0,
            coordinator=PgRepoCoordinator(conninfo),
        )
        history.ensure_repo()
    catalogue = Catalogue(
        paths, history, media_store=PostgresMediaStore(pool), wrapper_prefix=WRAPPER_PREFIX
    )
    rows = TemplateRows(pool)
    refresh = TemplateRefresh(catalogue, rows)
    catalogue.refresh = refresh
    if history is not None:
        history.template_rows = refresh
    image = tmp_path / "image"
    _bundle(image, "keychain", "cube(10);\n")
    catalogue.sync_builtins(image)
    catalogue.create("demo", SOURCE, ModelMeta(name="Demo"))
    catalogue.create("other", "cube(2);\n", ModelMeta(name="Other"))
    return World(catalogue, history, rows, refresh, pool, conninfo)


@pytest.fixture
def world(tmp_path: Path, pool: PgPool, pg_conninfo: str) -> World:
    return _world(tmp_path, pool, pg_conninfo)


def _copy(world: World, stop: threading.Event | None = None) -> TemplateCopyResult:
    result = copy_templates(world.catalogue, world.rows, world.refresh, world.pool, stop)
    assert result is not None
    return result


def _empty(world: World) -> None:
    with world.pool.connection() as conn:
        conn.execute("DELETE FROM templates")


def _expected(world: World, template_id: str) -> TemplateRow | None:
    head = None if world.history is None else world.history.head()
    version = world.catalogue.version(template_id)
    return world.catalogue.template_row(template_id, version=version, head=head)


def _git(world: World, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(world.catalogue.paths.models), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


IDS = ["demo", "other", "builtin:keychain"]


def test_copy_fills_every_row_and_finishes_the_run(world: World) -> None:
    _empty(world)
    result = _copy(world)
    assert sorted(world.rows.ids()) == sorted(IDS)
    for template_id in IDS:
        assert world.rows.get(template_id) == _expected(world, template_id)
    assert result.added == len(IDS)
    assert (result.refreshed, result.dropped, result.incomplete) == (0, 0, False)
    run = world.rows.last_run()
    assert run is not None
    assert run["finished_at"] is not None
    assert not run["incomplete"]
    assert run["added"] == len(IDS)
    assert world.rows.copied()


def test_a_second_run_changes_nothing(world: World) -> None:
    _empty(world)
    _copy(world)
    before = world.rows.all()
    result = _copy(world)
    assert (result.added, result.refreshed, result.dropped) == (0, 0, 0)
    assert world.rows.all() == before


def test_copy_repairs_a_hand_edit_of_model_json(world: World) -> None:
    _copy(world)
    world.catalogue.paths.model_meta("demo").write_text('{"name": "By hand"}', encoding="utf-8")
    result = _copy(world)
    assert result.refreshed == 1
    row = world.rows.get("demo")
    assert row is not None and row.name == "By hand"
    assert row == _expected(world, "demo")


def test_copy_repairs_a_commit_made_outside_the_catalogue(world: World) -> None:
    _copy(world)
    world.catalogue.paths.model_meta("other").write_text('{"name": "Committed"}', encoding="utf-8")
    _git(world, "add", "other")
    _git(world, "commit", "-q", "-m", "By hand", "--", "other")
    commit = _git(world, "rev-parse", "HEAD")
    result = _copy(world)
    assert result.refreshed == 1
    row = world.rows.get("other")
    assert row is not None
    assert (row.name, row.version) == ("Committed", commit)


def test_copy_drops_a_row_whose_folder_is_gone(world: World) -> None:
    _copy(world)
    shutil.rmtree(world.catalogue.paths.model_dir("other"))
    result = _copy(world)
    assert result.dropped == 1
    assert world.rows.get("other") is None
    run = world.rows.last_run()
    assert run is not None and run["finished_at"] is not None


def _commit_after_the_walk(world: World, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`Catalogue.versions` answers the walk's versions, then a save through the catalogue
    commits: the commit lands after the walk and before the first batch."""
    landed: list[str] = []
    walk = Catalogue.versions

    def versions(self: Catalogue) -> dict[str, str]:
        found = walk(self)
        if not landed:
            self.write_source("demo", SOURCE.replace("cube(1)", "cube(7)"))
            head = self.version("demo")
            assert head is not None
            landed.append(head)
        return found

    monkeypatch.setattr(Catalogue, "versions", versions)
    return landed


def test_copy_does_not_undo_a_commit_made_after_its_walk(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _copy(world)
    landed = _commit_after_the_walk(world, monkeypatch)
    _copy(world)
    row = world.rows.get("demo")
    assert row is not None
    assert row.version == landed[0]
    assert row == _expected(world, "demo")


def test_copy_reads_each_version_when_the_walk_base_is_not_visible(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Task 5 amendment 1: `last_commits_since` raises GitError when this replica cannot
    # see the walk's HEAD; the batch then reads each template's last commit in full.
    _copy(world)
    landed = _commit_after_the_walk(world, monkeypatch)

    def unseen(self: ModelHistory, base: str, ids: Any) -> dict[str, str]:
        raise GitError("bad revision", "fatal: bad revision 'base..HEAD'")

    monkeypatch.setattr(ModelHistory, "last_commits_since", unseen)
    _copy(world)
    row = world.rows.get("demo")
    assert row is not None
    assert row.version == landed[0]
    assert row == _expected(world, "demo")


def test_copy_listing_error_is_incomplete_and_drops_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _copy(world)
    before = world.rows.all()
    shutil.rmtree(world.catalogue.paths.model_dir("other"))
    models = world.catalogue.paths.models
    listing = Path.iterdir

    def stale(self: Path) -> Iterator[Path]:
        if self == models:
            raise OSError(errno.ESTALE, "Stale file handle")
        return listing(self)

    monkeypatch.setattr(Path, "iterdir", stale)
    result = _copy(world)
    assert result.incomplete
    assert result.dropped == 0
    assert world.rows.all() == before
    run = world.rows.last_run()
    assert run is not None
    assert run["finished_at"] is None
    assert run["incomplete"]


def test_copy_names_unreadable_templates(world: World) -> None:
    world.catalogue.paths.model_meta("demo").write_text("{not json", encoding="utf-8")
    result = _copy(world)
    assert [item["template_id"] for item in result.unreadable] == ["demo"]
    assert result.unreadable[0]["error"].startswith("not JSON")
    run = world.rows.last_run()
    assert run is not None
    assert run["unreadable"] == result.unreadable
    # A row the next run keeps is named too.
    again = _copy(world)
    assert (again.added, again.refreshed) == (0, 0)
    assert again.unreadable == result.unreadable


def test_copy_runs_once_under_its_lock(world: World) -> None:
    with psycopg.connect(world.conninfo, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (COPY_LOCK,))
        before = world.rows.last_run()
        assert copy_templates(world.catalogue, world.rows, world.refresh, world.pool) is None
        assert world.rows.last_run() == before
    assert copy_templates(world.catalogue, world.rows, world.refresh, world.pool) is not None


def test_copy_stops_unfinished_on_stop(world: World) -> None:
    _empty(world)
    stop = threading.Event()
    stop.set()
    _copy(world, stop)
    assert world.rows.ids() == []
    run = world.rows.last_run()
    assert run is not None
    assert run["finished_at"] is None
    assert not run["incomplete"]
    assert not world.rows.copied()


def test_copy_without_git_fills_rows_with_no_version(
    tmp_path: Path, pool: PgPool, pg_conninfo: str
) -> None:
    world = _world(tmp_path, pool, pg_conninfo, git=False)
    _empty(world)
    result = _copy(world)
    assert result.added == len(IDS)
    for template_id in IDS:
        row = world.rows.get(template_id)
        assert row is not None and row.version is None
        assert row == _expected(world, template_id)
    run = world.rows.last_run()
    assert run is not None and run["finished_at"] is not None


def test_copy_takes_the_write_lock_per_batch(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    # Task 5 amendment 4: a save waits for at most one batch, never the whole run.
    _empty(world)
    monkeypatch.setattr(template_copy, "BATCH", 1)
    history = world.history
    assert history is not None
    taken: list[int] = []
    lock = history.write_lock

    @contextmanager
    def counted() -> Iterator[None]:
        taken.append(1)
        with lock():
            yield

    monkeypatch.setattr(history, "write_lock", counted)
    read = world.refresh.read
    held: list[bool] = []

    def checked(*args: Any, **kwargs: Any) -> Any:
        held.append(history._lock.locked())
        return read(*args, **kwargs)

    monkeypatch.setattr(world.refresh, "read", checked)
    _copy(world)
    assert held == [True] * len(IDS)
    # One hold per batch, and one for the drop.
    assert len(taken) == len(IDS) + 1
    assert not history._lock.locked()


def test_a_write_lock_timeout_in_a_batch_is_incomplete_and_the_run_goes_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Task 5 review B-M1: a `GitTimeoutError` on the write lock (a long save on another
    # replica) costs that batch only. The run copies the other batches, drops nothing and
    # ends `incomplete`.
    _empty(world)
    monkeypatch.setattr(template_copy, "BATCH", 1)
    history = world.history
    assert history is not None
    lock = history.write_lock
    tries: list[int] = []

    @contextmanager
    def first_times_out() -> Iterator[None]:
        tries.append(1)
        if len(tries) == 1:
            raise GitTimeoutError("the models lock was held past git_timeout")
        with lock():
            yield

    monkeypatch.setattr(history, "write_lock", first_times_out)
    result = _copy(world)

    assert result.incomplete is True
    assert result.added == len(IDS) - 1
    assert len(world.rows.ids()) == len(IDS) - 1
    last = world.rows.last_run()
    assert last is not None
    assert last["incomplete"] is True and last["finished_at"] is None
