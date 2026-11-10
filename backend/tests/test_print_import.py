"""The printer path a print import may read (#4698 review)."""

import pytest
from pydantic import ValidationError

from scadbuddy.bambuddy.print_import import SourceChoice


@pytest.mark.parametrize(
    "path",
    ["/cache/a.3mf", "/a.3mf", "/c/x.gcode.3mf", "/.hidden/x.3mf", "/..a/x.3mf", "/cache/A.3MF"],
)
def test_an_absolute_3mf_path_is_taken(path: str) -> None:
    assert SourceChoice(kind="printer", path=path).path == path


@pytest.mark.parametrize(
    "path",
    ["/cache/../x.3mf", "/../x.3mf", "/./x.3mf", "/cache/./x.3mf", "cache/x.3mf", "/x.txt"],
)
def test_a_dot_segment_or_another_file_is_refused(path: str) -> None:
    with pytest.raises(ValidationError):
        SourceChoice(kind="printer", path=path)


def test_library_candidates_are_newest_first_and_undated_last() -> None:
    """#5979: a row with no date never takes a dated row's place under the cut."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from scadbuddy.bambuddy.print_import import _newest_first

    old = SimpleNamespace(id=1, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    new = SimpleNamespace(id=2, created_at=datetime(2026, 6, 1, tzinfo=UTC))
    undated = SimpleNamespace(id=3, created_at=None)
    rows = sorted([undated, old, new], key=_newest_first, reverse=True)  # type: ignore[arg-type]
    assert [row.id for row in rows] == [2, 1, 3]
