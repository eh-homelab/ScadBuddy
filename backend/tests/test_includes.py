"""Resolving a model's `include <…>`/`use <…>` and `font = "…"` (#253), against
directories laid out as the data volume lays them out."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from scadbuddy.library.fonts import normalise_family
from scadbuddy.library.includes import (
    MAX_FILES,
    Candidates,
    DependencyReport,
    FontLiteral,
    Statement,
    resolve_dependencies,
    scan,
)
from scadbuddy.library.libraries import CatalogueLibrary, ModelLibrary

COMMIT = "a" * 40
OTHER = "b" * 40
BOSL2 = CatalogueLibrary(
    name="BOSL2",
    url="https://github.com/BelfrySCAD/BOSL2.git",
    ref="v2.0.761",
    licence="BSD-2-Clause",
    homepage="https://github.com/BelfrySCAD/BOSL2",
)


def pin(
    name: str, commit: str = COMMIT, url: str = BOSL2.url, ref: str = "v2.0.761"
) -> ModelLibrary:
    return ModelLibrary(name=name, url=url, ref=ref, commit=commit)


def checkout(root: Path, name: str, commit: str, *files: str) -> None:
    for file in files:
        path = root / name / commit / name / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("module m() cube(1);\n", encoding="utf-8")


def no_candidates(
    pins: Mapping[str, Sequence[tuple[str, ModelLibrary | None]]] | None = None,
) -> Candidates:
    def pins_of(name: str) -> list[tuple[str, ModelLibrary | None]]:
        return list((pins or {}).get(name, []))

    return Candidates(catalogue=[BOSL2], pins_of=pins_of)


def report(
    tmp_path: Path,
    source: str,
    pins: list[ModelLibrary] | None = None,
    *,
    candidates: Candidates | None = None,
    fonts: set[str] | None = None,
) -> DependencyReport:
    model = tmp_path / "models" / "widget"
    model.mkdir(parents=True, exist_ok=True)
    return resolve_dependencies(
        model,
        source,
        pins or [],
        libraries_root=tmp_path / "libraries",
        candidates=candidates or no_candidates(),
        resolvable_fonts=fonts,
    )


# ── scanning, as OpenSCAD's lexer reads it ──────────────────────────────────────


def test_statements_are_found_with_their_lines() -> None:
    statements, _ = scan("include <a.scad>\n\nuse<BOSL2/std.scad>\ncube(1);\n")
    assert statements == [
        Statement(kind="include", target="a.scad", line=1),
        Statement(kind="use", target="BOSL2/std.scad", line=3),
    ]


def test_comments_and_strings_hold_no_statements() -> None:
    source = (
        "// include <line.scad>\n"
        "/* use <block.scad>\n include <still.scad> */\n"
        'echo("include <string.scad>");\n'
        "include <real.scad>\n"
    )
    statements, _ = scan(source)
    assert statements == [Statement(kind="include", target="real.scad", line=5)]


def test_a_keyword_inside_an_identifier_is_not_one() -> None:
    statements, _ = scan("myinclude = 1; reuse <x.scad>\n")
    assert statements == []


def test_whitespace_and_line_breaks_before_the_bracket_are_allowed() -> None:
    statements, _ = scan("include\n  <spaced name.scad>\n")
    assert statements == [Statement(kind="include", target="spaced name.scad", line=1)]


def test_font_literals_are_found_and_unescaped() -> None:
    _, fonts = scan(
        'text("A", font = "Lobster Two:style=Bold");\nfont="Say \\"hi\\"";\nf = font;\n'
    )
    assert fonts == [
        FontLiteral(value="Lobster Two:style=Bold", line=1),
        FontLiteral(value='Say "hi"', line=2),
    ]


# ── resolving, as find_valid_path resolves ──────────────────────────────────────


def test_a_file_beside_the_model_resolves_first(tmp_path: Path) -> None:
    model = tmp_path / "models" / "widget"
    model.mkdir(parents=True)
    (model / "helper.scad").write_text("", encoding="utf-8")

    result = report(tmp_path, "include <helper.scad>\n")

    [entry] = result.includes
    assert (entry.status, entry.path, entry.library) == ("resolved", "helper.scad", None)
    assert result.unresolved == 0


def test_a_pinned_library_resolves_into_its_checkout(tmp_path: Path) -> None:
    checkout(tmp_path / "libraries", "BOSL2", COMMIT, "std.scad")

    result = report(tmp_path, "use <BOSL2/std.scad>\n", [pin("BOSL2")])

    [entry] = result.includes
    assert (entry.status, entry.path, entry.library) == ("resolved", "BOSL2/std.scad", "BOSL2")


def test_an_unpinned_curated_library_is_suggested(tmp_path: Path) -> None:
    result = report(tmp_path, "use <BOSL2/std.scad>\n")

    [entry] = result.includes
    assert entry.status == "unresolved"
    assert "the model pins no libraries" in (entry.reason or "")
    assert entry.suggestion is not None
    assert (entry.suggestion.name, entry.suggestion.source, entry.suggestion.ref) == (
        "BOSL2",
        "catalogue",
        "v2.0.761",
    )
    assert entry.suggestion.has_file is None
    assert result.unresolved == 1


def test_a_catalogue_suggestion_says_whether_a_checkout_on_the_volume_has_the_file(
    tmp_path: Path,
) -> None:
    checkout(tmp_path / "libraries", "BOSL2", COMMIT, "std.scad")
    others = {"BOSL2": [("gadget", pin("BOSL2"))]}

    result = report(
        tmp_path, "use <BOSL2/std.scad>\nuse <BOSL2/nope.scad>\n", candidates=no_candidates(others)
    )

    found, missing = result.includes
    assert found.suggestion is not None and found.suggestion.has_file is True
    assert found.suggestion.commit == COMMIT
    assert missing.suggestion is not None and missing.suggestion.has_file is False


def test_a_library_another_model_pins_from_its_own_url_is_suggested(tmp_path: Path) -> None:
    fork = "https://example.com/me/gridfinity.git"
    checkout(tmp_path / "libraries", "gridfinity", OTHER, "base.scad")
    others = {"gridfinity": [("bins", pin("gridfinity", OTHER, url=fork, ref="main"))]}

    result = report(tmp_path, "use <gridfinity/base.scad>\n", candidates=no_candidates(others))

    suggestion = result.includes[0].suggestion
    assert suggestion is not None
    assert (suggestion.source, suggestion.url, suggestion.ref, suggestion.pinned_by) == (
        "installed",
        fork,
        "main",
        "bins",
    )
    assert suggestion.has_file is True


def test_a_pinned_library_without_the_file_says_so_and_suggests_nothing(tmp_path: Path) -> None:
    checkout(tmp_path / "libraries", "BOSL2", COMMIT, "std.scad")

    result = report(tmp_path, "include <BOSL2/rounding.scad>\n", [pin("BOSL2")])

    [entry] = result.includes
    assert entry.status == "unresolved"
    assert entry.reason == "BOSL2 is pinned at v2.0.761 (aaaaaaa), which has no rounding.scad"
    assert entry.suggestion is None


def test_a_pin_whose_checkout_is_gone_is_reported_not_fetched(tmp_path: Path) -> None:
    result = report(tmp_path, "use <BOSL2/std.scad>\n", [pin("BOSL2")])

    assert result.missing_checkouts == ["BOSL2"]
    [entry] = result.includes
    assert entry.status == "unresolved"
    assert "not on this volume" in (entry.reason or "")
    assert not (tmp_path / "libraries").exists()


def test_an_absolute_path_is_unresolved(tmp_path: Path) -> None:
    [entry] = report(tmp_path, "include </etc/passwd>\n").includes
    assert entry.status == "unresolved"
    assert "absolute path" in (entry.reason or "")


def test_a_model_file_is_followed_relative_to_its_own_directory(tmp_path: Path) -> None:
    model = tmp_path / "models" / "widget"
    (model / "parts").mkdir(parents=True)
    (model / "parts" / "body.scad").write_text(
        "include <shared.scad>\nuse <BOSL2/std.scad>\n", encoding="utf-8"
    )
    (model / "parts" / "shared.scad").write_text("", encoding="utf-8")

    result = report(tmp_path, "include <parts/body.scad>\n")

    assert [(e.file, e.target, e.status) for e in result.includes] == [
        ("model.scad", "parts/body.scad", "resolved"),
        ("parts/body.scad", "shared.scad", "resolved"),
        ("parts/body.scad", "BOSL2/std.scad", "unresolved"),
    ]
    assert result.includes[1].path == "parts/shared.scad"


def test_a_file_that_includes_itself_is_read_once(tmp_path: Path) -> None:
    model = tmp_path / "models" / "widget"
    model.mkdir(parents=True)
    (model / "loop.scad").write_text("include <loop.scad>\n", encoding="utf-8")

    result = report(tmp_path, "include <loop.scad>\n")

    assert len(result.includes) == 2
    assert not result.truncated


def test_following_stops_at_the_file_cap(tmp_path: Path) -> None:
    model = tmp_path / "models" / "widget"
    model.mkdir(parents=True)
    for index in range(MAX_FILES + 5):
        (model / f"f{index}.scad").write_text(f"include <f{index + 1}.scad>\n", encoding="utf-8")

    result = report(tmp_path, "include <f0.scad>\n")

    assert result.truncated


def test_a_library_file_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "libraries"
    checkout(root, "BOSL2", COMMIT, "std.scad")
    (root / "BOSL2" / COMMIT / "BOSL2" / "std.scad").write_text(
        "include <missing-inside.scad>\n", encoding="utf-8"
    )

    result = report(tmp_path, "use <BOSL2/std.scad>\n", [pin("BOSL2")])

    assert [e.target for e in result.includes] == ["BOSL2/std.scad"]


# ── fonts ─────────────────────────────────────────────────────────────────────


def test_a_font_family_fontconfig_does_not_have_is_reported_missing(tmp_path: Path) -> None:
    fonts = {normalise_family("DejaVu Sans")}
    source = 'text("a", font="DejaVu Sans:style=Bold");\ntext("b", font="Lobster:style=Bold");\n'

    result = report(tmp_path, source, fonts=fonts)

    assert [(f.font, f.missing) for f in result.fonts] == [
        ("DejaVu Sans:style=Bold", []),
        ("Lobster:style=Bold", ["Lobster"]),
    ]
    assert result.fonts_checked


def test_without_fontconfig_fonts_are_listed_but_not_judged(tmp_path: Path) -> None:
    result = report(tmp_path, 'font = "Lobster";\n', fonts=None)

    assert [(f.families, f.missing) for f in result.fonts] == [(["Lobster"], [])]
    assert not result.fonts_checked
