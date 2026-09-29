"""What a model's ``include <…>`` and ``use <…>`` statements resolve to (#253).

OpenSCAD only WARNs on a file it cannot find ("Can't open library", "Can't find include
file") and renders without it, so a model missing a library comes out with part of its
geometry gone. This answers the question before a render does, the way OpenSCAD itself
resolves each target:

- **Finding the statements** follows OpenSCAD's lexer (``src/core/lexer.l``,
  https://github.com/openscad/openscad/blob/master/src/core/lexer.l):
  ``include[ \\t\\r\\n]*"<"`` and ``use[ \\t\\r\\n]*"<"``, then everything up to ``>``
  that is not a tab or line break. Nothing inside a comment or a string is one.
- **Resolving a target** follows ``find_valid_path`` (``src/core/parsersettings.cc``,
  same repository): an absolute path as it is; otherwise the including file's own
  directory first, then each ``OPENSCADPATH`` directory in order, the first regular
  file winning. A render's ``OPENSCADPATH`` is exactly the model's pinned checkouts
  (``render/runner.py`` `run_openscad`, #93), so those are what is searched here.
  OpenSCAD's built-in and user library directories come after them; they are not
  searched here, because nothing a model may depend on lives there (#93).
- **Nested statements** in a file of the model's own directory are followed, relative
  to that file's directory as OpenSCAD reads them. Files inside a library are the
  library's business and are not.
- **Nothing outside the model's directory and its checkouts is looked at.** OpenSCAD
  would open ``../../x.scad`` as it opens an absolute path; here either is unresolved
  without asking whether the file exists, so a report cannot say what is on the
  container's filesystem (review of #740). A symbolic link counts where it leads.
- **A report is bounded**: at most :data:`MAX_STATEMENTS` statements, and
  :data:`MAX_SUGGESTIONS` library names looked up for a suggestion, whatever the source
  packs in (review of #740). The rest is ``truncated``.

Nothing is fetched and nothing is written: a pin whose checkout is not on the volume is
reported as such (a render would clone it again, ``CheckoutFetcher``), not cloned.

Fonts are reported beside the includes: every ``font = "…"`` string literal, with the
families it names that fontconfig does not resolve. OpenSCAD renders those in the
default font without a word (library/fonts.py). A literal is all this can see; a font
computed at run time is not reported.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.core.paths import SOURCE_NAME
from scadbuddy.library.fonts import font_families, normalise_family
from scadbuddy.library.libraries import CatalogueLibrary, ModelLibrary, same_repository

#: Files of the model's own directory followed, at most. A model reads a handful; a
#: cap keeps a pathological tree from turning a read into a walk of the volume.
MAX_FILES = 64
#: A nested file larger than this is not read (the source limit on a model is far below).
MAX_FILE_BYTES = 2 * 1024 * 1024
#: ``include``/``use`` statements resolved per report, across every file followed. Each
#: costs a stat per search directory, and ``use<a/x.scad>use<b/x.scad>…`` packs tens
#: of thousands into a source within the route's limit (review of #740).
MAX_STATEMENTS = 512
#: Distinct unpinned library names given a suggestion per report.
MAX_SUGGESTIONS = 32

# lexer.l: `include[ \t\r\n]*"<"` then `[^\t\r\n>]*` up to `>`.
_STATEMENT = re.compile(r"(include|use)[ \t\r\n]*<([^\t\r\n>]*)>")
# `font = "`, the start of a string literal assigned to a `font` argument or variable.
_FONT = re.compile(r"font[ \t\r\n]*=[ \t\r\n]*\"")
# OpenSCAD identifiers are [A-Za-z0-9_$] (lexer.l); a keyword preceded by one is not one.
_IDENTIFIER = re.compile(r"[A-Za-z0-9_$]")
_STRING_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}

Kind = Literal["include", "use"]


@dataclass(frozen=True)
class Statement:
    kind: Kind
    target: str
    line: int


@dataclass(frozen=True)
class FontLiteral:
    value: str
    line: int


def _string_at(source: str, start: int) -> tuple[str, int]:
    """The string literal whose opening quote is at ``start - 1``: its value (escapes
    undone the way OpenSCAD's ``cond_string`` does for the common ones) and the index
    just past its closing quote, or the end of the source when it is unterminated."""
    out: list[str] = []
    index = start
    while index < len(source):
        char = source[index]
        if char == "\\" and index + 1 < len(source):
            out.append(_STRING_ESCAPES.get(source[index + 1], source[index + 1]))
            index += 2
            continue
        if char == '"':
            return "".join(out), index + 1
        out.append(char)
        index += 1
    return "".join(out), index


def scan(source: str) -> tuple[list[Statement], list[FontLiteral]]:
    """The ``include``/``use`` statements and ``font = "…"`` literals of ``source``,
    skipping comments and strings as OpenSCAD's lexer does."""
    statements: list[Statement] = []
    fonts: list[FontLiteral] = []
    index = 0
    line = 1
    length = len(source)

    def advance(to: int) -> None:
        nonlocal index, line
        line += source.count("\n", index, to)
        index = to

    while index < length:
        char = source[index]
        if source.startswith("//", index):
            end = source.find("\n", index)
            advance(length if end == -1 else end)
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            advance(length if end == -1 else end + 2)
            continue
        if char == '"':
            _, end = _string_at(source, index + 1)
            advance(end)
            continue
        if char in "iuf" and (index == 0 or not _IDENTIFIER.match(source[index - 1])):
            statement = _STATEMENT.match(source, index)
            if statement is not None:
                kind: Kind = "include" if statement.group(1) == "include" else "use"
                statements.append(Statement(kind=kind, target=statement.group(2), line=line))
                advance(statement.end())
                continue
            font = _FONT.match(source, index)
            if font is not None:
                value, end = _string_at(source, font.end())
                fonts.append(FontLiteral(value=value, line=line))
                advance(end)
                continue
        advance(index + 1)
    return statements, fonts


# ── the report ────────────────────────────────────────────────────────────────


class LibrarySuggestion(BaseModel):
    """A library that would provide an unresolved target once pinned to this model."""

    name: str = Field(description="The library to pin, as `PUT /models/{slug}/libraries/{name}`")
    source: Literal["catalogue", "installed"] = Field(
        description="`catalogue`: a curated library (`GET /libraries`); `installed`: a "
        "checkout another model pins, from a URL outside the catalogue"
    )
    url: str
    ref: str = Field(description="The catalogue's ref, or the ref the other model pins")
    commit: str | None = Field(
        default=None, description="The checkout on the volume that was looked in, if any"
    )
    has_file: bool | None = Field(
        default=None,
        description="Whether that checkout has the target; null when none is on the volume",
    )
    pinned_by: str | None = Field(
        default=None, description="For `installed`: the model whose pin this is"
    )


class IncludeTarget(BaseModel):
    file: str = Field(description="The model file the statement is in, relative to its directory")
    line: int
    kind: Kind
    target: str = Field(description="What is between the angle brackets")
    status: Literal["resolved", "unresolved"]
    path: str | None = Field(
        default=None,
        description="What it resolved to: relative to the model's directory, or "
        "`<library>/<path>` inside a pinned library; null when unresolved",
    )
    library: str | None = Field(
        default=None, description="The pinned library it resolved into, if it did"
    )
    reason: str | None = Field(default=None, description="Why it is unresolved")
    suggestion: LibrarySuggestion | None = None


class FontUse(BaseModel):
    file: str
    line: int
    font: str = Field(description='The string literal, e.g. "Liberation Sans:style=Bold"')
    families: list[str] = Field(description="The families it names, as fontconfig reads it")
    missing: list[str] = Field(
        description="Those fontconfig does not resolve: a render draws them in the "
        "default font instead. Empty when fontconfig could not be asked"
    )


class DependencyReport(BaseModel):
    includes: list[IncludeTarget] = Field(default_factory=list)
    unresolved: int = Field(description="How many of `includes` are unresolved")
    fonts: list[FontUse] = Field(default_factory=list)
    fonts_checked: bool = Field(
        description="False when fontconfig was not available to ask, so `missing` is empty"
    )
    missing_checkouts: list[str] = Field(
        default_factory=list,
        description="Pinned libraries whose checkout is not on the volume; a render "
        "clones each again, but nothing inside one can be resolved here until then",
    )
    truncated: bool = Field(
        default=False,
        description=f"True when the report stops short: more than {MAX_FILES} files of "
        f"the model to follow, more than {MAX_STATEMENTS} include/use statements (the "
        f"rest are not listed), or more than {MAX_SUGGESTIONS} unpinned library names "
        "to suggest a library for (the rest have no `suggestion`)",
    )


@dataclass(frozen=True)
class Candidates:
    """Where unresolved targets could come from, gathered by the caller."""

    catalogue: Sequence[CatalogueLibrary]
    #: library name -> (slug, pin) of every model pinning a library of that name. It
    #: reads every model.json, so a report calls it once at most, and only when a
    #: suggestion is looked for (review of #740).
    pin_index: Callable[[], Mapping[str, Sequence[tuple[str, ModelLibrary]]]]


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _within(path: Path, root: Path) -> Path | None:
    """``path`` relative to ``root`` once both are resolved, or None outside it."""
    try:
        return path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None


def _inside(base: Path, target: str, root: Path) -> Path | None:
    """``base / target`` when it stays within ``root``, both written out and with every
    symbolic link followed; None otherwise. Whether a file outside ``root`` exists is
    never asked: it is not this report's to say (review of #740)."""
    joined = base / target
    # Written out first, so ``../../etc/passwd`` is refused without a filesystem call.
    if not Path(os.path.normpath(joined)).is_relative_to(os.path.normpath(root)):
        return None
    return joined if _within(joined, root) is not None else None


def resolve_dependencies(
    model_dir: Path,
    source: str,
    pins: Sequence[ModelLibrary],
    *,
    libraries_root: Path,
    candidates: Candidates,
    resolvable_fonts: set[str] | None,
) -> DependencyReport:
    """The report for ``source`` read as ``model_dir``'s ``model.scad``, against the
    model's own ``pins``, with checkouts under ``libraries_root``."""
    # One OPENSCADPATH entry per pin, in order, as libraries.search_path builds it.
    search: list[tuple[ModelLibrary, Path]] = []
    missing_checkouts: list[str] = []
    for pin in pins:
        directory = libraries_root / pin.name / pin.commit
        if (directory / pin.name).is_dir():
            search.append((pin, directory))
        else:
            missing_checkouts.append(pin.name)
    pinned = {pin.name: pin for pin in pins}
    truncated = False
    # The index reads every model.json: built once, the first time a name needs it.
    index: list[Mapping[str, Sequence[tuple[str, ModelLibrary]]]] = []

    def pins_of(name: str) -> Sequence[tuple[str, ModelLibrary]]:
        if not index:
            index.append(candidates.pin_index())
        return index[0].get(name, ())

    heads: set[str] = set()
    suggestions: dict[str, LibrarySuggestion | None] = {}

    def suggest(target: str) -> LibrarySuggestion | None:
        nonlocal truncated
        head = target.partition("/")[0]
        if head not in heads:
            if len(heads) >= MAX_SUGGESTIONS:
                truncated = True
                return None
            heads.add(head)
        if target not in suggestions:
            suggestions[target] = _suggest(target, candidates.catalogue, pins_of, libraries_root)
        return suggestions[target]

    includes: list[IncludeTarget] = []
    fonts: list[FontUse] = []
    queue: list[tuple[str, Path, str]] = [(SOURCE_NAME, model_dir, source)]
    seen: set[Path] = {(model_dir / SOURCE_NAME).resolve()}
    while queue:
        name, directory, text = queue.pop(0)
        statements, literals = scan(text)
        for literal in literals:
            families = font_families(literal.value)
            missing = (
                []
                if resolvable_fonts is None
                else [f for f in families if normalise_family(f) not in resolvable_fonts]
            )
            fonts.append(
                FontUse(
                    file=name,
                    line=literal.line,
                    font=literal.value,
                    families=families,
                    missing=missing,
                )
            )
        for statement in statements:
            if len(includes) >= MAX_STATEMENTS:
                truncated = True
                break
            entry, followed = _resolve(
                statement, name, directory, model_dir, search, pinned, missing_checkouts
            )
            if entry.status == "unresolved" and _names_a_library(statement.target, pinned):
                entry.suggestion = suggest(statement.target)
            includes.append(entry)
            if followed is None or not followed.name.endswith(".scad"):
                continue
            key = followed.resolve()
            if key in seen:
                continue
            if len(seen) >= MAX_FILES:
                truncated = True
                continue
            seen.add(key)
            try:
                if followed.stat().st_size > MAX_FILE_BYTES:
                    continue
                nested = followed.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            queue.append((entry.path or followed.name, followed.parent, nested))

    return DependencyReport(
        includes=includes,
        unresolved=sum(1 for entry in includes if entry.status == "unresolved"),
        fonts=fonts,
        fonts_checked=resolvable_fonts is not None,
        missing_checkouts=missing_checkouts,
        truncated=truncated,
    )


def _resolve(
    statement: Statement,
    file: str,
    directory: Path,
    model_dir: Path,
    search: Sequence[tuple[ModelLibrary, Path]],
    pinned: dict[str, ModelLibrary],
    missing_checkouts: Sequence[str],
) -> tuple[IncludeTarget, Path | None]:
    """One statement's entry, and the model file to follow into when it is one."""
    target = statement.target

    def entry(
        status: Literal["resolved", "unresolved"],
        *,
        path: Path | None = None,
        library: str | None = None,
        reason: str | None = None,
    ) -> IncludeTarget:
        return IncludeTarget(
            file=file,
            line=statement.line,
            kind=statement.kind,
            target=target,
            status=status,
            path=path.as_posix() if path is not None else None,
            library=library,
            reason=reason,
        )

    if not target.strip():
        return entry("unresolved", reason="the file name is empty"), None
    if Path(target).is_absolute():
        # OpenSCAD reads an absolute path as it is; a model that depends on one does
        # not render the same anywhere else, and it is nothing this can vouch for.
        reason = "an absolute path; a model can only rely on files beside it and in its libraries"
        return entry("unresolved", reason=reason), None

    beside = _inside(directory, target, model_dir)
    if beside is None:
        # The file naming it is inside the model's directory, so a target that leaves
        # that directory leaves every checkout too. Refused like an absolute path,
        # before anything is asked of the filesystem about it (review of #740).
        reason = (
            "outside the model's directory; a model can only rely on files beside it "
            "and in its libraries"
        )
        return entry("unresolved", reason=reason), None
    if _is_file(beside):
        return entry("resolved", path=_within(beside, model_dir)), beside

    for pin, root in search:
        candidate = _inside(root, target, root)
        if candidate is not None and _is_file(candidate):
            return entry("resolved", path=_within(candidate, root), library=pin.name), None

    head, _, rest = target.partition("/")
    if rest and head in pinned:
        pin = pinned[head]
        if head in missing_checkouts:
            reason = (
                f"{head} is pinned at {pin.ref} ({pin.commit[:7]}), but that checkout is not "
                "on this volume, so its files cannot be looked at; a render clones it again"
            )
        else:
            reason = f"{head} is pinned at {pin.ref} ({pin.commit[:7]}), which has no {rest}"
        return entry("unresolved", reason=reason), None
    where = "beside the file that names it"
    if pinned:
        where += f" or in the libraries the model pins ({', '.join(pinned)})"
    else:
        where += ", and the model pins no libraries"
    return entry("unresolved", reason=f"no {target} {where}"), None


def _checkouts(libraries_root: Path, name: str) -> Iterator[tuple[str, Path]]:
    library = libraries_root / name
    if not library.is_dir():
        return
    for checkout in sorted(library.iterdir()):
        if (checkout / name).is_dir():
            yield checkout.name, checkout


def _names_a_library(target: str, pinned: Mapping[str, ModelLibrary]) -> bool:
    """Whether ``target`` names a library by its first path component that this model
    does not pin (``use <BOSL2/std.scad>`` names BOSL2, as ``use <NAME/...>`` always
    does, #93), and still names it once written out: ``BOSL2/../../x`` does not."""
    head, _, rest = target.partition("/")
    if not rest or head in pinned or head in ("", ".", ".."):
        return False
    written = Path(os.path.normpath(target)).parts
    return len(written) > 1 and written[0] == head


def _suggest(
    target: str,
    catalogue: Sequence[CatalogueLibrary],
    pins_of: Callable[[str], Sequence[tuple[str, ModelLibrary]]],
    libraries_root: Path,
) -> LibrarySuggestion | None:
    """For a target that :func:`_names_a_library`: the curated library of that name,
    else one another model pins from its own URL."""
    head = target.partition("/")[0]
    pins = list(pins_of(head))
    on_volume = dict(_checkouts(libraries_root, head))

    def has(commit: str) -> bool | None:
        root = on_volume.get(commit)
        if root is None:
            return None
        found = _inside(root, target, root)
        return found is not None and _is_file(found)

    curated = next((entry for entry in catalogue if entry.name == head), None)
    if curated is not None:
        # A checkout of the catalogue's own repository at the catalogue's ref, when
        # another model pins one, says whether the target is really in it.
        same = next(
            (
                pin
                for _, pin in pins
                if pin.ref == curated.ref
                and same_repository(pin.url, curated.url)
                and pin.commit in on_volume
            ),
            None,
        )
        return LibrarySuggestion(
            name=head,
            source="catalogue",
            url=curated.url,
            ref=curated.ref,
            commit=same.commit if same else None,
            has_file=has(same.commit) if same else None,
        )
    ranked = sorted(pins, key=lambda item: (has(item[1].commit) is not True, item[0]))
    if not ranked:
        return None
    slug, pin = ranked[0]
    return LibrarySuggestion(
        name=head,
        source="installed",
        url=pin.url,
        ref=pin.ref,
        commit=pin.commit,
        has_file=has(pin.commit),
        pinned_by=slug,
    )
