"""`FlowHistory` (#2503, plan 2026-10-11-flow-grants-and-git.md Task C2): `data/flows`,
one commit per version, numbered by its position among its flow's commits."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from scadbuddy.core.authorship import AGENT_AUTHOR_NAME, AgentAuthor, authored_as
from scadbuddy.core.gitrepo import LOCK_NAME, GitError, RevisionNotFoundError, git_env
from scadbuddy.flows.definitions import FlowDefinitions
from scadbuddy.flows.repository import (
    DETAILS_TRAILER,
    DetailsNotSavedError,
    FlowHistory,
    FlowNotPresentError,
)

pytestmark = pytest.mark.requires_git

ONE = "import asyncio\nasync def main():\n    return 1\nasyncio.run(main())\n"
TWO = ONE.replace("return 1", "return 2")


@pytest.fixture
def history(tmp_path: Path) -> FlowHistory:
    made = FlowHistory(tmp_path / "data" / "flows")
    made.ensure_repo()
    return made


def register(history: FlowHistory, name: str, script: str) -> tuple[str, str, int]:
    version_id = str(uuid.uuid4())
    indexed: list[tuple[str, int]] = []
    commit, version = history.register(
        name, version_id, lambda c, v: indexed.append((c, v)), script=script
    )
    assert indexed == [(commit, version)]
    return version_id, commit, version


def test_a_registration_is_one_commit_with_its_author_and_flow_trailers(
    history: FlowHistory,
) -> None:
    with authored_as(AgentAuthor(principal="token:p1", session="s1")):
        version_id, commit, version = register(history, "swap", ONE)
    assert version == 1 and commit == history.head()
    assert (history.root / "swap" / "flow.py").read_text() == ONE
    [revision] = history.log("swap")
    assert (revision.author, revision.message) == (AGENT_AUTHOR_NAME, "Register swap v1")
    trailers = history._out("log", "-1", "--format=%(trailers:only)")
    assert f"ScadBuddy-Flow: swap\nScadBuddy-Flow-Version: {version_id}" in trailers
    assert "ScadBuddy-Agent-Principal: token:p1" in trailers
    assert history.commit_for(version_id) == commit
    assert history.script_at("swap", commit) == ONE
    # Its lock is its own, in data/flows.
    assert (history.root / LOCK_NAME).exists()


def test_an_unchanged_script_is_still_a_new_commit_and_version(history: FlowHistory) -> None:
    _, first, _ = register(history, "swap", ONE)
    register(history, "other", TWO)
    _, again, version = register(history, "swap", ONE)
    assert again != first and version == 2
    assert [v.version for v in history.versions("swap")] == [2, 1]
    assert history.version_of("swap", again) == 2
    assert history.version_of("swap", first[:7]) == 1
    with pytest.raises(RevisionNotFoundError):
        history.version_of("other", first)


def test_restore_to_the_heads_own_content_is_a_new_commit(history: FlowHistory) -> None:
    _, first, _ = register(history, "swap", ONE)
    register(history, "swap", TWO)
    version_id = str(uuid.uuid4())
    restored, version = history.register("swap", version_id, lambda c, v: None, restore=first)
    assert version == 3 and history.script_at("swap", restored) == ONE
    assert history.log("swap", limit=1)[0].message == f"Restore swap to {first[:7]}"
    again, version = history.register("swap", str(uuid.uuid4()), lambda c, v: None, restore=first)
    assert version == 4 and again != restored
    assert history.commit_for(version_id) == restored


def test_a_diff_names_flow_py_and_defaults_to_the_version_before(history: FlowHistory) -> None:
    _, first, _ = register(history, "swap", ONE)
    register(history, "other", ONE)
    _, second, _ = register(history, "swap", TWO)
    base, head, patch = history.diff_flow("swap", None, second)
    assert (base, head) == (first, second)
    assert "--- a/flow.py" in patch and "+    return 2" in patch
    _, _, back = history.diff_flow("swap", second, first)
    assert "+    return 1" in back


def test_concurrent_registrations_each_get_their_own_commits_position(
    history: FlowHistory,
) -> None:
    results: list[tuple[str, int]] = []
    errors: list[BaseException] = []

    def one(n: int) -> None:
        try:
            _, commit, version = register(history, "swap", ONE.replace("1", str(n)))
            results.append((commit, version))
        except BaseException as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=one, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(v for _, v in results) == [1, 2, 3, 4, 5, 6]
    for commit, version in results:
        assert history.version_of("swap", commit) == version


def test_a_failed_log_is_an_error_not_an_empty_history(
    history: FlowHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(history, "swap", ONE)
    run = history._run

    def failing(*args: str, **kwargs: object) -> object:
        if args[0] == "log":
            args = ("log", "--no-such-option")
        return run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(history, "_run", failing)
    with pytest.raises(GitError):
        register(history, "swap", TWO)


def test_a_repository_with_no_commit_has_no_versions(tmp_path: Path) -> None:
    root = tmp_path / "flows"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], env=git_env(), check=True)
    assert FlowHistory(root).commits() == []


def test_a_delete_is_one_commit_removing_the_flow_and_its_history_stays(
    history: FlowHistory,
) -> None:
    _, first, _ = register(history, "swap", ONE)
    _, second, _ = register(history, "swap", TWO)
    register(history, "other", ONE)
    recorded: list[str] = []
    with authored_as(AgentAuthor(principal="token:p1", session="s1")):
        deleted = history.delete("swap", recorded.append)
    assert deleted is not None and deleted == history.head() and recorded == [deleted]
    assert not (history.root / "swap").exists()
    assert (history.root / "other" / "flow.py").exists()
    [revision] = history.log("swap", limit=1)
    assert (revision.author, revision.message) == (AGENT_AUTHOR_NAME, "Delete swap")
    trailers = history._out("log", "-1", "--format=%(trailers:only)")
    assert "ScadBuddy-Flow-Deleted: swap" in trailers
    assert "ScadBuddy-Agent-Principal: token:p1" in trailers
    # Not a version: the history lists the two, and each one's script is still there.
    assert [v.commit for v in history.versions("swap")] == [second, first]
    assert history.script_at("swap", first) == ONE
    # Deleting it again commits nothing, and records the delete that removed it.
    assert history.delete("swap", recorded.append) == deleted
    assert history.head() == deleted and recorded == [deleted, deleted]


def test_a_deleted_flow_is_restored_as_its_next_version(history: FlowHistory) -> None:
    _, first, _ = register(history, "swap", ONE)
    register(history, "swap", TWO)
    history.delete("swap", lambda _c: None)
    restored, version = history.register(
        "swap", str(uuid.uuid4()), lambda c, v: None, restore=first
    )
    assert version == 3 and restored == history.head()
    assert (history.root / "swap" / "flow.py").read_text() == ONE
    _, _, again = register(history, "swap", TWO)
    assert again == 4


def test_deleting_a_flow_with_no_version_is_refused(history: FlowHistory) -> None:
    with pytest.raises(RevisionNotFoundError):
        history.delete("nothing", lambda _c: None)


INPUTS = {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]}


def test_inputs_are_committed_with_the_script_and_restored_with_it(history: FlowHistory) -> None:
    """#2483: `<name>/inputs.json` beside `flow.py`, in the version's one commit."""
    first, version = history.register(
        "swap", str(uuid.uuid4()), lambda c, v: None, script=ONE, inputs=INPUTS
    )
    assert version == 1
    assert history.inputs_at("swap", first) == INPUTS
    changed = history._out("show", "--name-only", "--format=", first).split()
    assert changed == ["swap/flow.py", "swap/inputs.json"]
    # A version without inputs drops the file; restoring the first brings it back.
    second, _ = history.register("swap", str(uuid.uuid4()), lambda c, v: None, script=TWO)
    assert history.inputs_at("swap", second) is None
    assert not (history.root / "swap" / "inputs.json").exists()
    restored, _ = history.register("swap", str(uuid.uuid4()), lambda c, v: None, restore=first)
    assert history.inputs_at("swap", restored) == INPUTS
    assert history.script_at("swap", restored) == ONE


# ── #3186: a display name and description in `flow.json`, and commit messages ──


def test_a_registration_takes_its_message_and_body(history: FlowHistory) -> None:
    commit, _ = history.register(
        "swap",
        str(uuid.uuid4()),
        lambda c, v: None,
        script=ONE,
        message="Queue the\nnightly batch",
        body="Longer notes.\n\nSecond paragraph.",
    )
    [version] = history.versions("swap")
    # The subject is one line; the body keeps its paragraphs, and the trailers stay
    # trailers (the version is still numbered).
    assert (version.commit, version.message, version.version) == (
        commit,
        "Queue the nightly batch",
        1,
    )
    assert version.body == "Longer notes.\n\nSecond paragraph."
    second, _ = history.register("swap", str(uuid.uuid4()), lambda c, v: None, script=TWO)
    newest = history.versions("swap")[0]
    assert (newest.commit, newest.message, newest.body) == (second, "Register swap v2", None)


def test_a_restore_and_a_delete_take_their_message(history: FlowHistory) -> None:
    _, first, _ = register(history, "swap", ONE)
    register(history, "swap", TWO)
    history.register(
        "swap", str(uuid.uuid4()), lambda c, v: None, restore=first, message="Back to v1"
    )
    assert history.versions("swap")[0].message == "Back to v1"
    history.delete("swap", lambda _c: None, message="Retire it", body="Superseded by swap2.")
    [revision] = history.log("swap", limit=1)
    assert revision.message == "Retire it"
    assert "Superseded by swap2." in history._out("log", "-1", "--format=%b")


def test_details_are_their_own_commit_in_flow_json(history: FlowHistory) -> None:
    _, first, _ = register(history, "swap", ONE)
    assert history.details("swap").display_name is None
    commit = history.set_details(
        "swap", display_name="Nightly swap", description="# Swap\nIt swaps."
    )
    assert commit is not None and commit == history.head()
    found = json.loads((history.root / "swap" / "flow.json").read_text())
    assert found == {"display_name": "Nightly swap", "description": "# Swap\nIt swaps."}
    details = history.details("swap")
    assert (details.display_name, details.description) == ("Nightly swap", "# Swap\nIt swaps.")
    [revision] = history.log("swap", limit=1)
    assert revision.message == "Rename swap to Nightly swap"
    assert "ScadBuddy-Flow-Details: swap" in history._out("log", "-1", "--format=%(trailers:only)")
    # Not a version: the history still lists one.
    assert [v.commit for v in history.versions("swap")] == [first]
    # An omitted field is unchanged; an unchanged one commits nothing.
    assert history.set_details("swap", description="Swaps.") is not None
    assert history.details("swap").display_name == "Nightly swap"
    assert history.log("swap", limit=1)[0].message == "Describe swap"
    assert history.set_details("swap", description="Swaps.") is None
    # A message of its own; an empty value clears the field, and with both cleared the
    # file goes.
    history.set_details("swap", display_name="", description="", message="Plain again")
    assert history.log("swap", limit=1)[0].message == "Plain again"
    assert not (history.root / "swap" / "flow.json").exists()


def test_details_of_a_flow_with_no_version_are_refused(history: FlowHistory) -> None:
    with pytest.raises(RevisionNotFoundError):
        history.set_details("nothing", display_name="Nothing")


def test_a_new_version_and_a_restore_keep_the_flows_details(history: FlowHistory) -> None:
    """Details are the flow's, not a version's: a restore of a version from before
    they were set keeps them."""
    _, first, _ = register(history, "swap", ONE)
    history.set_details("swap", display_name="Nightly swap")
    register(history, "swap", TWO)
    assert history.details("swap").display_name == "Nightly swap"
    restored, _ = history.register("swap", str(uuid.uuid4()), lambda c, v: None, restore=first)
    assert history.details("swap").display_name == "Nightly swap"
    assert history.script_at("swap", restored) == ONE


def test_a_registration_with_details_commits_them_after_the_version(
    history: FlowHistory,
) -> None:
    commit, version = history.register(
        "swap",
        str(uuid.uuid4()),
        lambda c, v: None,
        script=ONE,
        display_name="Nightly swap",
        description="Swaps.",
    )
    assert version == 1 and history.head() != commit
    messages = [r.message for r in history.log("swap", limit=2)]
    assert messages == ["Rename swap to Nightly swap", "Register swap v1"]
    assert "swap/flow.json" not in history._out("show", "--name-only", "--format=", commit)
    # The same details again commit nothing more.
    again, _ = history.register(
        "swap", str(uuid.uuid4()), lambda c, v: None, script=TWO, display_name="Nightly swap"
    )
    assert history.head() == again


def test_a_failed_details_commit_leaves_the_version_and_says_so(
    history: FlowHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version is committed and indexed first; a details commit that then fails is
    rolled back on disk and reported with the version, never as a failed registration
    (a retry would add one more version)."""
    register(history, "swap", ONE)
    history.set_details("swap", display_name="Nightly swap")
    real = history._commit_locked

    def failing(message: str, *paths: str, **kwargs: Any) -> str | None:
        if any(t.startswith(DETAILS_TRAILER) for t in kwargs.get("trailers", ())):
            raise GitError("could not commit the details")
        return real(message, *paths, **kwargs)

    monkeypatch.setattr(history, "_commit_locked", failing)
    indexed: list[tuple[str, int]] = []
    with pytest.raises(DetailsNotSavedError) as caught:
        history.register(
            "swap",
            str(uuid.uuid4()),
            lambda c, v: indexed.append((c, v)),
            script=TWO,
            display_name="Renamed",
        )
    assert (caught.value.commit, caught.value.version) == indexed[0]
    assert indexed[0][1] == 2 and history.head() == indexed[0][0]
    # The file and the index are as the last details commit left them.
    assert history.details("swap").display_name == "Nightly swap"
    assert history._out("status", "--porcelain").strip() == ""


def test_a_body_ending_in_a_key_value_paragraph_keeps_it(history: FlowHistory) -> None:
    """Only the trailers ScadBuddy adds are taken off a body: a paragraph of the
    person's own that reads like trailers stays."""
    history.register(
        "swap",
        str(uuid.uuid4()),
        lambda c, v: None,
        script=ONE,
        body="Notes.\n\nReviewed-by: Sam\nTicket: 42",
    )
    assert history.versions("swap")[0].body == "Notes.\n\nReviewed-by: Sam\nTicket: 42"
    history.register(
        "swap", str(uuid.uuid4()), lambda c, v: None, script=TWO, body="Reviewed-by: Sam"
    )
    assert history.versions("swap")[0].body == "Reviewed-by: Sam"


def test_restoring_a_deleted_flow_brings_back_its_details_at_that_version(
    history: FlowHistory,
) -> None:
    """A deleted flow has no details in the tree: a restore brings back the ones it had
    at the restored commit, in the version's own commit."""
    _, first, _ = register(history, "swap", ONE)
    history.set_details("swap", display_name="Old name")
    _, second, _ = register(history, "swap", TWO)
    history.set_details("swap", display_name="New name")
    history.delete("swap", lambda _c: None)
    # Deleted, it reads as it was when it was deleted (the list's archived view).
    assert not (history.root / "swap").exists()
    assert history.details("swap").display_name == "New name"
    restored, _ = history.register("swap", str(uuid.uuid4()), lambda c, v: None, restore=second)
    assert history.details("swap").display_name == "Old name"
    changed = history._out("show", "--name-only", "--format=", restored).split()
    assert "swap/flow.json" in changed
    # A version from before any details had none, so the flow comes back without.
    history.delete("swap", lambda _c: None)
    history.register("swap", str(uuid.uuid4()), lambda c, v: None, restore=first)
    assert history.details("swap").display_name is None


def test_a_versions_diff_never_shows_the_flows_details(history: FlowHistory) -> None:
    """Details are the flow's, not a version's: a details change between two versions,
    or brought back by a deleted flow's restore, is no hunk of their diff."""
    _, first, _ = register(history, "swap", ONE)
    history.set_details("swap", display_name="Nightly swap")
    _, second, _ = register(history, "swap", ONE)
    _, _, patch = history.diff_flow("swap", first, second)
    assert patch == ""
    _, third, _ = register(history, "swap", TWO)
    _, _, patch = history.diff_flow("swap", None, third)
    assert "flow.py" in patch and "flow.json" not in patch
    history.delete("swap", lambda _c: None)
    restored, _ = history.register("swap", str(uuid.uuid4()), lambda c, v: None, restore=first)
    _, _, patch = history.diff_flow("swap", None, restored)
    assert "flow.json" not in patch and "-    return 2" in patch


def test_a_display_name_is_one_line_of_printable_text(history: FlowHistory) -> None:
    register(history, "swap", ONE)
    history.set_details(
        "swap", display_name="  Nightly\nswap\x1f batch\t ", description="# A\n\nB\x00."
    )
    details = history.details("swap")
    assert details.display_name == "Nightly swap batch"
    # The description is Markdown: its lines stay, control characters go.
    assert details.description == "# A\n\nB."


def test_details_of_a_deleted_flow_are_refused_as_deleted(history: FlowHistory) -> None:
    register(history, "swap", ONE)
    history.delete("swap", lambda _c: None)
    with pytest.raises(FlowNotPresentError):
        history.set_details("swap", display_name="Back")


def test_a_damaged_flow_json_reads_as_no_details(history: FlowHistory) -> None:
    register(history, "swap", ONE)
    (history.root / "swap" / "flow.json").write_text("not json")
    assert history.details("swap").display_name is None


def test_a_details_write_swaps_flow_json_in_whole(
    history: FlowHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read without the write lock sees the old ``flow.json`` or the new one, never a
    part of one (#5357): the new file is written in full under ``.git/``, then renamed
    into place."""
    register(history, "swap", ONE)
    history.set_details("swap", display_name="Old name")
    path = history.root / "swap" / "flow.json"
    seen: list[tuple[Path, str, str]] = []
    real = os.replace

    def replace(source: str, target: Path) -> None:
        staged = Path(source)
        seen.append((staged.parent, staged.read_text(), path.read_text()))
        real(source, target)

    monkeypatch.setattr("scadbuddy.flows.repository.os.replace", replace)
    history.set_details("swap", display_name="New name")
    assert len(seen) == 1
    where, staged, visible = seen[0]
    assert where == history.root / ".git"
    assert json.loads(staged) == {"display_name": "New name"}
    assert json.loads(visible) == {"display_name": "Old name"}
    assert history.details("swap").display_name == "New name"
    assert sorted(p.name for p in (history.root / "swap").iterdir()) == ["flow.json", "flow.py"]


def test_a_details_write_leaves_flow_json_readable_by_others(history: FlowHistory) -> None:
    """The swapped-in ``flow.json`` has the mode a plain write gives (0666 less the
    umask, 0644 under 022), not mkstemp's 0600 (#5372, #5382)."""
    register(history, "swap", ONE)
    history.set_details("swap", display_name="Name")
    plain = history.root / "plain"
    plain.write_text("")
    expected = plain.stat().st_mode & 0o777
    assert (history.root / "swap" / "flow.json").stat().st_mode & 0o777 == expected


def test_a_failed_details_swap_leaves_flow_json_and_no_temp_file(
    history: FlowHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(history, "swap", ONE)
    history.set_details("swap", display_name="Old name")
    before = set((history.root / ".git").iterdir())

    def replace(source: str, target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("scadbuddy.flows.repository.os.replace", replace)
    with pytest.raises(OSError):
        history.set_details("swap", display_name="New name")
    assert history.details("swap").display_name == "Old name"
    assert set((history.root / ".git").iterdir()) == before


def test_a_restore_never_checks_out_or_rewrites_flow_json(
    history: FlowHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore of a flow in the tree checks out only its script and inputs (#5357):
    ``flow.json`` is the same file, untouched, with the details it had."""
    register(history, "swap", ONE)
    history.set_details("swap", display_name="Old name")
    # A version whose tree has a flow.json with other details.
    _, first, _ = register(history, "swap", ONE)
    register(history, "swap", TWO)
    history.set_details("swap", display_name="New name")
    path = history.root / "swap" / "flow.json"
    stat = path.stat()
    checkouts: list[tuple[str, ...]] = []
    real = history._run

    def run(*args: str, **kwargs: Any) -> Any:
        if args and args[0] == "checkout":
            checkouts.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(history, "_run", run)
    restored, _ = history.register("swap", str(uuid.uuid4()), lambda c, v: None, restore=first)
    assert checkouts == [("checkout", first, "--", "swap/flow.py")]
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (stat.st_ino, stat.st_mtime_ns)
    assert history.details("swap").display_name == "New name"
    assert history.script_at("swap", restored) == ONE


def _definitions(history: FlowHistory) -> FlowDefinitions:
    # The display names read git only: no store or states.
    return FlowDefinitions(history, cast(Any, None), cast(Any, None))


def test_display_names_are_read_at_head_and_cached_until_it_moves(
    history: FlowHistory, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(history, "swap", ONE)
    register(history, "gone", ONE)
    history.set_details("swap", display_name="Nightly swap")
    history.set_details("gone", display_name="Gone flow")
    history.delete("gone", lambda _c: None)
    definitions = _definitions(history)
    reads: list[str] = []
    real = history.details_at

    def details_at(name: str, commit: str) -> Any:
        reads.append(name)
        return real(name, commit)

    monkeypatch.setattr(history, "details_at", details_at)
    names = {"swap", "gone", "never"}
    want = {"swap": "Nightly swap", "gone": "Gone flow", "never": None}
    assert definitions._display_names(names) == want
    assert sorted(reads) == ["gone", "never", "swap"]
    reads.clear()
    assert definitions._display_names(names) == want
    assert reads == []
    # A details write not yet committed is never read, so never cached.
    (history.root / "swap" / "flow.json").write_text('{"display_name": "Uncommitted"}\n')
    assert definitions._display_names({"swap"}) == {"swap": "Nightly swap"}
    history.set_details("swap", display_name="Renamed")
    assert definitions._display_names({"swap"}) == {"swap": "Renamed"}
    assert reads == ["swap"]


def test_the_umask_is_read_without_setting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Linux the umask comes from /proc/self/status, so no thread ever sees a umask
    of 0 while it is read (#5397). The expected value comes from a plainly written
    file's mode, so the test does not set the umask either (#5417)."""
    from scadbuddy.flows import repository

    def refuse(_mask: int) -> int:
        raise AssertionError("os.umask was called")

    plain = tmp_path / "plain"
    plain.write_text("")
    # A new directory's mode shows the umask's execute bits too, which a file's cannot.
    folder = tmp_path / "folder"
    folder.mkdir()
    monkeypatch.setattr("scadbuddy.flows.repository.os.umask", refuse)
    mask = repository._umask()
    assert 0o666 & ~mask == plain.stat().st_mode & 0o777
    assert 0o777 & ~mask == folder.stat().st_mode & 0o777
