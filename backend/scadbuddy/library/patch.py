"""Patching a model's source: a unified diff, or search/replace edits (#252).

The agent's ``apply_patch`` tool sends one or the other instead of the whole file, so
an edit to three lines of a 600-line model costs three lines. Both are applied in
memory, to the source as it stands, and either apply completely or not at all: a hunk
whose lines are not there, or a search that is missing or ambiguous, is a
:class:`PatchError` naming which one, with nothing written.

Unified diff, as GNU diff and ``git diff`` write it
(https://www.gnu.org/software/diffutils/manual/html_node/Detailed-Unified.html). File
headers (``diff --git``, ``index``, a ``---`` line followed by a ``+++`` one) are
skipped, since the route already names the one file; a diff that names a second file
is refused rather than half-applied. After the first hunk a second file starts only at
a ``diff`` line, as ``git diff`` and ``diff -ru`` write one: a ``---`` then ``+++`` pair
there is a removed and an added line, even straight before the next ``@@``, since a
source line may begin ``--`` and the next hunk of the same file looks just like a
second file's first (review of #741). A second file pasted on with no ``diff`` line
still fails, as a hunk whose lines are not in the source, and that error says so.

The hunk header's line counts are not trusted, only its start line: each hunk is tried
where its header puts it, then, because a model writing a diff by hand often gets the
numbers wrong, at the one other place below the previous hunk where its old-side lines
occur. Two or more such places is
ambiguous and refused. No fuzz: a context line that differs is a conflict, not
something to guess past.

``\\ No newline at end of file`` follows the line it is about, on that line's side: after
a ``-`` line the old file ended there without a newline, after a ``+`` line the new one
does, after a context line both do
(https://www.gnu.org/software/diffutils/manual/html_node/Incomplete-Lines.html). So a
hunk carrying the marker must reach the end of the file, and it decides the result's
ending: a newline unless its new side has the marker. An old-side marker against a
source that does end in a newline is a conflict. A diff with no marker at all, as one
written by hand often has, keeps the source's own ending (review of #741).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

#: A bound on edits per call, so one request cannot turn into a long scan.
MAX_EDITS = 100
#: The same bound on a diff's hunks: each one that misses its stated line scans the
#: rest of the source (review of #741).
MAX_HUNKS = 100

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE_HEADERS = ("index ", "new file mode", "deleted file mode", "similarity ")


class PatchError(ValueError):
    """The patch does not apply to the source as it stands."""


#: Each search and replace text, as long as a whole source may be (``MAX_SOURCE_CHARS``
#: in api/models.py), so the request declares its own bound (review of #741).
MAX_EDIT_CHARS = 1_000_000


class SearchReplace(BaseModel):
    search: str = Field(
        min_length=1,
        max_length=MAX_EDIT_CHARS,
        description="Text that occurs exactly once in the source",
    )
    replace: str = Field(max_length=MAX_EDIT_CHARS, description="What it becomes")


@dataclass
class _Hunk:
    number: int
    old_start: int
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    # "\ No newline at end of file" after that side's last line.
    old_unterminated: bool = False
    new_unterminated: bool = False
    # Ends in a `---` then `+++` pair straight before the next `@@`: a second file's
    # header, if the hunk does not apply.
    ends_like_a_header: bool = False


def apply_edits(source: str, edits: list[SearchReplace]) -> str:
    """Each edit in turn, against the source the previous ones left."""
    if not edits:
        raise PatchError("no edits given")
    if len(edits) > MAX_EDITS:
        raise PatchError(f"at most {MAX_EDITS} edits per patch; got {len(edits)}")
    for index, edit in enumerate(edits, start=1):
        count = source.count(edit.search)
        if count == 0:
            raise PatchError(f"edit {index}: its search text is not in the source")
        if count > 1:
            raise PatchError(
                f"edit {index}: its search text occurs {count} times; "
                "include more of the surrounding lines so it names one"
            )
        source = source.replace(edit.search, edit.replace, 1)
    return source


def _parse(diff: str) -> list[_Hunk]:
    hunks: list[_Hunk] = []
    files = 0
    # A `diff` line opened this file, so its `---`/`+++` pair is not another one.
    opened = False
    current: _Hunk | None = None
    lines = diff.splitlines()
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        after = lines[index + 2] if index + 2 < len(lines) else ""
        # No hunk line starts with "diff ", so this one starts a file wherever it is.
        if line.startswith("diff "):
            files += 1
            if files > 1:
                raise PatchError("the diff changes more than one file; send one per file")
            current, opened = None, True
            continue
        header_like = line.startswith("--- ") and following.startswith("+++ ")
        if header_like and current is None:
            if not opened:
                files += 1
                if files > 1:
                    raise PatchError("the diff changes more than one file; send one per file")
            opened = False
            continue
        if header_like and current is not None and _HUNK.match(after):
            current.ends_like_a_header = True
        if current is None and line.startswith(("+++ ", *_FILE_HEADERS)):
            continue
        header = _HUNK.match(line)
        if header:
            if len(hunks) == MAX_HUNKS:
                raise PatchError(f"at most {MAX_HUNKS} hunks per patch")
            current = _Hunk(number=len(hunks) + 1, old_start=int(header.group(1)))
            hunks.append(current)
            continue
        if current is None:
            if line.strip():
                raise PatchError(f"not a unified diff: {line[:80]!r} comes before any @@ hunk")
            continue
        if line.startswith("\\"):
            # "\ No newline at end of file", about the line before it.
            before = lines[index - 1]
            side = before[:1] or " "
            if _HUNK.match(before) or side not in " -+":
                raise PatchError(f"hunk {current.number}: {line[:80]!r} follows no line")
            current.old_unterminated |= side in " -"
            current.new_unterminated |= side in " +"
            continue
        marker, body = (line[:1], line[1:]) if line else (" ", "")
        if (marker in " -" and current.old_unterminated) or (
            marker in " +" and current.new_unterminated
        ):
            raise PatchError(
                f"hunk {current.number}: {line[:80]!r} comes after that side's "
                '"No newline at end of file"'
            )
        if marker == " ":
            current.old.append(body)
            current.new.append(body)
        elif marker == "-":
            current.old.append(body)
        elif marker == "+":
            current.new.append(body)
        else:
            raise PatchError(f"hunk {current.number}: unexpected line {line[:80]!r}")
    if not hunks:
        raise PatchError("the diff has no @@ hunks")
    return hunks


def _occurrences(lines: list[str], block: list[str], start: int) -> list[int]:
    """Where ``block`` starts at or below ``start``: the first two places at most, which
    is all the caller needs to tell one from ambiguous (review of #741)."""
    width = len(block)
    first = block[0]
    found: list[int] = []
    for i in range(start, len(lines) - width + 1):
        if lines[i] == first and lines[i : i + width] == block:
            found.append(i)
            if len(found) == 2:
                break
    return found


def apply_unified_diff(source: str, diff: str) -> str:
    """``diff`` applied to ``source``, hunk by hunk, top to bottom."""
    trailing_newline = source.endswith("\n")
    lines = source.splitlines()
    # How far the source has moved, against the diff's stated line numbers, under the
    # hunks applied so far; and the first line the next hunk may touch.
    offset = 0
    floor = 0
    hunks = _parse(diff)
    for hunk in hunks:
        # `-N,0` is a pure insertion AFTER line N; otherwise the old side starts at N.
        stated = hunk.old_start if not hunk.old else hunk.old_start - 1
        at = stated + offset
        if hunk.old and (at < floor or lines[at : at + len(hunk.old)] != hunk.old):
            found = _occurrences(lines, hunk.old, floor)
            if not found:
                hint = (
                    "; if its last two lines are a second file's header, send one file per patch"
                    if hunk.ends_like_a_header
                    else ""
                )
                raise PatchError(
                    f"hunk {hunk.number} (at line {hunk.old_start}): its context and removed "
                    f"lines are not in the source; read it again and rebuild the patch{hint}"
                )
            if len(found) > 1:
                raise PatchError(
                    f"hunk {hunk.number}: its lines are not at line {hunk.old_start} and occur "
                    "more than once below the previous hunk; add context so it names one"
                )
            at = found[0]
        if not floor <= at <= len(lines):
            raise PatchError(
                f"hunk {hunk.number}: line {hunk.old_start} is out of order or past the end"
            )
        if hunk.old_unterminated or hunk.new_unterminated:
            if hunk is not hunks[-1] or at + len(hunk.old) != len(lines):
                raise PatchError(
                    f"hunk {hunk.number}: it says the file ends without a newline "
                    "but does not reach the end of the file"
                )
            if hunk.old_unterminated and trailing_newline:
                raise PatchError(
                    f"hunk {hunk.number}: it says the source ends without a newline, "
                    "but it ends with one; read it again and rebuild the patch"
                )
            trailing_newline = not hunk.new_unterminated
        lines[at : at + len(hunk.old)] = hunk.new
        floor = at + len(hunk.new)
        offset = at - stated + len(hunk.new) - len(hunk.old)
    patched = "\n".join(lines)
    return patched + "\n" if trailing_newline and lines else patched
