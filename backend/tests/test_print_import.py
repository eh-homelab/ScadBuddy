"""The printer path a print import may read (#4698 review)."""

import asyncio

import httpx
import pytest
import respx
from pydantic import ValidationError

from scadbuddy.bambuddy.print_import import SourceChoice
from scadbuddy.core.problems import ApiError


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
        ("running", False, "operation op-1, `GET /api/v1/operations/op-1`"),
        ("running", False, "if it never finishes, send the request with a new Idempotency-Key"),
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


# --- a mesh model from a print's file (#6916) ----------------------------------------


def test_a_mesh_model_names_one_source_and_a_print_with_its_place() -> None:
    from scadbuddy.api.mesh_models import MeshModelCreate

    place = SourceChoice(kind="archive_source")
    made = MeshModelCreate(name="T", archive_id=193, archive_source=place)
    assert (made.archive_id, made.archive_source) == (193, place)
    wrong: list[dict[str, object]] = [
        {"archive_id": 193},
        {"archive_source": place},
        {"archive_id": 193, "archive_source": place, "library_file_id": 7},
        {"archive_id": 193, "archive_source": place, "output_id": "a" * 32},
        {},
    ]
    for body in wrong:
        with pytest.raises(ValidationError):
            MeshModelCreate.model_validate({"name": "T", **body})


class _Store:
    """A settings store that holds a Bambuddy and nothing else."""

    def load(self) -> object:
        from scadbuddy.library.settings_store import StoredSettings
        from tests.api.test_send import BASE

        return StoredSettings(bambuddy_url=BASE, bambuddy_api_key="s3cret")


@respx.mock
def test_a_print_s_sliced_file_is_refused_before_it_is_read() -> None:
    from scadbuddy.api.mesh_models import _read_print_file
    from tests.api.test_print_imports import ARCHIVE, bambuddy

    bambuddy()

    with pytest.raises(ApiError) as refused:
        asyncio.run(_read_print_file(_Store(), ARCHIVE, SourceChoice(kind="archive")))  # type: ignore[arg-type]

    assert refused.value.status == 422
    assert "sliced file" in refused.value.detail
    assert not [call for call in respx.calls if "/download" in call.request.url.path]


@respx.mock
def test_a_print_s_file_on_the_printer_is_read_and_not_kept() -> None:
    from scadbuddy.api.mesh_models import _read_print_file
    from tests.api.test_print_imports import ARCHIVE, a_3mf, bambuddy
    from tests.api.test_send import API

    bambuddy(
        printer_files={
            "path": "/cache",
            "files": [{"name": "demo_elan.3mf", "path": "/cache/demo_elan.3mf", "size": 10}],
            "warnings": [],
        }
    )
    data = a_3mf()
    download = respx.get(
        f"{API}/printers/1/files/download", params={"path": "/cache/demo_elan.3mf"}
    ).mock(return_value=httpx.Response(200, content=data))
    upload = respx.post(f"{API}/library/files")

    read = asyncio.run(
        _read_print_file(
            _Store(),  # type: ignore[arg-type]
            ARCHIVE,
            SourceChoice(kind="printer", path="/cache/demo_elan.3mf"),
        )
    )

    assert (read.name, read.data) == ("demo_elan.3mf", data)
    assert download.called and not upload.called
