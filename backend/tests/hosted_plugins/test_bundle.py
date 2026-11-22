"""A hosted plugin's publish (#6932, spec 2026-11-21 §6.3 steps 1-3): the draft's checks,
the generated ``plugin.json`` and the deterministic zip."""

from __future__ import annotations

import io
import json
import stat
import zipfile

import pytest

from scadbuddy.hosted_plugins import bundle

SKILL = b"---\nname: hello\ndescription: Says hello\n---\n\nSay hello.\n"


def _draft(**extra: tuple[bytes, bool]) -> dict[str, tuple[bytes, bool]]:
    return {"skills/hello/SKILL.md": (SKILL, False), **extra}


def test_the_manifest_names_the_version() -> None:
    text = bundle.plugin_json("greeter", "Says hello", 3)
    assert json.loads(text) == {"name": "greeter", "description": "Says hello", "version": "3.0.0"}
    assert text.endswith(b"\n")


def test_the_same_draft_gives_the_same_zip_bytes() -> None:
    files = _draft(**{"skills/hello/run.sh": (b"#!/bin/sh\n", True), "agents/a.md": (b"x", False)})
    reordered = dict(reversed(list(files.items())))
    first = bundle.build_zip(files)
    assert bundle.build_zip(reordered) == first
    with zipfile.ZipFile(io.BytesIO(first)) as zf:
        infos = zf.infolist()
        assert [i.filename for i in infos] == ["agents/a.md", "skills/hello/SKILL.md", "skills/hello/run.sh"]
        for info in infos:
            assert info.date_time == bundle.ZIP_DATE_TIME
            assert info.extra == b""
            assert info.create_system == 3
        modes = {i.filename: stat.S_IMODE(i.external_attr >> 16) for i in infos}
        assert modes == {"agents/a.md": 0o644, "skills/hello/SKILL.md": 0o644, "skills/hello/run.sh": 0o755}
        assert stat.S_ISREG(infos[0].external_attr >> 16)
        assert zf.read("skills/hello/run.sh") == b"#!/bin/sh\n"


def test_a_change_of_bytes_or_mode_changes_the_zip() -> None:
    base = bundle.build_zip(_draft(**{"skills/hello/run.sh": (b"x", False)}))
    assert bundle.build_zip(_draft(**{"skills/hello/run.sh": (b"x", True)})) != base
    assert bundle.build_zip(_draft(**{"skills/hello/run.sh": (b"y", False)})) != base


def test_a_good_draft_has_no_problems() -> None:
    files = _draft(
        **{
            "skills/hello/scripts/go.sh": (b"#!/bin/sh\n", True),
            "agents/review.md": (b"---\nname: review\ndescription: Reviews\n---\nBody\n", False),
            "commands/wave.md": (b"Wave at $ARGUMENTS.\n", False),
            "commands/greet.md": (b"---\ndescription: >\n  Greets\n  warmly\nallowed-tools: mcp__scadbuddy__get_model\n---\n", False),
        }
    )
    assert bundle.problems(files) == []


@pytest.mark.parametrize(
    ("files", "why"),
    [
        ({}, "empty"),
        ({"skills/hello/notes.md": (b"x", False)}, "skills/hello/SKILL.md"),
        ({"skills/SKILL.md": (SKILL, False)}, "skills/<skill>/"),
        ({"skills/hello/SKILL.md": (b"---\nname: hello\n---\n", False)}, "description"),
        ({"skills/hello/SKILL.md": (b"---\ndescription: d\n---\n", False)}, "name"),
        ({"skills/hello/SKILL.md": (b"no frontmatter\n", False)}, "frontmatter"),
        ({"skills/hello/SKILL.md": (b"---\nname: hello\ndescription: d\n", False)}, "closing"),
        ({"skills/hello/SKILL.md": (b"---\nname: [hello\ndescription: d\n---\n", False)}, "name"),
        ({"skills/hello/SKILL.md": (b"\xff\xfe", False)}, "UTF-8"),
        (_draft(**{"agents/a.md": (b"---\nname: a\n---\n", False)}), "description"),
        (_draft(**{"agents/a.txt": (b"x", False)}), "Markdown"),
        (_draft(**{"commands/a.json": (b"{}", False)}), "Markdown"),
        (_draft(**{"commands/a.md": (b"---\nhooks: x\n---\n", False)}), "hooks"),
        (_draft(**{"agents/a.md": (b"---\nname: a\ndescription: d\nmcpServers: x\n---\n", False)}), "mcpServers"),
        (_draft(**{"agents/a.md": (b"---\nname: a\ndescription: d\npermissionMode: x\n---\n", False)}), "permissionMode"),
        (_draft(**{"commands/a.md": (b"---\n\"hooks\": x\n---\n", False)}), "plain"),
        (_draft(**{"commands/a.md": (b"---\na: &x 1\n---\n", False)}), "anchor"),
        (_draft(**{"commands/a.md": (b"---\na: 1\na: 2\n---\n", False)}), "twice"),
        (_draft(**{"commands/a.md": (b"Run !`ls` now\n", False)}), "dynamic context"),
        (_draft(**{"skills/hello/x.md": (b"```!\nls\n```\n", False)}), "dynamic context"),
        (_draft(**{"agents/a.md": (b"---\nname: a\ndescription: d\n---\n", True)}), "skills/"),
    ],
)
def test_a_draft_the_rules_refuse_says_why(files: dict[str, tuple[bytes, bool]], why: str) -> None:
    found = bundle.problems(files)
    assert found, files
    assert any(why in p for p in found), found


def test_the_published_tree_is_the_draft_and_its_manifest() -> None:
    files = _draft()
    tree = bundle.published_tree(files, name="greeter", description="d", version=2)
    assert set(tree) == {".claude-plugin/plugin.json", "skills/hello/SKILL.md"}
    assert tree[".claude-plugin/plugin.json"] == (bundle.plugin_json("greeter", "d", 2), False)


def test_the_diff_says_what_was_added_removed_and_changed() -> None:
    before = {
        "a": {"sha256": "1", "size": 1, "executable": False},
        "b": {"sha256": "2", "size": 1, "executable": False},
        "c": {"sha256": "3", "size": 1, "executable": False},
    }
    after = {
        "a": {"sha256": "1", "size": 1, "executable": True},
        "c": {"sha256": "3", "size": 1, "executable": False},
        "d": {"sha256": "4", "size": 1, "executable": False},
    }
    assert bundle.diff_files(before, after) == {"added": ["d"], "removed": ["b"], "changed": ["a"]}
