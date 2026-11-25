"""The second models repository (#7314 step 3): what the builder makes of the old one, and
that the check sees each kind of damage."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

import psycopg
import pytest

from scadbuddy.core.gitrepo import LOCK_NAME, GitError, git_env
from scadbuddy.core.settings import Settings
from scadbuddy.git_remote.push import RemoteTarget
from scadbuddy.library import models_next
from scadbuddy.library.history import ModelHistory, RevisionNotFoundError
from scadbuddy.library.models_next import (
    MAP_NAME,
    TRIAL_BRANCH,
    Pin,
    RefusedError,
    build,
    check,
    derived_map,
    push_trial,
    read_pins,
)
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.library.upstream import Upstream, state_of
from scadbuddy.render.solids import WRAPPER_PREFIX

pytestmark = [
    pytest.mark.requires_git,
    pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs is not on PATH"),
]

MESH = "meshes/o1-p1.3mf"
ONE = bytes(range(256)) * 64
TWO = ONE[::-1]
GONE = "0" * 39 + "1"
#: A library's commit, which is of another repository.
LIBRARY = "a" * 40
SECRET = "trial-token-1234567890"


def git(root: Path, *args: str, env: dict[str, str] | None = None, stdin: bytes = b"") -> bytes:
    filters = ModelHistory(root)._filters()
    command = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root)]
    command += [part for item in filters for part in ("-c", item)]
    done = subprocess.run(
        [*command, *args], check=False, capture_output=True, env=env or git_env(), input=stdin
    )
    assert done.returncode == 0, done.stderr.decode()
    return done.stdout


class Old:
    """A plain models repository, as prod's is: each save is one commit, each an hour
    after the last."""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.root = data / "models"
        self.history = ModelHistory(self.root, wrapper_prefix=WRAPPER_PREFIX)
        self.history._gitattributes_body = lambda: None  # type: ignore[method-assign]
        self.history.ensure_repo()
        self.clock = 1_700_000_000

    def write(self, path: str, files: dict[str, bytes | str]) -> None:
        for name, body in files.items():
            target = self.root / path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body if isinstance(body, bytes) else body.encode())

    def commit(self, message: str = "Save") -> str:
        self.clock += 3600
        env = {
            **git_env(),
            "GIT_AUTHOR_NAME": "Élan",
            "GIT_AUTHOR_EMAIL": "elan@example.test",
            "GIT_AUTHOR_DATE": f"@{self.clock} +0200",
            "GIT_COMMITTER_DATE": f"@{self.clock + 60} -0500",
        }
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message, env=env)
        return git(self.root, "rev-parse", "HEAD").decode().strip()

    def save(self, path: str, message: str = "Save", **files: bytes | str) -> str:
        names = {"scad": "model.scad", "meta": "model.json", "mesh": MESH}
        self.write(path, {names[name]: body for name, body in files.items()})
        return self.commit(message)

    def delete(self, path: str) -> str:
        shutil.rmtree(self.root / path)
        return self.commit(f"Delete {path}")


@pytest.fixture
def old(tmp_path: Path) -> Old:
    return Old(tmp_path / "data")


@pytest.fixture
def dest(old: Old) -> Path:
    return old.data / "models-next"


def made(old: Old, dest: Path, *pins: tuple[str, str]) -> ModelHistory:
    build(old.root, dest, [Pin(*pin) for pin in pins], live=old.root)
    return ModelHistory(dest, wrapper_prefix=WRAPPER_PREFIX)


def problems(old: Old, dest: Path, *pins: tuple[str, str]) -> list[str]:
    return check(old.root, dest, [Pin(*pin) for pin in pins]).differences


def ids(dest: Path) -> dict[tuple[str, str], str]:
    held = json.loads((dest / ".git" / MAP_NAME).read_text())
    return {(entry["slug"], entry["old"]): entry["new"] for entry in held["map"]}


def meta(upstream: Mapping[str, object] | None = None) -> str:
    body: dict[str, object] = {"name": "A", "libraries": [{"name": "l", "commit": LIBRARY}]}
    if upstream is not None:
        body["upstream"] = upstream
    return json.dumps(body, indent=2) + "\n"


def state(root: Path) -> dict[str, tuple[str, int, int]]:
    """Every file under ``root``, ``.git`` with its index and HEAD included."""
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            found[str(path.relative_to(root))] = (digest, stat.st_mode, stat.st_mtime_ns)
    return found


def subjects(root: Path, *paths: str) -> list[str]:
    return git(root, "log", "--reverse", "--format=%s", "--", *paths).decode().splitlines()


# ── the builder ───────────────────────────────────────────────────────────────


def test_each_version_is_a_commit_oldest_first(old: Old, dest: Path) -> None:
    first = old.save("a", "one", scad="cube(1);\n")
    second = old.save("a", "two", scad="cube(2);\n")
    third = old.save("a", "three", scad="cube(3);\n")
    other = old.save("b", "b one", scad="sphere();\n")
    last = old.save("a", "four", scad="cube(4);\n")
    new = made(old, dest, ("a", third), ("a", first))
    assert subjects(dest) == ["Initial revision", "one", "two", "three", "b one", "four"]
    found = ids(dest)
    assert set(found) == {("a", i) for i in (first, second, third, last)} | {("b", other)}
    assert new.show(found["a", first], "a/model.scad") == b"cube(1);\n"
    assert new.show(found["a", third], "a/model.scad") == b"cube(3);\n"
    assert new.last_commit("a") == found["a", last] and new.head() == found["a", last]
    assert all(new.touched(commit, slug) for (slug, _), commit in found.items())
    assert problems(old, dest, ("a", third), ("a", first)) == []


def test_the_author_date_and_message_are_kept(old: Old, dest: Path) -> None:
    message = "Subject\n\nA body.\n\nOn two.\n\nScadBuddy-Session: s1\n"
    first = old.save("a", message, scad="cube(1);\n")
    bare = old.save("a", "Subject only", scad="cube(2);\n")
    made(old, dest, ("a", first))
    found = ids(dest)
    for commit in (first, bare):
        args = ("show", "-s", "--format=%an|%ae|%ad|%cn|%ce|%cd", "--date=raw")
        assert git(dest, *args, found["a", commit]) == git(old.root, *args, commit)
        body = git(old.root, "show", "-s", "--format=%B", commit).decode().strip()
        now = git(dest, "show", "-s", "--format=%B", found["a", commit]).decode().strip()
        gap = "\n" if commit == first else "\n\n"
        assert now == f"{body}{gap}Imported-From: {commit}"
    trailers = git(dest, "show", "-s", "--format=%(trailers)", found["a", first]).decode()
    assert trailers.splitlines()[:2] == ["ScadBuddy-Session: s1", f"Imported-From: {first}"]


def test_a_pin_that_is_the_current_version_is_one_commit(old: Old, dest: Path) -> None:
    commit = old.save("a", scad="cube();\n")
    made(old, dest, ("a", commit))
    assert list(ids(dest)) == [("a", commit)]
    assert subjects(dest, "a") == ["Save"]


def test_one_tree_one_after_the_other_is_one_commit(old: Old, dest: Path) -> None:
    """A commit that changed nothing would be no revision of the template."""
    first = old.save("a", "one", scad="cube(1);\n")
    other = old.save("b", "other", scad="sphere();\n")
    old.save("a", "two", scad="cube(2);\n")
    back = old.save("a", "back", scad="cube(1);\n")
    made(old, dest, ("a", other))
    found = ids(dest)
    assert found["a", first] == found["a", other] != found["a", back]
    assert subjects(dest, "a") == ["one", "two", "back"]
    assert problems(old, dest, ("a", other)) == []


def test_a_pin_at_a_commit_of_another_template(old: Old, dest: Path) -> None:
    first = old.save("a", scad="cube(1);\n")
    other = old.save("b", scad="sphere();\n")
    made(old, dest, ("a", other))
    found = ids(dest)
    assert found["a", other] == found["a", first] != found["b", other]


def test_a_file_that_moves_to_another_template_is_a_change_of_both(old: Old, dest: Path) -> None:
    """The commit that takes the file away is that template's newest one."""
    old.save("a", scad="cube();\n", mesh=ONE)
    old.save("b", scad="sphere();\n")
    old.write("b", {MESH: ONE})
    (old.root / "a" / MESH).unlink()
    moved = old.commit("Recover uncommitted changes")
    made(old, dest)
    assert {slug for slug, commit in ids(dest) if commit == moved} == {"a", "b"}
    assert problems(old, dest) == []


def test_a_pin_of_a_deleted_template_resolves_and_is_not_current(old: Old, dest: Path) -> None:
    pinned = old.save("a", "one", scad="cube(1);\n", mesh=ONE)
    old.save("a", "two", scad="cube(2);\n")
    old.save("b", scad="sphere();\n")
    gone = old.delete("a")
    new = made(old, dest, ("a", pinned))
    commit = ids(dest)["a", pinned]
    assert new.show(commit, f"a/{MESH}") == ONE
    assert subjects(dest, "a") == ["one", "two", "Delete a"]
    assert ("a", gone) not in ids(dest)
    assert not (dest / "a").exists()
    with pytest.raises(RevisionNotFoundError):
        new.show("HEAD", "a/model.scad")
    assert problems(old, dest, ("a", pinned)) == []


def test_a_template_made_again_after_its_delete(old: Old, dest: Path) -> None:
    first = old.save("a", "one", scad="cube(1);\n", mesh=ONE)
    gone = old.delete("a")
    again = old.save("a", "again", scad="cube(1);\n", mesh=ONE)
    new = made(old, dest)
    found = ids(dest)
    assert subjects(dest, "a") == ["one", "Delete a", "again"]
    assert new.last_commit("a") == found["a", again] != found["a", first]
    assert ("a", gone) not in found
    assert problems(old, dest) == []


def test_a_commit_of_several_templates_is_one_of_each(old: Old, dest: Path) -> None:
    old.save("c", scad="cube();\n")
    for path in ("a", "b", "_builtin/d"):
        old.write(path, {"model.scad": f"cube({len(path)});\n"})
    shutil.rmtree(old.root / "c")
    both = old.commit("Recover uncommitted changes")
    new = made(old, dest)
    found = ids(dest)
    assert {slug for slug, commit in found if commit == both} == {"a", "b", "builtin:d"}
    for slug, path in (("a", "a"), ("b", "b"), ("builtin:d", "_builtin/d")):
        assert new.last_commit(path) == found[slug, both]
        assert subjects(dest, path) == ["Recover uncommitted changes"]
    assert subjects(dest, "c") == ["Save", "Recover uncommitted changes"]
    assert problems(old, dest) == []


def test_no_version_of_the_old_one_is_lost(old: Old, dest: Path) -> None:
    for at in range(4):
        old.save("a", scad=f"cube({at});\n", mesh=bytes([at]) * 4096)
        old.save("_builtin/b", scad=f"sphere({at});\n")
    old.delete("a")
    log = ("log", "--format=%H", "--reverse", "HEAD", "--")
    made(old, dest)
    found = ids(dest)
    for slug, path in (("a", "a"), ("builtin:b", "_builtin/b")):
        commits = git(old.root, *log, path).decode().split()
        kept = [c for c in commits if git(old.root, "ls-tree", c, path)]
        assert {c for s, c in found if s == slug} == set(kept)
    # A lost one fails the check, though nothing pins it.
    plan = models_next._plan

    def short(objects: models_next._Objects, pins: Sequence[Pin]) -> models_next._Plan:
        made = plan(objects, pins)
        made.versions = [v for v in made.versions if v.source != log_first]
        return made

    log_first = git(old.root, *log, "a").decode().split()[0]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(models_next, "_plan", short)
        build(old.root, dest, [], live=old.root)
    assert any(log_first in line for line in problems(old, dest))


def test_each_file_keeps_its_time(old: Old, dest: Path) -> None:
    old.save("a", scad="cube();\n", mesh=ONE, meta=meta(None))
    (old.root / "a/link").symlink_to("model.scad")
    old.commit("link")
    for at, name in enumerate(("model.scad", "model.json", MESH)):
        os.utime(old.root / "a" / name, (1_600_000_000 + at, 1_600_000_000 + at))
    new = made(old, dest)
    # The index has the new times: nothing is read again to say it is clean.
    left = git(dest, "-c", "filter.lfs.clean=false", "diff-files", "--name-only")
    assert left == b""
    for name in ("model.scad", "model.json", MESH, "link"):
        was, now = (old.root / "a" / name).lstat(), (dest / "a" / name).lstat()
        assert now.st_mtime_ns == was.st_mtime_ns
    assert new.clean(".")
    assert problems(old, dest) == []


@pytest.mark.parametrize("kind", ["no commit", "no template", "no template there"])
def test_a_pin_that_does_not_resolve_is_reported(old: Old, dest: Path, kind: str) -> None:
    first = old.save("a", scad="cube(1);\n")
    old.save("b", scad="cube(1);\n")
    pin = Pin(*{"no commit": ("a", GONE), "no template": ("z", first)}.get(kind, ("b", first)))
    report = build(old.root, dest, [pin, Pin("a", first)], live=old.root)
    assert report.unresolved == [pin]
    assert ("a", first) in ids(dest)
    result = check(old.root, dest, [pin, Pin("a", first)])
    assert result.differences == []
    assert any(pin.commit in note for note in result.notes)


def test_a_commit_head_does_not_reach_still_resolves(old: Old, dest: Path) -> None:
    old.save("a", "one", scad="cube(1);\n")
    lost = old.save("a", "lost", scad="cube(2);\n")
    git(old.root, "reset", "-q", "--hard", "HEAD~1")
    new = made(old, dest, ("a", lost))
    assert new.show(ids(dest)["a", lost], "a/model.scad") == b"cube(2);\n"
    assert new.show("HEAD", "a/model.scad") == b"cube(1);\n"
    assert problems(old, dest, ("a", lost)) == []


def test_a_lost_commit_of_a_template_that_is_not_current(old: Old, dest: Path) -> None:
    old.save("b", scad="cube(1);\n")
    lost = old.save("a", "lost", scad="cube(2);\n")
    git(old.root, "reset", "-q", "--hard", "HEAD~1")
    new = made(old, dest, ("a", lost))
    assert new.show(ids(dest)["a", lost], "a/model.scad") == b"cube(2);\n"
    assert not (dest / "a").exists()
    assert problems(old, dest, ("a", lost)) == []


def test_a_built_in_and_its_pin(old: Old, dest: Path) -> None:
    old.write("_builtin/c", {"model.scad": "cube(3);\n"})
    first = old.save("_builtin/b", "Sync", scad="cube(1);\n", mesh=ONE)
    last = old.save("_builtin/b", "Sync again", scad="cube(2);\n")
    new = made(old, dest, ("builtin:b", first))
    found = ids(dest)
    assert set(found) == {("builtin:b", first), ("builtin:c", first), ("builtin:b", last)}
    assert new.show(found["builtin:b", first], "_builtin/b/model.scad") == b"cube(1);\n"
    assert new.show("HEAD", f"_builtin/b/{MESH}") == ONE
    assert problems(old, dest, ("builtin:b", first)) == []


def test_an_upstream_is_the_new_id_and_a_library_pin_stays(old: Old, dest: Path) -> None:
    """The built-in is older than its duplicate, so it is committed before it."""
    base = old.save("_builtin/b", "Sync", scad="cube(1);\n")
    link = {"id": "builtin:b", "path": "_builtin/b", "base": base, "dismissed": None}
    copy = old.save("a", "Duplicate", scad="cube(1);\n", meta=meta(link))
    new = made(old, dest)
    found = ids(dest)
    held = json.loads((dest / "a/model.json").read_bytes())
    assert held["upstream"] == {**link, "base": found["builtin:b", base]}
    assert held["libraries"] == [{"name": "l", "commit": LIBRARY}]
    assert new.show(found["a", copy], "a/model.json") == (dest / "a/model.json").read_bytes()
    upstream = Upstream.model_validate(held["upstream"])
    revision = new.last_commit("_builtin/b")
    assert state_of(upstream, exists=True, revision=revision) == "current"
    assert problems(old, dest) == []


def test_a_dismissed_update_stays_dismissed(old: Old, dest: Path) -> None:
    base = old.save("b", "one", scad="cube(1);\n")
    dismissed = old.save("b", "two", scad="cube(2);\n")
    link = {"id": "b", "path": "b", "base": base, "dismissed": dismissed}
    old.save("a", scad="cube(1);\n", meta=meta(link))
    old.save("b", "three", scad="cube(3);\n")
    report = build(old.root, dest, [], live=old.root)
    held = json.loads((dest / "a/model.json").read_bytes())["upstream"]
    # Nothing else pins them: a duplicate keeps what it merges from.
    assert held["base"] == ids(dest)["b", base]
    assert held["dismissed"] == ids(dest)["b", dismissed]
    assert report.unmapped == []
    assert problems(old, dest) == []


def test_the_base_of_a_kept_base_is_kept(old: Old, dest: Path) -> None:
    first = old.save("c", scad="cube(1);\n")
    second = old.save("b", scad="cube(2);\n", meta=meta({"id": "c", "path": "c", "base": first}))
    old.save("c", "later", scad="cube(9);\n")
    old.save("a", scad="cube(3);\n", meta=meta({"id": "b", "path": "b", "base": second}))
    old.save("b", "later", scad="cube(8);\n", meta=meta(None))
    new = made(old, dest)
    found = ids(dest)
    held = json.loads(new.show(found["b", second], "b/model.json"))
    assert held["upstream"]["base"] == found["c", first]
    assert problems(old, dest) == []


def test_a_base_inside_another_template_is_not_a_template(old: Old, dest: Path) -> None:
    inner = old.save("b", scad="cube(1);\n", mesh=ONE)
    link = {"id": "b/meshes", "path": "b/meshes", "base": inner, "dismissed": inner}
    old.save("a", scad="cube(1);\n", meta=meta(link))
    made(old, dest)
    assert {slug for slug, _ in ids(dest)} == {"a", "b"}
    assert json.loads((dest / "a/model.json").read_bytes())["upstream"]["dismissed"] == inner
    assert problems(old, dest) == []


def test_a_base_the_old_one_does_not_have_stays(old: Old, dest: Path) -> None:
    old.save("b", scad="cube(1);\n")
    old.save("a", scad="cube(1);\n", meta=meta({"id": "b", "path": "b", "base": GONE}))
    report = build(old.root, dest, [], live=old.root)
    assert json.loads((dest / "a/model.json").read_bytes())["upstream"]["base"] == GONE
    assert len(report.unmapped) == 1
    assert GONE in report.unmapped[0] and "a at" in report.unmapped[0]
    assert report.unresolved == []
    assert problems(old, dest) == []


def test_a_base_where_the_source_lived_before(old: Old, dest: Path) -> None:
    """A seeded template's base is its own old revision, at its own path (#155)."""
    base = old.save("a", "Seed", scad="cube(1);\n")
    old.save("_builtin/a", "Sync", scad="cube(2);\n")
    link = {"id": "builtin:a", "path": "a", "base": base, "dismissed": None}
    old.save("a", scad="cube(1);\n", meta=meta(link))
    made(old, dest, ("a", base))
    held = json.loads((dest / "a/model.json").read_bytes())["upstream"]
    assert held["base"] == ids(dest)["a", base]
    assert problems(old, dest, ("a", base)) == []


def test_a_pinned_older_version_names_the_new_ids_too(old: Old, dest: Path) -> None:
    base = old.save("b", scad="cube(1);\n")
    link = {"id": "b", "path": "b", "base": base, "dismissed": None}
    pinned = old.save("a", "one", scad="cube(1);\n", meta=meta(link))
    old.save("a", "two", scad="cube(2);\n")
    new = made(old, dest, ("a", pinned))
    found = ids(dest)
    held = json.loads(new.show(found["a", pinned], "a/model.json"))
    assert held["upstream"]["base"] == found["b", base]


@pytest.mark.parametrize("body", ["{ not json", "[]", '{"upstream": 1}', '{"upstream": {}}'])
def test_a_model_json_without_an_upstream_is_kept(old: Old, dest: Path, body: str) -> None:
    old.save("a", scad="cube();\n", meta=body)
    made(old, dest)
    assert (dest / "a/model.json").read_text() == body
    assert problems(old, dest) == []


def test_meshes_are_in_lfs_from_the_first_commit(old: Old, dest: Path, tmp_path: Path) -> None:
    first = old.save("a", scad="cube();\n", mesh=ONE)
    old.save("a", mesh=TWO)
    old.save("_builtin/b", scad="cube();\n", mesh=b"")
    new = made(old, dest, ("a", first))
    root = git(dest, "rev-list", "--max-parents=0", "HEAD").decode().strip()
    assert git(dest, "ls-tree", "--name-only", root).split() == [b".gitattributes", b".gitignore"]
    commit = ids(dest)["a", first]
    pointer = git(dest, "cat-file", "blob", f"{commit}:a/{MESH}")
    assert len(pointer) < 200 and hashlib.sha256(ONE).hexdigest().encode() in pointer
    assert (dest / f"a/{MESH}").read_bytes() == TWO
    assert (dest / f"_builtin/b/{MESH}").read_bytes() == b""
    new.export("a", commit, tmp_path / "out")
    assert (tmp_path / "out" / MESH).read_bytes() == ONE
    # What git-lfs itself and the app's own boot make of it.
    git(dest, "lfs", "fsck")
    git(dest, "add", "--renormalize", ".")
    assert new.ensure_repo() is None
    assert problems(old, dest, ("a", first)) == []


def test_an_old_one_that_has_lfs_is_read_too(old: Old, dest: Path) -> None:
    """One made by #7423 on a fresh volume: its pointers are resolved."""
    del old.history._gitattributes_body  # the app's own again
    shutil.rmtree(old.root)
    old.history.ensure_repo()
    old.save("a", scad="cube();\n", mesh=ONE)
    made(old, dest)
    assert (dest / f"a/{MESH}").read_bytes() == ONE
    assert problems(old, dest) == []


def test_videos_are_copied_and_renders_wrappers_and_locks_are_not(old: Old, dest: Path) -> None:
    old.save("a", scad="cube();\n")
    old.save("_builtin/b", scad="cube();\n")
    for path in ("a", "_builtin/b"):
        old.write(
            path,
            {
                "media/clip.mp4": ONE,
                "media/clip.webm": TWO,
                ".renders/x/out.3mf": ONE,
                f"{WRAPPER_PREFIX}1.scad": "x",
                f"{WRAPPER_PREFIX}asset_1.png": "x",
            },
        )
    (old.root / LOCK_NAME).touch()
    new = made(old, dest)
    for path in ("a", "_builtin/b"):
        names = {str(p.relative_to(dest / path)) for p in (dest / path).rglob("*") if p.is_file()}
        assert names == {"model.scad", "media/clip.mp4", "media/clip.webm"}
        assert (dest / path / "media/clip.mp4").read_bytes() == ONE
    assert not (dest / LOCK_NAME).exists()
    assert new.clean(".")
    assert problems(old, dest) == []


def test_a_second_run_over_a_copy_only_the_old_one_ignores(old: Old, dest: Path) -> None:
    (old.root / ".git/info").mkdir(exist_ok=True)
    (old.root / ".git/info/exclude").write_text("*.dat\n")
    old.save("a", scad="cube();\n")
    old.write("a", {"über.dat": ONE})
    made(old, dest)
    made(old, dest)
    assert (dest / "a/über.dat").read_bytes() == ONE
    (dest / "a/other.dat").write_bytes(TWO)
    with pytest.raises(RefusedError, match=r"other\.dat"):
        build(old.root, dest, [], live=old.root)


@pytest.mark.parametrize("name", ["media/clip.mp4", "über.dat"])
def test_a_copy_that_was_changed_is_not_replaced(old: Old, dest: Path, name: str) -> None:
    (old.root / ".git/info").mkdir(exist_ok=True)
    (old.root / ".git/info/exclude").write_text("*.dat\n")
    old.save("a", scad="cube();\n")
    old.write("a", {name: ONE})
    made(old, dest)
    (dest / "a" / name).write_bytes(TWO)
    with pytest.raises(RefusedError, match=Path(name).name):
        build(old.root, dest, [], live=old.root)
    (dest / "a" / name).unlink()
    made(old, dest)
    assert (dest / "a" / name).read_bytes() == ONE


def test_modes_links_and_odd_names(old: Old, dest: Path) -> None:
    odd = 'ui/é "quoted"\n\\name.js'
    old.write("a", {"model.scad": "cube();\n", "run.sh": "#!/bin/sh\n", odd: "x"})
    (old.root / "a/run.sh").chmod(0o755)
    (old.root / "a/link").symlink_to("model.scad")
    first = old.commit("one")
    # A commit that changes only such a name is still the template's last one.
    old.write("a", {odd: "y"})
    last = old.commit("two")
    new = made(old, dest)
    assert set(ids(dest)) == {("a", first), ("a", last)}
    assert os.access(dest / "a/run.sh", os.X_OK) and not os.access(dest / "a/model.scad", os.X_OK)
    assert (dest / "a/link").readlink() == Path("model.scad")
    assert (dest / "a" / odd).read_text() == "y"
    assert new.clean(".")
    assert problems(old, dest) == []


def test_the_old_repository_is_untouched(old: Old, dest: Path) -> None:
    first = old.save("a", scad="cube();\n", mesh=ONE)
    old.save("a", mesh=TWO)
    old.write("a", {"media/clip.mp4": ONE})
    # A file whose stat data the index no longer has: a plain `git status` would write it.
    os.utime(old.root / "a/model.scad", ns=(1, 1))
    before, head = state(old.root), old.history.head()
    made(old, dest, ("a", first))
    assert state(old.root) == before
    assert check(old.root, dest, [Pin("a", first)]).differences == []
    assert state(old.root) == before and old.history.head() == head
    assert {p.name for p in old.data.iterdir()} == {"models", "models-next"}


def test_a_second_run_is_as_a_fresh_one(old: Old, dest: Path) -> None:
    first = old.save("a", scad="cube(1);\n", mesh=ONE)
    old.write("a", {"media/clip.mp4": ONE})
    made(old, dest)
    (old.root / "a/media/clip.mp4").unlink()
    old.save("a", scad="cube(2);\n", mesh=TWO)
    old.save("b", scad="sphere();\n")
    again = made(old, dest, ("a", first))
    fresh = made(old, old.data / "fresh", ("a", first))
    assert again.head() == fresh.head()
    assert ids(dest) == ids(fresh.root)
    names = [{p for p in state(r) if not p.startswith(".git/")} for r in (dest, fresh.root)]
    assert names[0] == names[1]
    assert problems(old, dest, ("a", first)) == []
    assert {p.name for p in old.data.iterdir()} == {"models", "models-next", "fresh"}


def test_the_map_is_what_the_trailers_say(old: Old, dest: Path) -> None:
    first = old.save("a", scad="cube(1);\n")
    old.save("a", scad="cube(2);\n")
    pinned = old.save("b", scad="cube(2);\n")
    old.delete("b")
    made(old, dest, ("a", first), ("b", pinned))
    assert derived_map(dest) == ids(dest) and len(ids(dest)) == 3


# ── where it may write ────────────────────────────────────────────────────────


def _foreign(old: Old, dest: Path) -> Path:
    dest.mkdir()
    (dest / "file").write_text("mine")
    return dest


def _links_to_live(old: Old, dest: Path) -> Path:
    dest.symlink_to(old.root)
    return dest


def _committed_to(old: Old, dest: Path) -> Path:
    """A built one that something served from since."""
    new = made(old, dest)
    (dest / "a/model.scad").write_text("cube(9);\n")
    assert new.commit("Save", "a") is not None
    return dest


def _forged(old: Old, dest: Path) -> Path:
    (dest / ".git").mkdir(parents=True)
    (dest / ".git" / MAP_NAME).write_text("{}")
    return dest


def _written_to(old: Old, dest: Path) -> Path:
    """A built one that holds a file no run made."""
    made(old, dest)
    (dest / "a/notes.txt").write_text("mine")
    return dest


def _changed(old: Old, dest: Path) -> Path:
    made(old, dest)
    (dest / "a/model.scad").write_text("cube(9);\n")
    return dest


BAD: dict[str, Callable[[Old, Path], Path]] = {
    "the live one": lambda old, dest: old.root,
    "inside the live one": lambda old, dest: old.root / "next",
    "spelled another way": lambda old, dest: old.data / "models-next" / ".." / "models",
    "a link to the live one": _links_to_live,
    "holds the live one": lambda old, dest: old.data,
    "not empty": _foreign,
    "a file": lambda old, dest: old.root / "a" / "model.scad",
    "committed to since": _committed_to,
    "a file of its own": _written_to,
    "changed": _changed,
    "not whole": _forged,
    "no parent": lambda old, dest: dest / "deeper",
}


@pytest.mark.parametrize("kind", list(BAD))
def test_a_bad_destination_is_refused(old: Old, dest: Path, kind: str) -> None:
    old.save("a", scad="cube();\n")
    target = BAD[kind](old, dest)
    before = state(old.data)
    with pytest.raises(RefusedError):
        build(old.root, target, [], live=old.root)
    assert state(old.data) == before


def test_the_live_one_is_refused_whatever_the_old_one_is(old: Old, tmp_path: Path) -> None:
    other = Old(tmp_path / "other")
    other.save("a", scad="cube();\n")
    with pytest.raises(RefusedError):
        build(other.root, old.root / "next", [], live=old.root)
    with pytest.raises(RefusedError):
        build(other.root, other.root / "next", [], live=old.root)


def test_an_empty_directory_is_a_destination(old: Old, dest: Path) -> None:
    old.save("a", scad="cube();\n")
    dest.mkdir()
    made(old, dest)
    assert (dest / "a/model.scad").is_file()
    assert problems(old, dest) == []


def test_a_run_that_fails_leaves_the_earlier_output(
    old: Old, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old.save("a", scad="cube(1);\n")
    made(old, dest)
    before = state(dest)
    old.save("a", scad="cube(2);\n")

    def fails(*_: object) -> None:
        raise OSError("no space")

    monkeypatch.setattr(models_next, "_copy_data", fails)
    with pytest.raises(OSError, match="no space"):
        made(old, dest)
    assert state(dest) == before
    assert {p.name for p in old.data.iterdir()} == {"models", "models-next"}


def test_without_git_lfs_nothing_is_built(
    old: Old, dest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old.save("a", scad="cube();\n")
    monkeypatch.setattr("scadbuddy.library.history.GIT_LFS", "no-such-git-lfs")
    with pytest.raises(RefusedError, match="git-lfs"):
        build(old.root, dest, [], live=old.root)
    assert not dest.exists()


def test_without_an_old_repository_nothing_is_built(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    with pytest.raises(RefusedError):
        build(tmp_path / "models", tmp_path / "next", [], live=tmp_path / "models")
    assert not (tmp_path / "next").exists()


# ── the check ─────────────────────────────────────────────────────────────────

Whole = tuple[Old, Path, tuple[str, str]]


@pytest.fixture
def whole(old: Old, dest: Path) -> Whole:
    """A built one that the check passes, with a pin, a mesh, a video and an upstream."""
    base = old.save("_builtin/b", "Sync", scad="cube(1);\n", mesh=TWO)
    link = {"id": "builtin:b", "path": "_builtin/b", "base": base, "dismissed": None}
    pinned = old.save("a", "one", scad="cube(1);\n", mesh=ONE, meta=meta(link))
    old.save("a", "two", scad="cube(2);\n", mesh=TWO)
    old.write("a", {"media/clip.mp4": ONE})
    made(old, dest, ("a", pinned))
    assert problems(old, dest, ("a", pinned)) == []
    return old, dest, ("a", pinned)


def commit_to(dest: Path, change: Callable[[], object]) -> None:
    """A commit on the new history, taken as the one the builder made."""
    change()
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "Damage")
    _take_head(dest)


def _take_head(dest: Path) -> None:
    path = dest / ".git" / MAP_NAME
    held = json.loads(path.read_text())
    held["head"] = git(dest, "rev-parse", "HEAD").decode().strip()
    path.write_text(json.dumps(held))


def rewrite(dest: Path, old_bytes: bytes, new_bytes: bytes) -> None:
    """The whole new history with ``old_bytes`` another, and its map again."""
    stream = git(dest, "fast-export", "--all")
    assert old_bytes in stream and len(old_bytes) == len(new_bytes)
    git(dest, "fast-import", "--quiet", "--force", stdin=stream.replace(old_bytes, new_bytes))
    git(dest, "read-tree", "HEAD")
    path = dest / ".git" / MAP_NAME
    held = json.loads(path.read_text())
    found = derived_map(dest)
    held["map"] = [{**e, "new": found[e["slug"], e["old"]]} for e in held["map"]]
    path.write_text(json.dumps(held))
    _take_head(dest)


def lfs_object(dest: Path, data: bytes) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    return dest / ".git/lfs/objects" / digest[:2] / digest[2:4] / digest


def named(found: Iterable[str], *words: str) -> bool:
    return any(all(word in line for word in words) for line in found)


def test_a_changed_byte_on_disk(whole: Whole) -> None:
    old, dest, pin = whole
    (dest / "a/model.scad").write_text("cube(3);\n")
    assert named(problems(old, dest, pin), "a/model.scad", "volume", "bytes differ")


def test_a_changed_byte_in_the_current_version(whole: Whole) -> None:
    old, dest, pin = whole
    commit_to(dest, lambda: (dest / "a/model.scad").write_text("cube(3);\n"))
    assert named(problems(old, dest, pin), "a/model.scad", "HEAD", "bytes differ")


def test_a_changed_byte_in_a_pinned_version(whole: Whole) -> None:
    old, dest, pin = whole
    rewrite(dest, b"cube(1);\n", b"cube(7);\n")
    assert named(problems(old, dest, pin), "a/model.scad", pin[1], "bytes differ")


def test_a_missing_file(whole: Whole) -> None:
    old, dest, pin = whole
    commit_to(dest, (dest / "a/model.scad").unlink)
    found = problems(old, dest, pin)
    assert named(found, "a/model.scad", "HEAD", "missing")
    assert named(found, "a/model.scad", "volume", "missing")


def test_a_missing_template(whole: Whole) -> None:
    old, dest, pin = whole
    commit_to(dest, lambda: shutil.rmtree(dest / "a"))
    assert named(problems(old, dest, pin), "a/model.scad", "HEAD", "missing")


def test_a_missing_video(whole: Whole) -> None:
    old, dest, pin = whole
    (dest / "a/media/clip.mp4").unlink()
    assert named(problems(old, dest, pin), "a/media/clip.mp4", "volume", "missing")


def test_an_extra_file(whole: Whole) -> None:
    old, dest, pin = whole
    commit_to(dest, lambda: (dest / "a/extra.txt").write_text("x"))
    assert named(problems(old, dest, pin), "a/extra.txt", "HEAD", "not in the old")


def test_an_extra_template(whole: Whole) -> None:
    old, dest, pin = whole

    def extra() -> None:
        (dest / "z").mkdir()
        (dest / "z/model.scad").write_text("x")

    commit_to(dest, extra)
    assert named(problems(old, dest, pin), "z/model.scad", "HEAD", "not in the old")


def test_an_executable_bit(whole: Whole) -> None:
    old, dest, pin = whole
    commit_to(dest, lambda: (dest / "a/model.scad").chmod(0o755))
    assert named(problems(old, dest, pin), "a/model.scad", "HEAD", "mode")


def test_a_missing_lfs_object(whole: Whole) -> None:
    old, dest, pin = whole
    lfs_object(dest, ONE).unlink()
    digest = hashlib.sha256(ONE).hexdigest()
    assert named(problems(old, dest, pin), digest, "LFS object", "missing")


def test_a_changed_lfs_object(whole: Whole) -> None:
    old, dest, pin = whole
    target = lfs_object(dest, ONE)
    target.chmod(0o644)
    target.write_bytes(b"x" * len(ONE))
    digest = hashlib.sha256(ONE).hexdigest()
    assert named(problems(old, dest, pin), digest, "LFS object", "hash")


def test_a_mesh_committed_as_bytes(whole: Whole) -> None:
    """The bytes a reader gets are the right ones, so only the pointer check sees it."""
    old, dest, pin = whole
    oid = git(dest, "hash-object", "-w", "--no-filters", f"a/{MESH}").decode().strip()
    git(dest, "update-index", "--cacheinfo", f"100644,{oid},a/{MESH}")
    git(dest, "commit", "-q", "-m", "Damage")
    _take_head(dest)
    assert named(problems(old, dest, pin), f"a/{MESH}", "not an LFS pointer")


def test_a_large_blob(whole: Whole, monkeypatch: pytest.MonkeyPatch) -> None:
    old, dest, pin = whole
    git(dest, "hash-object", "-w", "--stdin", stdin=b"x" * 4096)
    monkeypatch.setattr(models_next, "MAX_BLOB_BYTES", 4095)
    assert named(problems(old, dest, pin), "4096", "over")


def test_an_upstream_id_that_is_wrong(whole: Whole) -> None:
    old, dest, pin = whole
    mapped = json.loads((dest / "a/model.json").read_bytes())["upstream"]["base"]
    text = (dest / "a/model.json").read_text().replace(mapped, "b" * 40)
    commit_to(dest, lambda: (dest / "a/model.json").write_text(text))
    assert named(problems(old, dest, pin), "a/model.json", "HEAD", "upstream")


def test_an_upstream_id_left_old(whole: Whole) -> None:
    old, dest, pin = whole
    text = (old.root / "a/model.json").read_text()
    commit_to(dest, lambda: (dest / "a/model.json").write_text(text))
    assert named(problems(old, dest, pin), "a/model.json", "HEAD", "upstream")


def test_a_model_json_changed_beside_its_upstream(whole: Whole) -> None:
    old, dest, pin = whole
    text = (dest / "a/model.json").read_text()
    commit_to(dest, lambda: (dest / "a/model.json").write_text(text + "\n"))
    assert named(problems(old, dest, pin), "a/model.json", "HEAD", "bytes differ")


def test_a_map_that_is_wrong(whole: Whole) -> None:
    old, dest, pin = whole
    path = dest / ".git" / MAP_NAME
    held = json.loads(path.read_text())
    held["map"][0]["new"] = "b" * 40
    path.write_text(json.dumps(held))
    assert named(problems(old, dest, pin), MAP_NAME, "trailers")


def test_a_commit_on_top(whole: Whole) -> None:
    old, dest, pin = whole
    git(dest, "commit", "-q", "--allow-empty", "-m", "More")
    assert named(problems(old, dest, pin), "HEAD", "builder")


def test_an_index_that_is_not_head(whole: Whole) -> None:
    old, dest, pin = whole
    git(dest, "rm", "-q", "--cached", "a/model.scad")
    assert named(problems(old, dest, pin), "a/model.scad", "not committed")


def test_a_broken_git_object(whole: Whole) -> None:
    old, dest, pin = whole
    oid = git(dest, "hash-object", "-w", "--stdin", stdin=b"spare").decode().strip()
    target = dest / ".git/objects" / oid[:2] / oid[2:]
    target.chmod(0o644)
    target.write_bytes(b"not zlib")
    assert named(problems(old, dest, pin), "git fsck")


def test_a_revision_made_since_the_build(whole: Whole) -> None:
    old, dest, pin = whole
    later = old.save("a", scad="cube(8);\n")
    assert named(problems(old, dest, pin), f"a at {later}", "no commit")


def test_an_uncommitted_change_in_the_old_one(whole: Whole) -> None:
    """What a failed commit left on the volume is not in the new one, and the check says."""
    old, dest, pin = whole
    old.write("a", {"model.scad": "cube(5);\n", "new.txt": "x"})
    found = problems(old, dest, pin)
    assert named(found, "a/model.scad", "volume", "bytes differ")
    assert named(found, "a/new.txt", "volume", "missing")


def test_no_new_repository(old: Old, dest: Path) -> None:
    old.save("a", scad="cube();\n")
    assert problems(old, dest) != []


# ── the pins, and the commands ────────────────────────────────────────────────


def seed(conninfo: str, pins: list[tuple[str, str, str]]) -> None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO orders (id, currency, created_by, source)"
            " VALUES ('o', 'USD', '{}', 'storefront')"
        )
        for place, slug, commit in pins:
            fields = {"slug": slug, "commit": commit}
            if place == "outputs":
                conn.execute(
                    "INSERT INTO outputs (output_id, slug, created_at, meta) VALUES (%(id)s,"
                    " %(slug)s, now(), jsonb_build_object('model_version', %(commit)s::text))",
                    {**fields, "id": uuid.uuid4().hex},
                )
                continue
            if place == "storefront_listings":
                conn.execute(
                    "INSERT INTO storefront_listings (slug, commit, title, position)"
                    " VALUES (%(slug)s, %(commit)s, 't', 1)",
                    fields,
                )
                continue
            row = conn.execute(
                "INSERT INTO order_items (order_id, position, quantity, title, kind, slug,"
                " commit, inputs, template_name, render_key) VALUES ('o', 1, 1, 't', 'model',"
                " %(slug)s, %(commit)s, '{}', 't', 'k') RETURNING id",
                fields,
            ).fetchone()
            assert row is not None
            if place == "order_item_revisions":
                conn.execute(
                    "INSERT INTO order_item_revisions (item_id, number, slug, commit, inputs,"
                    " template_name, render_key, created_by) VALUES (%(item)s, 1, %(slug)s,"
                    " %(commit)s, '{}', 't', 'k', '{}')",
                    {**fields, "commit": "e" * 40, "item": row[0]},
                )


@pytest.mark.requires_postgres
def test_the_pins_are_the_orders_the_listings_and_the_outputs(pg_conninfo: str) -> None:
    seed(
        pg_conninfo,
        [
            ("order_items", "a", "c" * 40),
            ("order_items", "a", "c" * 40),
            ("order_item_revisions", "a", "d" * 40),
            ("storefront_listings", "a", "c" * 40),
            ("storefront_listings", "builtin:b", "f" * 40),
            ("outputs", "a", "c" * 40),
            ("outputs", "g", "b" * 40),
            ("outputs", "g", "sha256:" + "b" * 64),
        ],
    )
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        # A custom item names no commit.
        conn.execute(
            "INSERT INTO order_items (order_id, position, quantity, title, kind,"
            " requires_printing) VALUES ('o', 2, 1, 't', 'custom', false)"
        )
    assert read_pins(pg_conninfo) == [
        Pin("a", "c" * 40, ("order_items", "outputs", "storefront_listings")),
        Pin("a", "d" * 40, ("order_items",)),
        Pin("a", "e" * 40, ("order_item_revisions",)),
        Pin("builtin:b", "f" * 40, ("storefront_listings",)),
        Pin("g", "b" * 40, ("outputs",)),
    ]


def test_the_trial_push_is_a_complete_copy(old: Old, dest: Path, tmp_path: Path) -> None:
    old.save("a", scad="cube();\n", mesh=ONE)
    new = made(old, dest)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare")
    target = RemoteTarget(url=f"file://{remote}")
    assert push_trial(dest, f"{TRIAL_BRANCH}-1", target, live=old.root) == new.head()
    refs = git(remote, "for-each-ref", "--format=%(refname) %(objectname)").decode().split()
    assert refs == [f"refs/heads/{TRIAL_BRANCH}-1", new.head()]
    stored = {p.name for p in (remote / "lfs/objects").rglob("*") if p.is_file()}
    assert stored == {hashlib.sha256(ONE).hexdigest()}


@pytest.mark.parametrize("branch", ["models", "flows", "main", "x/models-trial", "models-trial:x"])
def test_a_trial_push_to_another_branch_is_refused(old: Old, dest: Path, branch: str) -> None:
    old.save("a", scad="cube();\n")
    made(old, dest)
    with pytest.raises(RefusedError, match="branch"):
        push_trial(dest, branch, RemoteTarget(url="file:///nowhere"), live=old.root)


def test_a_trial_push_of_anything_else_is_refused(old: Old, dest: Path) -> None:
    old.save("a", scad="cube();\n")
    target = RemoteTarget(url="file:///nowhere")
    for root in (old.root, dest):
        with pytest.raises(RefusedError):
            push_trial(root, TRIAL_BRANCH, target, live=old.root)
    made(old, dest)
    (dest / "a/model.scad").write_text("cube(9);\n")
    with pytest.raises(RefusedError, match="holds changes"):
        push_trial(dest, TRIAL_BRANCH, target, live=old.root)


@pytest.mark.requires_postgres
def test_the_trial_push_is_to_the_stored_remote_and_shows_no_token(
    old: Old,
    dest: Path,
    pg_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old.save("a", scad="cube();\n")
    made(old, dest)
    monkeypatch.setenv("SCADBUDDY_DATABASE_URL", pg_conninfo)
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_ADDRESS", "127.0.0.1:1")
    monkeypatch.setenv("SCADBUDDY_DATA_DIR", str(old.data))
    store = SettingsStore(Settings())
    store.open()
    try:
        assert models_next.main(["push", "--branch", TRIAL_BRANCH]) == 2
        assert "no git remote" in capsys.readouterr().err
        url = "http://127.0.0.1:1/o/r"
        store.save(SettingsPatch(git_remote_url=url, git_remote_token=SECRET))
        seen: list[RemoteTarget] = []

        def refuse(_: Path, __: str, target: RemoteTarget, *, live: Path) -> str:
            seen.append(target)
            raise GitError(f"git push failed: {SECRET}", SECRET)

        monkeypatch.setattr(models_next, "push_trial", refuse)
        assert models_next.main(["push", "--branch", TRIAL_BRANCH]) == 2
    finally:
        store.close()
    assert [(t.url, t.token) for t in seen] == [(url, SECRET)]
    said = capsys.readouterr()
    assert "failed" in said.err and SECRET not in said.err + said.out


@pytest.mark.requires_postgres
def test_the_two_commands(
    old: Old,
    dest: Path,
    pg_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = old.save("a", scad="cube(1);\n", mesh=ONE)
    old.save("a", scad="cube(2);\n")
    seed(pg_conninfo, [("order_items", "a", first), ("storefront_listings", "a", GONE)])
    monkeypatch.setenv("SCADBUDDY_DATABASE_URL", pg_conninfo)
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_ADDRESS", "127.0.0.1:1")
    monkeypatch.setenv("SCADBUDDY_DATA_DIR", str(old.data))
    assert models_next.main(["build"]) == 0
    out = capsys.readouterr().out
    assert GONE in out and "storefront_listings" in out
    assert models_next.main(["check"]) == 0
    assert GONE in capsys.readouterr().out
    (dest / "a/model.scad").write_text("cube(3);\n")
    assert models_next.main(["check"]) == 1
    assert "a/model.scad" in capsys.readouterr().out
    assert models_next.main(["build", "--dest", str(old.root)]) == 2
    assert "refused" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        models_next.main(["switch"])
    assert models_next.main(["build"]) == 2
    assert "holds changes" in capsys.readouterr().err
    (dest / "a/model.scad").write_text("cube(2);\n")
    git(old.root, "update-index", "--add", "--cacheinfo", f"160000,{first},a/sub")
    git(old.root, "commit", "-q", "-m", "A submodule")
    assert models_next.main(["build"]) == 2
    assert "a/sub" in capsys.readouterr().err
