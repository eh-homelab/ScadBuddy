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
    from datetime import datetime

    from scadbuddy.bambuddy.models import LibraryListRow
    from scadbuddy.bambuddy.print_import import _newest_first

    def row(file_id: int, created_at: datetime | None) -> LibraryListRow:
        return LibraryListRow(
            id=file_id, filename=f"{file_id}.3mf", file_type="3mf", created_at=created_at
        )

    rows = sorted(
        [row(3, None), row(1, datetime(2026, 1, 1)), row(2, datetime(2026, 6, 1))],
        key=_newest_first,
        reverse=True,
    )
    assert [r.id for r in rows] == [2, 1, 3]


@pytest.mark.parametrize(
    ("status", "made", "says"),
    [
        ("succeeded", True, "no longer the file the model 'trophy' was made from"),
        ("succeeded", False, "made the model 'trophy', which was deleted since"),
        ("running", False, "is still being made (operation op-1, `GET /api/v1/operations/op-1`)"),
        ("failed", False, "made no model: its operation op-1 failed"),
    ],
)
def test_a_changed_retry_says_what_its_first_send_did(status: str, made: bool, says: str) -> None:
    """#5979 review: the 409 names a model only by what the key's operation did."""
    from datetime import UTC, datetime

    from scadbuddy.api.print_imports import _source_changed
    from scadbuddy.operations.store import Operation

    first = Operation(
        id="op-1",
        kind="mesh_model_create",
        subject="trophy",
        status=status,  # type: ignore[arg-type]
        created_at=datetime(2026, 11, 10, tzinfo=UTC),
    )
    assert says in _source_changed("x.3mf", "trophy", first, made)
