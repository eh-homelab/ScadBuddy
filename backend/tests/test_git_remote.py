"""The off-box git remote (#4974): each commit of the model and flow history pushed after
it lands, never with the credential on a command line, and the lag recorded."""

from __future__ import annotations

import dataclasses
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.core.gitrepo import (
    CREDENTIAL_HELPER,
    PUSH_PASSWORD_ENV,
    PUSH_TIMEOUT,
    PUSH_USERNAME_ENV,
    GitError,
    GitRepo,
    GitTimeoutError,
    git_env,
)
from scadbuddy.core.settings import check_value
from scadbuddy.git_remote.push import (
    REFUSED_BACKOFF,
    SCRUBBED,
    PushError,
    RemotePush,
    RemoteTarget,
    explain,
    scrub,
)
from scadbuddy.git_remote.store import PushRecord, PushRecords
from scadbuddy.workflows.housekeeping import (
    GIT_REMOTE_SWEEP,
    GIT_REMOTE_TIMEOUT,
    PRUNE_SWEEPS,
    SWEEPS,
)
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_git

TOKEN = "glpat-s3cret-token-4974"


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    made = GitRepo(tmp_path / "repo")
    made.ensure_repo()
    return made


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(bare)], check=True, env=git_env(), timeout=30
    )
    return bare


def write_commit(repo: GitRepo, path: str, text: str) -> str:
    (repo.root / path).parent.mkdir(parents=True, exist_ok=True)
    (repo.root / path).write_text(text)
    commit = repo.commit(f"Write {path}", path)
    assert commit is not None
    return commit


def remote_ref(remote: Path, ref: str) -> str | None:
    completed = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        env=git_env(),
        timeout=30,
    )
    return completed.stdout.strip() or None


class MemoryRecords:
    """`PushRecords`' three calls, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, PushRecord] = {}

    def get(self, repository: str) -> PushRecord | None:
        return self.rows.get(repository)

    def pushed(self, repository: str, url: str, commit: str) -> None:
        now = _now()
        self.rows[repository] = PushRecord(repository, url, commit, now, now, None)

    def failed(
        self, repository: str, url: str, error: str, refused_commit: str | None = None
    ) -> None:
        old = self.rows.get(repository)
        kept = old if old is not None and old.url == url else None
        self.rows[repository] = PushRecord(
            repository,
            url,
            None if kept is None else kept.pushed_commit,
            None if kept is None else kept.pushed_at,
            _now(),
            error,
            refused_commit,
        )

    def age(self, repository: str, by: timedelta) -> None:
        """The last attempt ``by`` earlier than it was."""
        old = self.rows[repository]
        self.rows[repository] = dataclasses.replace(old, attempted_at=old.attempted_at - by)


def _now() -> datetime:
    return datetime.now(UTC)


def pusher(
    repo: GitRepo, records: Any, target: RemoteTarget | None, name: str = "models"
) -> RemotePush:
    return RemotePush({name: repo}, records, lambda: target)


# ── GitRepo.push ──────────────────────────────────────────────────────────────


def test_a_push_lands_the_commit_on_the_remote_branch(repo: GitRepo, remote: Path) -> None:
    commit = write_commit(repo, "a/f.txt", "one\n")
    repo.push(str(remote), commit, "refs/heads/models")
    assert remote_ref(remote, "refs/heads/models") == commit


def test_a_push_is_never_a_force(repo: GitRepo, remote: Path, tmp_path: Path) -> None:
    """A remote on another history refuses the push: the off-box copy is never rewritten."""
    other = GitRepo(tmp_path / "other")
    other.ensure_repo()
    elsewhere = write_commit(other, "b.txt", "elsewhere\n")
    other.push(str(remote), elsewhere, "refs/heads/models")
    commit = write_commit(repo, "a.txt", "one\n")
    with pytest.raises(GitError):
        repo.push(str(remote), commit, "refs/heads/models")
    assert remote_ref(remote, "refs/heads/models") == elsewhere


def test_the_credential_is_never_on_the_command_line(
    repo: GitRepo, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token reaches git through the child's environment only, for the helper."""
    commit = write_commit(repo, "a.txt", "one\n")
    calls: list[tuple[list[str], dict[str, str]]] = []
    real = subprocess.run

    def spy(command: list[str], **kwargs: Any) -> Any:
        calls.append((list(command), dict(kwargs.get("env") or {})))
        return real(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    repo.push(str(remote), commit, "refs/heads/models", username="me", password=TOKEN)
    [(command, env)] = [call for call in calls if "push" in call[0]]
    assert not any(TOKEN in part for part in command)
    assert f"credential.helper={CREDENTIAL_HELPER}" in command
    assert "protocol.allow=never" in command and "protocol.file.allow=always" in command
    assert (env[PUSH_USERNAME_ENV], env[PUSH_PASSWORD_ENV]) == ("me", TOKEN)
    # Hermetic as every call: no operator config reaches it.
    assert env["GIT_CONFIG_GLOBAL"] == env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert remote_ref(remote, "refs/heads/models") == commit


def test_only_the_remotes_own_protocol_is_allowed(repo: GitRepo, remote: Path) -> None:
    """An `ext::` URL (a command git would run) is refused, never run."""
    commit = write_commit(repo, "a.txt", "one\n")
    with pytest.raises(GitError):
        repo.push(f"ext::sh -c touch% {repo.root}/ran", commit, "refs/heads/models")
    assert not (repo.root / "ran").exists()


def test_the_credential_helper_answers_a_get_from_the_environment(repo: GitRepo) -> None:
    """The helper git runs prints the two variables for a `get`, and nothing else."""
    env = {**git_env(), PUSH_USERNAME_ENV: "me", PUSH_PASSWORD_ENV: TOKEN}
    completed = subprocess.run(
        [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper={CREDENTIAL_HELPER}",
            "credential",
            "fill",
        ],
        input="protocol=https\nhost=git.example\n\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=repo.root,
        timeout=30,
        check=True,
    )
    assert "username=me\n" in completed.stdout
    assert f"password={TOKEN}\n" in completed.stdout


def test_after_commit_is_told_once_the_lock_is_released(repo: GitRepo) -> None:
    told: list[tuple[str, bool]] = []

    def after(commit: str) -> None:
        # Released: another write could take the lock now.
        free = repo._lock.acquire(blocking=False)
        if free:
            repo._lock.release()
        told.append((commit, free))

    repo.after_commit = after
    commit = write_commit(repo, "a.txt", "one\n")
    assert told == [(commit, True)]
    # Nothing to commit: nothing told.
    assert repo.commit("Again", "a.txt") is None
    assert len(told) == 1


def test_a_failing_after_commit_never_fails_the_commit(repo: GitRepo) -> None:
    def after(commit: str) -> None:
        raise RuntimeError("the push blew up")

    repo.after_commit = after
    commit = write_commit(repo, "a.txt", "one\n")
    assert repo.head() == commit


def test_unpushed_counts_what_the_remote_lacks(repo: GitRepo) -> None:
    first = write_commit(repo, "a.txt", "one\n")
    write_commit(repo, "a.txt", "two\n")
    head = write_commit(repo, "a.txt", "three\n")
    behind, oldest = repo.unpushed(first, head)
    assert behind == 2 and oldest is not None
    assert repo.unpushed(head, head) == (0, None)
    # A remote ahead of this view is not behind.
    assert repo.unpushed(head, first) == (0, None)
    # A commit this repository has never seen: every commit is unpushed.
    assert repo.unpushed("0" * 40, head)[0] == 4


# ── RemotePush ────────────────────────────────────────────────────────────────


def test_a_push_records_the_commit_and_the_lag_closes(repo: GitRepo, remote: Path) -> None:
    records = MemoryRecords()
    push = pusher(repo, records, RemoteTarget(url=str(remote)))
    write_commit(repo, "a.txt", "one\n")
    head = write_commit(repo, "a.txt", "two\n")
    [lag] = push.lag()
    assert (lag.repository, lag.branch, lag.behind, lag.pushed_commit) == (
        "models",
        "models",
        3,
        None,
    )
    assert push.push("models") is True
    assert remote_ref(remote, "refs/heads/models") == head
    [lag] = push.lag()
    assert (lag.behind, lag.pushed_commit, lag.error) == (0, head, None)
    # Nothing new: no push at all.
    assert push.push("models") is False


def test_a_failed_push_is_recorded_scrubbed_and_raised(repo: GitRepo, tmp_path: Path) -> None:
    records = MemoryRecords()
    missing = tmp_path / "nowhere.git"
    push = pusher(repo, records, RemoteTarget(url=str(missing), username="me", token=TOKEN))
    write_commit(repo, "a.txt", "one\n")
    with pytest.raises(PushError):
        push.push("models")
    [lag] = push.lag()
    assert lag.error and TOKEN not in lag.error
    assert lag.behind == 2 and lag.pushed_commit is None


def test_scrub_removes_the_token() -> None:
    assert scrub(f"fatal: {TOKEN} refused", TOKEN) == f"fatal: {SCRUBBED} refused"
    assert len(scrub("x" * 5000, None)) == 2000


def test_scrub_removes_a_credential_in_a_url() -> None:
    """Whatever URL git echoes, its user and password never reach the record."""
    text = scrub("fatal: unable to access 'https://me:hunter2@git.example/r.git/'", None)
    assert "hunter2" not in text and "me:" not in text
    assert f"https://{SCRUBBED}@git.example/r.git/" in text


# ── why a push failed (#7388) ─────────────────────────────────────────────────

#: GitHub's answer to the models repository on prod, 2026-11-24 (#7388), with the
#: lines git prints around it.
GITHUB_SIZE_REFUSAL = """\
remote: error: Trace: 3f1c2a
remote: error: See https://gh.io/lfs for more information.
remote: error: File flexi-fidget-multicolor/meshes/o1-p1.3mf is 130.30 MB; this exceeds \
GitHub's file size limit of 100.00 MB
remote: error: GH001: Large files detected. You may want to try Git Large File Storage.
remote: warning: File brick-santa-figurine/meshes/o1-p2.3mf is 79.15 MB; this is larger \
than GitHub's recommended maximum file size of 50.00 MB
To https://github.com/example/history.git
 ! [remote rejected] abc123 -> models (pre-receive hook declined)
error: failed to push some refs to 'https://github.com/example/history.git'
"""


def test_a_size_refusal_names_the_file_and_lasts() -> None:
    cause, lasting = explain(GITHUB_SIZE_REFUSAL)
    assert lasting is True
    assert cause == (
        "the remote refused flexi-fidget-multicolor/meshes/o1-p1.3mf (130.30 MB):"
        " over its file size limit"
    )


@pytest.mark.parametrize(
    ("stderr", "cause"),
    [
        (
            """To /srv/remote.git
 ! [rejected]        abc -> models (non-fast-forward)
error: failed to push some refs to '/srv/remote.git'
hint: Updates were rejected because the tip of your current branch is behind""",
            "the remote refused it: [rejected] abc -> models (non-fast-forward)",
        ),
        (
            """remote: error: GH013: Repository rule violations found for refs/heads/models.
To https://github.com/example/history.git
 ! [remote rejected] abc -> models (push declined due to repository rule violations)
error: failed to push some refs to 'https://github.com/example/history.git'""",
            "the remote refused it: GH013: Repository rule violations found for refs/heads/models.",
        ),
        (
            """remote: Invalid username or token.
fatal: Authentication failed for 'https://github.com/example/history.git/'""",
            "the remote refused it: Authentication failed for"
            " 'https://github.com/example/history.git/'",
        ),
        (
            """remote: Permission to example/history.git denied to someone.
fatal: unable to access 'https://github.com/example/history.git/': The requested URL \
returned error: 403""",
            "the remote refused it: unable to access"
            " 'https://github.com/example/history.git/': The requested URL returned error: 403",
        ),
    ],
)
def test_a_refusal_no_retry_can_fix_lasts(stderr: str, cause: str) -> None:
    assert explain(stderr) == (cause, True)


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: unable to access 'https://git.example/r.git/': Could not resolve host: git.example",
        "fatal: unable to access 'https://git.example/r.git/': Failed to connect to"
        " git.example port 443 after 3 ms: Connection refused",
        "error: RPC failed; HTTP 502 curl 22 The requested URL returned error: 502\n"
        "send-pack: unexpected disconnect while reading sideband packet\n"
        "fatal: the remote end hung up unexpectedly",
        "",
    ],
)
def test_a_transport_failure_does_not_last(stderr: str) -> None:
    _cause, lasting = explain(stderr)
    assert lasting is False


def test_a_refused_head_is_not_pushed_again(
    repo: GitRepo, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote on another history (git: "fetch first") refuses every try: once, then
    not until the head moves
    or the backoff ends. The refusal stays in the lag, with when it is tried again."""
    other = GitRepo(tmp_path / "other")
    other.ensure_repo()
    other.push(str(remote), write_commit(other, "b.txt", "elsewhere\n"), "refs/heads/models")
    head = write_commit(repo, "a.txt", "one\n")
    records = MemoryRecords()
    push = pusher(repo, records, RemoteTarget(url=str(remote), username="me", token=TOKEN))
    tries: list[str] = []
    real = repo.push

    def counted(url: str, commit: str, ref: str, **kwargs: Any) -> None:
        tries.append(commit)
        real(url, commit, ref, **kwargs)

    monkeypatch.setattr(repo, "push", counted)
    with pytest.raises(PushError):
        push.push("models")
    record = records.get("models")
    assert record is not None and record.refused_commit == head
    # The sweep's next runs: no push, no new error, and the sweep itself passes.
    assert push.push("models") is False
    assert push.push_all() == []
    assert tries == [head]
    [lag] = push.lag()
    assert lag.error is not None and lag.error.startswith("the remote refused it: ")
    assert "[rejected]" in lag.error.splitlines()[0] and TOKEN not in lag.error
    assert lag.retry_after == record.attempted_at + REFUSED_BACKOFF
    # Past the backoff: tried once more.
    records.age("models", REFUSED_BACKOFF)
    with pytest.raises(PushError):
        push.push("models")
    assert tries == [head, head]
    # A new head is tried at once.
    newer = write_commit(repo, "a.txt", "two\n")
    with pytest.raises(PushError):
        push.push("models")
    assert tries == [head, head, newer]


def test_a_transport_failure_is_tried_at_every_sweep(
    repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = write_commit(repo, "a.txt", "one\n")
    records = MemoryRecords()
    push = pusher(repo, records, RemoteTarget(url="https://git.example/r.git"))
    tries: list[str] = []

    def timing_out(url: str, commit: str, ref: str, **kwargs: Any) -> None:
        tries.append(commit)
        raise GitTimeoutError("git push timed out after 120s")

    monkeypatch.setattr(repo, "push", timing_out)
    for _ in range(2):
        with pytest.raises(PushError):
            push.push_all()
    assert tries == [head, head]
    record = records.get("models")
    assert record is not None and record.refused_commit is None
    [lag] = push.lag()
    assert lag.error == "git push timed out after 120s" and lag.retry_after is None


def test_a_refusal_at_another_remote_holds_nothing_back(repo: GitRepo, remote: Path) -> None:
    head = write_commit(repo, "a.txt", "one\n")
    records = MemoryRecords()
    records.failed("models", "https://old.example/r.git", "refused", head)
    assert pusher(repo, records, RemoteTarget(url=str(remote))).push("models") is True
    assert remote_ref(remote, "refs/heads/models") == head


def test_no_remote_pushes_nothing(repo: GitRepo) -> None:
    push = pusher(repo, MemoryRecords(), None)
    push.watch()
    write_commit(repo, "a.txt", "one\n")
    assert push.push("models") is False
    assert push.lag() == []


def test_a_remote_set_since_another_counts_every_commit(repo: GitRepo, remote: Path) -> None:
    records = MemoryRecords()
    head = write_commit(repo, "a.txt", "one\n")
    records.pushed("models", "https://old.example/repo.git", head)
    [lag] = pusher(repo, records, RemoteTarget(url=str(remote))).lag()
    assert (lag.behind, lag.pushed_commit) == (2, None)


def test_each_commit_is_pushed_after_it_lands(repo: GitRepo, remote: Path) -> None:
    """`watch`: a commit asks for a push on a thread of its own; the commit does not wait."""
    records = MemoryRecords()
    push = pusher(repo, records, RemoteTarget(url=str(remote)))
    push.watch()
    head = write_commit(repo, "a.txt", "one\n")
    deadline = time.monotonic() + 20
    while remote_ref(remote, "refs/heads/models") != head and time.monotonic() < deadline:
        time.sleep(0.05)
    assert remote_ref(remote, "refs/heads/models") == head
    while (records.get("models") is None) and time.monotonic() < deadline:
        time.sleep(0.05)
    record = records.get("models")
    assert record is not None and record.pushed_commit == head


def test_a_slow_push_never_holds_up_a_commit(
    repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    started = threading.Event()

    class Slow(RemotePush):
        def push(self, name: str) -> bool:
            started.set()
            release.wait(10)
            return False

    push = Slow(
        {"models": repo}, MemoryRecords(), lambda: RemoteTarget(url="https://x.example/r.git")
    )
    push.watch()
    try:
        write_commit(repo, "a.txt", "one\n")
        assert started.wait(10)
        # The push is still running; the next commit lands regardless.
        before = time.monotonic()
        write_commit(repo, "a.txt", "two\n")
        assert time.monotonic() - before < 5
    finally:
        release.set()


def test_push_all_tries_every_repository_then_names_the_failures(
    repo: GitRepo, remote: Path, tmp_path: Path
) -> None:
    flows = GitRepo(tmp_path / "flows")
    flows.ensure_repo()
    write_commit(flows, "f/flow.py", "print(1)\n")
    write_commit(repo, "a.txt", "one\n")
    records = MemoryRecords()
    push = RemotePush(
        {"models": repo, "flows": flows}, records, lambda: RemoteTarget(url=str(remote))
    )
    assert push.push_all() == ["models", "flows"]
    assert remote_ref(remote, "refs/heads/models") == repo.head()
    assert remote_ref(remote, "refs/heads/flows") == flows.head()
    assert push.push_all() == []


# ── the settings and the sweep ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "ftp://git.example/r.git",
        "git.example/r.git",
        f"https://me:{TOKEN}@git.example/r.git",
        "https://me@git.example/r.git",
        "ext::sh -c id",
    ],
)
def test_the_remote_url_is_http_with_no_credential(url: str) -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_GIT_REMOTE_URL") as raised:
        check_value("git_remote_url", url)
    assert TOKEN not in str(raised.value)


def test_a_plain_https_remote_url_is_taken() -> None:
    assert check_value("git_remote_url", " https://git.example/r.git ") == (
        "https://git.example/r.git"
    )
    assert check_value("git_remote_url", "") is None


def test_the_sweep_retries_within_the_prune_interval() -> None:
    """With the sweeps off (the owner's choice), the prune Schedule still retries."""
    assert GIT_REMOTE_SWEEP in SWEEPS and GIT_REMOTE_SWEEP in PRUNE_SWEEPS
    assert timedelta(seconds=2 * PUSH_TIMEOUT) < GIT_REMOTE_TIMEOUT


@pytest.mark.requires_postgres
def test_the_records_keep_what_landed_through_a_failure(pg_pool: PgPool) -> None:
    records = PushRecords(pg_pool)
    assert records.get("models") is None
    records.pushed("models", "https://a.example/r.git", "c1")
    records.failed("models", "https://a.example/r.git", "refused")
    record = records.get("models")
    assert record is not None
    assert (record.pushed_commit, record.error) == ("c1", "refused")
    # Another URL's failure forgets what the old one had.
    records.failed("models", "https://b.example/r.git", "unreachable")
    record = records.get("models")
    assert record is not None
    assert (record.url, record.pushed_commit, record.pushed_at) == (
        "https://b.example/r.git",
        None,
        None,
    )
    records.pushed("models", "https://b.example/r.git", "c2")
    record = records.get("models")
    assert record is not None and (record.pushed_commit, record.error) == ("c2", None)


@pytest.mark.requires_postgres
def test_the_records_keep_a_refused_head_until_another_attempt(pg_pool: PgPool) -> None:
    """#7388: the head the remote refused for good, cleared by any later outcome."""
    records = PushRecords(pg_pool)
    url = "https://a.example/r.git"
    records.failed("models", url, "too big", "c1")
    record = records.get("models")
    assert record is not None and (record.refused_commit, record.error) == ("c1", "too big")
    records.failed("models", url, "timed out")
    record = records.get("models")
    assert record is not None and (record.refused_commit, record.error) == (None, "timed out")
    records.failed("models", url, "too big", "c1")
    records.pushed("models", url, "c2")
    record = records.get("models")
    assert record is not None and (record.refused_commit, record.error) == (None, None)
