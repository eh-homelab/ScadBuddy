"""Files the source editor opens read-only beside the model (#185).

Go-to-definition can land in a model's sibling file (an ``include``/``use`` target)
or in a library on its ``OPENSCADPATH``. The editor has no copy of either, so it
reads the one file it jumps into through here, confined to one root: the model's
directory, or one pinned library checkout.

A path is relative, ``/``-separated and plain: no empty, ``.`` or ``..`` segment and
no dot-file anywhere along it (``.git``, a model's ``.renders``), so nothing outside
the files a model ships is reachable. The file it names must resolve, symlinks
followed, to a regular file still under the root, and be UTF-8 text no larger than
the caller's limit (the API's ``MAX_SOURCE_CHARS``, the cap on a model's own source).
"""

from __future__ import annotations

from pathlib import Path

#: Longer than any path a library ships, short enough to refuse before touching disk.
MAX_PATH_LENGTH = 1024


class FilePathError(ValueError):
    """Not a plain relative path under the root."""


class FileTooLargeError(ValueError):
    """Larger than the limit."""


class NotTextError(ValueError):
    """Not UTF-8 text: a binary file."""


def _segments(relative: str) -> list[str]:
    if not relative or len(relative) > MAX_PATH_LENGTH:
        raise FilePathError("the path is empty or too long")
    if "\\" in relative or "\0" in relative:
        raise FilePathError("the path has a character a file path here never has")
    segments = relative.split("/")
    for segment in segments:
        # An empty first segment is an absolute path; `..` and `.` start with a dot.
        if not segment or segment.startswith("."):
            raise FilePathError(f"{relative!r} is not a plain path under the directory")
    return segments


def read_text_file(root: Path, relative: str, *, limit: int) -> str:
    """The text of ``relative`` under ``root``, at most ``limit`` bytes.

    Raises :class:`FilePathError` for a path that is not plain, ``FileNotFoundError``
    for one that is missing, not a regular file, or resolves outside ``root`` (a
    symlink out of it reads as missing, so it says nothing about what is outside),
    :class:`FileTooLargeError` and :class:`NotTextError`.
    """
    segments = _segments(relative)
    base = root.resolve(strict=True)
    target = base.joinpath(*segments).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise FileNotFoundError(relative)
    if target.stat().st_size > limit:
        raise FileTooLargeError(relative)
    with target.open("rb") as file:
        # One byte past the cap: a file that grew since the stat is still refused.
        data = file.read(limit + 1)
    if len(data) > limit:
        raise FileTooLargeError(relative)
    if b"\0" in data:
        raise NotTextError(relative)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise NotTextError(relative) from None
