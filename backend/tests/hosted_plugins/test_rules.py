"""A hosted plugin's draft paths and names (#6932, spec 2026-11-21 §6.2)."""

from __future__ import annotations

import pytest

from scadbuddy.hosted_plugins import rules


@pytest.mark.parametrize(
    "path",
    [
        "skills/hello/SKILL.md",
        "skills/hello/scripts/run.sh",
        "agents/review.md",
        "agents/review/deep.md",
        "commands/wave.md",
        "skills/héllo/SKILL.md",
    ],
)
def test_a_path_under_the_allowed_folders_is_taken(path: str) -> None:
    assert rules.check_path(path) == path


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("", "empty"),
        ("/skills/a/SKILL.md", "relative"),
        ("skills/../agents/x.md", ".."),
        ("skills/./a.md", '"."'),
        ("skills/.hidden/SKILL.md", '"."'),
        ("skills/a/.env", '"."'),
        (".claude-plugin/plugin.json", "generated"),
        ("hooks/hooks.json", "skills/"),
        (".mcp.json", '"."'),
        ("README.md", "skills/"),
        ("skills", "skills/"),
        ("skills/", "empty segment"),
        ("skills//a.md", "empty segment"),
        ("skills\\a\\SKILL.md", "backslash"),
        ("skills/a/\x00.md", "control"),
        ("skills/a/\ud800.md", "UTF-8"),
        ("skills/" + "a" * 1100, "long"),
    ],
)
def test_a_path_outside_the_rules_is_refused(path: str, why: str) -> None:
    with pytest.raises(rules.PathRefusedError) as refused:
        rules.check_path(path)
    assert why in str(refused.value)


def test_only_a_skill_may_hold_an_executable() -> None:
    rules.check_executable("skills/a/run.sh", executable=True)
    rules.check_executable("agents/a.md", executable=False)
    for path in ("agents/a.md", "commands/a.md"):
        with pytest.raises(rules.PathRefusedError, match="skills/"):
            rules.check_executable(path, executable=True)


def test_a_path_that_clashes_with_another_is_refused() -> None:
    existing = ["skills/a/SKILL.md", "skills/b/x.md"]
    assert rules.clash("skills/c/SKILL.md", existing) is None
    assert rules.clash("skills/a/SKILL.md", existing) is None  # itself: an overwrite
    assert "a folder" in (rules.clash("skills/a", existing) or "")
    assert "a file" in (rules.clash("skills/b/x.md/y.md", existing) or "")
    assert "case" in (rules.clash("skills/A/SKILL.md", existing) or "")


@pytest.mark.parametrize("name", ["greeter", "my-tools", "a1"])
def test_a_kebab_name_is_taken(name: str) -> None:
    assert rules.name_problem(name) is None


@pytest.mark.parametrize(
    "name", ["Greeter", "a", "-a", "a-", "a--b", "a_b", "a.b", "x" * 33, "claude", "workspace"]
)
def test_a_name_the_package_rules_refuse_is_refused(name: str) -> None:
    assert rules.name_problem(name) is not None


def test_the_sort_is_javascripts() -> None:
    paths = ["skills/a/～.md", "skills/a/\U0001f600.md", "skills/a-b/x", "skills/a/x"]
    assert sorted(paths, key=rules.sort_key) == [
        "skills/a-b/x",
        "skills/a/x",
        "skills/a/\U0001f600.md",
        "skills/a/～.md",
    ]
