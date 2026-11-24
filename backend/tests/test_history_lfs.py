"""Meshes in Git LFS (#7314): a fresh models repository holds a pointer in git, every
reader gets the bytes, and a push takes the bytes too."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scadbuddy.core.gitrepo import PUSH_TIMEOUT, GitError, git_env
from scadbuddy.library import history as history_module
from scadbuddy.library.history import (
    LFS_PUSH_TIMEOUT,
    BlobTooLargeError,
    ModelHistory,
    lfs_endpoint,
)
from scadbuddy.library.upstream import Upstream, plan_merge
from scadbuddy.workflows.housekeeping import GIT_REMOTE_TIMEOUT

pytestmark = [
    pytest.mark.requires_git,
    pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs is not on PATH"),
]

MESH = "a/meshes/o1-p1.3mf"
ONE = bytes(range(256)) * 64
TWO = ONE[::-1]
TOKEN = "s3cret-token-7314"


@pytest.fixture
def history(tmp_path: Path) -> ModelHistory:
    made = ModelHistory(tmp_path / "models")
    made.ensure_repo()
    return made


def put(history: ModelHistory, data: bytes, path: str = MESH, source: str = "cube();\n") -> str:
    target = history.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    (target.parent.parent / "model.scad").write_text(source)
    commit = history.commit("Save", path.rsplit("/meshes/", 1)[0])
    assert commit is not None
    return commit


def blob(history: ModelHistory, commit: str, path: str = MESH) -> bytes:
    done = history._run("cat-file", "blob", f"{commit}:{path}", text=False)
    assert isinstance(done.stdout, bytes)
    return done.stdout


def bare(path: Path) -> Path:
    subprocess.run(["git", "init", "--bare", "-q", str(path)], check=True, env=git_env())
    return path


@pytest.mark.parametrize("path", [MESH, "_builtin/b/meshes/o1-p1.3mf"])
def test_git_holds_a_pointer_and_the_volume_the_mesh(history: ModelHistory, path: str) -> None:
    commit = put(history, ONE, path)
    pointer = blob(history, commit, path)
    assert hashlib.sha256(ONE).hexdigest().encode() in pointer and len(pointer) < 200
    assert (history.root / path).read_bytes() == ONE


def test_a_repository_with_a_commit_is_never_turned_into_an_lfs_one(tmp_path: Path) -> None:
    root = tmp_path / "models"
    old = ModelHistory(root)
    old._gitattributes_body = lambda: None  # type: ignore[method-assign]
    old.ensure_repo()
    first = put(old, ONE)
    again = ModelHistory(root)
    again.ensure_repo()
    second = put(again, TWO)
    assert not (root / ".gitattributes").exists()
    assert (blob(again, first), blob(again, second)) == (ONE, TWO)


def test_without_git_lfs_a_fresh_repository_is_a_plain_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(history_module, "GIT_LFS", "no-such-git-lfs")
    plain = ModelHistory(tmp_path / "models")
    plain.ensure_repo()
    assert not (plain.root / ".gitattributes").exists()


def test_without_its_filter_a_mesh_is_refused_and_never_a_pointer(
    history: ModelHistory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = put(history, ONE)
    monkeypatch.setattr(history_module, "GIT_LFS", "no-such-git-lfs")
    with pytest.raises(GitError):
        history.export("a", commit, tmp_path / "out")
    with pytest.raises(GitError):
        put(history, TWO)


def test_export_show_and_restore_give_the_bytes(history: ModelHistory, tmp_path: Path) -> None:
    first = put(history, ONE)
    put(history, TWO)
    history.export("a", first, tmp_path / "out")
    assert (tmp_path / "out/meshes/o1-p1.3mf").read_bytes() == ONE
    assert history.show(first, MESH) == ONE
    history.restore("a", first)
    assert (history.root / MESH).read_bytes() == ONE


def test_a_mesh_read_by_size_is_its_own_size_and_bytes(history: ModelHistory) -> None:
    """The editor's file route refuses a mesh as it is, and never shows its pointer."""
    commit = put(history, ONE)
    put(history, TWO)
    assert history.read_blob(commit, MESH, limit=len(ONE)) == ONE
    with pytest.raises(BlobTooLargeError):
        history.read_blob(commit, MESH, limit=len(ONE) - 1)


def test_a_lost_object_is_an_error_and_not_a_missing_revision(
    history: ModelHistory, tmp_path: Path
) -> None:
    first = put(history, ONE)
    put(history, TWO)
    digest = hashlib.sha256(ONE).hexdigest()
    next((history.root / ".git/lfs/objects").rglob(digest)).unlink()
    for read in (
        lambda: history.export("a", first, tmp_path / "out"),
        lambda: history.show(first, MESH),
    ):
        with pytest.raises(GitError) as caught:
            read()
        assert not isinstance(caught.value, history_module.RevisionNotFoundError)


def test_an_upstream_merge_takes_the_bytes(history: ModelHistory) -> None:
    base = put(history, ONE)
    (history.root / "copy/meshes").mkdir(parents=True)
    shutil.copy(history.root / MESH, history.root / "copy/meshes/o1-p1.3mf")
    shutil.copy(history.root / "a/model.scad", history.root / "copy/model.scad")
    history.commit("Duplicate", "copy")
    theirs = put(history, TWO)
    upstream = Upstream(id="a", path="a", base=base)
    plan = plan_merge(history, "copy", history.root / "copy", upstream, theirs)
    assert plan.files["meshes/o1-p1.3mf"] == TWO


def test_a_mesh_works_on_a_volume_another_user_owns(
    history: ModelHistory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The share's owner is not the pod's: git-lfs's own git calls take safe.directory."""
    real = git_env

    def foreign() -> dict[str, str]:
        return {**real(), "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}

    monkeypatch.setattr("scadbuddy.core.gitrepo.git_env", foreign)
    commit = put(history, ONE)
    history.export("a", commit, tmp_path / "out")
    assert (tmp_path / "out/meshes/o1-p1.3mf").read_bytes() == ONE


def test_a_push_is_a_complete_copy(history: ModelHistory, tmp_path: Path) -> None:
    put(history, ONE)
    commit = put(history, TWO)
    remote = bare(tmp_path / "remote.git")
    history.push(f"file://{remote}", commit, "refs/heads/models")
    stored = {p.name for p in (remote / "lfs/objects").rglob("*") if p.is_file()}
    assert stored == {hashlib.sha256(d).hexdigest() for d in (ONE, TWO)}


@pytest.mark.parametrize(
    ("url", "endpoint"),
    [
        ("https://h/o/r", "https://h/o/r.git/info/lfs"),
        ("https://h/o/r/", "https://h/o/r.git/info/lfs"),
        ("https://h/o/r.git", "https://h/o/r.git/info/lfs"),
        ("file:///srv/r.git", None),
        ("/srv/r.git", None),
    ],
)
def test_the_endpoint_is_the_remotes_own(url: str, endpoint: str | None) -> None:
    assert lfs_endpoint(url) == endpoint


class _Lfs(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]]
    stored: dict[str, bytes]

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append((self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") is None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Basic realm=git")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        base = f"http://127.0.0.1:{self.server.server_port}/put/"  # type: ignore[attr-defined]
        objects = [
            {**o, "authenticated": True, "actions": {"upload": {"href": base + o["oid"]}}}
            for o in body["objects"]
        ]
        answer = json.dumps({"transfer": "basic", "objects": objects}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.git-lfs+json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def do_PUT(self) -> None:
        data = self.rfile.read(int(self.headers["Content-Length"]))
        self.stored[self.path.rsplit("/", 1)[1]] = data
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return


def _serve() -> tuple[ThreadingHTTPServer, type[_Lfs]]:
    handler = type("Lfs", (_Lfs,), {"seen": [], "stored": {}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler


@pytest.fixture
def two_servers() -> Iterator[list[tuple[ThreadingHTTPServer, type[_Lfs]]]]:
    servers = [_serve(), _serve()]
    yield servers
    for server, _ in servers:
        server.shutdown()
        server.server_close()


def test_the_credential_goes_to_the_remote_only_and_in_no_argv(
    history: ModelHistory,
    two_servers: list[tuple[ThreadingHTTPServer, type[_Lfs]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `.lfsconfig` a commit carries cannot send the meshes or the token elsewhere."""
    (server, ours), (other, theirs) = two_servers
    evil = f"http://127.0.0.1:{other.server_address[1]}/evil"
    (history.root / ".lfsconfig").write_text(
        f"[lfs]\n\turl = {evil}\n\tpushurl = {evil}\n"
        f'[remote "origin"]\n\tlfsurl = {evil}\n\tlfspushurl = {evil}\n'
    )
    history.commit("Config", ".lfsconfig")
    commit = put(history, ONE)
    argvs: list[str] = []
    real = subprocess.run

    def spy(command: list[str], **kwargs: object) -> object:
        argvs.append(" ".join(command))
        return real(command, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("scadbuddy.core.gitrepo.subprocess.run", spy)
    url = f"http://127.0.0.1:{server.server_address[1]}/o/r"
    with pytest.raises(GitError):  # no git at this server, only LFS
        history.push(url, commit, "refs/heads/models", username="u", password=TOKEN)
    assert ours.stored == {hashlib.sha256(ONE).hexdigest(): ONE}
    assert {path for path, _ in ours.seen} == {"/o/r.git/info/lfs/objects/batch"}
    assert any(auth is not None for _, auth in ours.seen)
    assert theirs.seen == [] and theirs.stored == {}
    assert argvs and not any(TOKEN in argv for argv in argvs)
    lfs = next(i for i, argv in enumerate(argvs) if " lfs push " in argv)
    ref = next(i for i, argv in enumerate(argvs) if " push --quiet " in argv)
    assert lfs < ref


def test_the_sweep_outlasts_the_pushes_it_makes() -> None:
    assert timedelta(seconds=2 * PUSH_TIMEOUT + LFS_PUSH_TIMEOUT) < GIT_REMOTE_TIMEOUT
