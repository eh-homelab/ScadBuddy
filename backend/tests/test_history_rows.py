"""Template rows move with the models HEAD (#7317, model records spec §5.2, §6).

A commit's rows are written in the transaction that records the HEAD, after the HEAD
and before the NOTIFY, so a replica that hears the commit reads rows that show it. A
change that commits nothing refreshes the rows of its paths under the same lock.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

import psycopg
import pytest

from scadbuddy.library.history import GitError, ModelHistory
from scadbuddy.library.repo_lock import HEAD_CHANNEL, PgRepoCoordinator, decode_change
from scadbuddy.library.template_rows import TemplateRow, write_rows
from scadbuddy.render.pg_store import migrate
from scadbuddy.render.solids import WRAPPER_PREFIX

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _row(template_id: str, version: str) -> TemplateRow:
    return TemplateRow(
        template_id=template_id,
        origin="builtin" if template_id.startswith("builtin:") else "mine",
        meta={"name": template_id},
        meta_error=None,
        meta_text=None,
        name=template_id,
        version=version,
        updated_at=NOW,
        has_readme=False,
        inputs_version=0,
        colour_params=None,
        files={},
        cover=None,
        files_stamp="stamp",
        refreshed_head=version,
    )


@dataclass
class FakeHook:
    """A `TemplateRowsHook` that writes a plain row per model, and notes its calls."""

    history: ModelHistory | None = None
    mode: Literal["rows", "fails", "unreadable"] = "rows"
    commits: list[tuple[list[str], str]] = field(default_factory=list)
    refreshes: list[tuple[list[str], str, bool]] = field(default_factory=list)
    #: What the HEAD row read on the transaction's own connection, when it ran.
    seen_head: list[str | None] = field(default_factory=list)

    def for_commit(self, models: list[str], commit: str) -> Callable[[Any], None] | None:
        self.commits.append((list(models), commit))
        if self.mode == "unreadable":
            return None
        if self.mode == "fails":
            return lambda conn: conn.execute("SELECT * FROM no_such_table")
        write = partial(write_rows, rows=[_row(model, commit) for model in models], gone=[])

        def run(conn: Any) -> None:
            found = conn.execute("SELECT head FROM models_repo_head WHERE id").fetchone()
            self.seen_head.append(None if found is None else found[0])
            write(conn)

        return run

    def refresh_locked(
        self, models: list[str], *, path: Literal["commit", "no_commit", "no_git"]
    ) -> None:
        assert self.history is not None
        held = self.history._lock.locked() and (
            self.history.coordinator is None or self.history._hold is not None
        )
        self.refreshes.append((list(models), path, held))


@pytest.fixture
def conninfo(pg_conninfo: str) -> str:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate(conn)
    return pg_conninfo


def _history(root: Path, conninfo: str | None, hook: FakeHook | None) -> ModelHistory:
    history = ModelHistory(
        root,
        wrapper_prefix=WRAPPER_PREFIX,
        timeout=10.0,
        coordinator=None if conninfo is None else PgRepoCoordinator(conninfo),
    )
    root.mkdir(parents=True, exist_ok=True)
    history.ensure_repo()
    if hook is not None:
        hook.history = history
        history.template_rows = hook
    return history


def _write(root: Path, slug: str, text: str = "cube(1);") -> None:
    (root / slug).mkdir(parents=True, exist_ok=True)
    (root / slug / "model.scad").write_text(text)


def _head(conninfo: str) -> str | None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        found = conn.execute("SELECT head FROM models_repo_head WHERE id").fetchone()
    return None if found is None else str(found[0])


def _version(conninfo: str, template_id: str) -> str | None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        found = conn.execute(
            "SELECT version FROM templates WHERE template_id = %s", (template_id,)
        ).fetchone()
    return None if found is None else str(found[0])


def _heads_announced(conn: psycopg.Connection[Any], timeout: float) -> list[str]:
    return [
        decode_change(notice.payload).commit
        for notice in conn.notifies(timeout=timeout, stop_after=None)
    ]


def _seeded(tmp_path: Path, conninfo: str, hook: FakeHook) -> tuple[ModelHistory, str]:
    """A repository whose first commit (``other``) was made before the hook was set."""
    root = tmp_path / "models"
    history = _history(root, conninfo, None)
    _write(root, "other")
    first = history.commit("Add other", "other")
    assert first is not None
    hook.history = history
    history.template_rows = hook
    return history, first


def test_rows_commit_with_the_head_and_before_the_notify(tmp_path: Path, conninfo: str) -> None:
    hook = FakeHook()
    history, _ = _seeded(tmp_path, conninfo, hook)
    _write(history.root, "demo")
    seen: dict[str, str | None] = {}
    with psycopg.connect(conninfo, autocommit=True) as listener:
        listener.execute(f"LISTEN {HEAD_CHANNEL}")
        created: list[str | None] = []
        worker = threading.Thread(target=lambda: created.append(history.commit("Add demo", "demo")))
        worker.start()
        heard = listener.notifies(timeout=10.0)
        notice = next(heard)
        # The generator holds the connection's lock until it is closed.
        heard.close()
        # Read at once on the listening connection: the rows must already be there.
        found = listener.execute(
            "SELECT version FROM templates WHERE template_id = 'demo'"
        ).fetchone()
        seen[decode_change(notice.payload).commit] = None if found is None else found[0]
        worker.join()
    assert created and created[0] is not None
    commit = created[0]
    assert seen == {commit: commit}
    assert _head(conninfo) == commit
    assert hook.commits == [(["demo"], commit)]
    # The rows run after the HEAD upsert, in its transaction.
    assert hook.seen_head == [commit]


def test_a_failing_row_write_leaves_neither_head_nor_rows_and_the_commit_stands(
    tmp_path: Path, conninfo: str
) -> None:
    hook = FakeHook(mode="fails")
    history, first = _seeded(tmp_path, conninfo, hook)
    _write(history.root, "demo")
    with psycopg.connect(conninfo, autocommit=True) as listener:
        listener.execute(f"LISTEN {HEAD_CHANNEL}")
        created = history.commit("Add demo", "demo")
        announced = _heads_announced(listener, 0.5)
    assert created is not None and created != first
    assert history.head() == created
    assert _head(conninfo) == first
    assert _version(conninfo, "demo") is None
    assert created not in announced


def test_a_hook_that_cannot_read_still_records_the_head(tmp_path: Path, conninfo: str) -> None:
    hook = FakeHook(mode="unreadable")
    history, _ = _seeded(tmp_path, conninfo, hook)
    _write(history.root, "demo")
    created = history.commit("Add demo", "demo")
    assert created is not None
    assert _head(conninfo) == created
    assert _version(conninfo, "demo") is None
    assert hook.commits == [(["demo"], created)]


def test_a_commit_with_nothing_staged_refreshes_its_ids_under_the_lock(
    tmp_path: Path, conninfo: str
) -> None:
    hook = FakeHook()
    history, _ = _seeded(tmp_path, conninfo, hook)
    assert history.commit("Touch other", "other") is None
    assert hook.refreshes == [(["other"], "no_commit", True)]
    assert hook.commits == []


def test_a_failed_commit_refreshes_its_ids_then_raises(
    tmp_path: Path, conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = FakeHook()
    history, _ = _seeded(tmp_path, conninfo, hook)
    _write(history.root, "demo", "sphere(1);")

    def refuse(*_args: object, **_kwargs: object) -> str | None:
        raise GitError("git commit failed")

    monkeypatch.setattr(history, "_commit_locked", refuse)
    with pytest.raises(GitError):
        history.commit("Add demo", "demo", "_builtin/tag")
    assert hook.refreshes == [(["demo", "builtin:tag"], "no_commit", True)]


def test_an_identical_restore_refreshes_its_id(tmp_path: Path, conninfo: str) -> None:
    hook = FakeHook()
    history, first = _seeded(tmp_path, conninfo, hook)
    answered = history.restore("other", first)
    assert answered == first
    assert hook.refreshes == [(["other"], "no_commit", True)]


def test_the_builtin_dir_alone_refreshes_nothing(tmp_path: Path, conninfo: str) -> None:
    hook = FakeHook()
    history, _ = _seeded(tmp_path, conninfo, hook)
    assert history.commit("Sync the built-in models", "_builtin") is None
    assert hook.refreshes == []


def test_without_a_coordinator_the_rows_follow_the_commit(tmp_path: Path) -> None:
    hook = FakeHook()
    root = tmp_path / "models"
    history = _history(root, None, hook)
    _write(root, "demo")
    created = history.commit("Add demo", "demo")
    assert created is not None
    assert hook.refreshes == [(["demo"], "commit", True)]
    assert hook.commits == []


def test_last_commits_since_names_only_the_later_commits(tmp_path: Path) -> None:
    root = tmp_path / "models"
    history = _history(root, None, None)
    _write(root, "demo")
    _write(root, "early")
    base = history.commit("Add demo and early", "demo", "early")
    assert base is not None
    _write(root, "other")
    second = history.commit("Add other", "other")
    _write(root, "demo", "sphere(2);")
    third = history.commit("Edit demo", "demo")
    _write(root / "_builtin", "tag")
    fourth = history.commit("Sync", "_builtin")
    assert None not in (second, third, fourth)
    ids: Sequence[str] = ["demo", "other", "early", "absent", "builtin:tag"]
    assert history.last_commits_since(base, ids) == {
        "demo": third,
        "other": second,
        "builtin:tag": fourth,
    }
    assert history.last_commits_since(fourth or "", ids) == {}
