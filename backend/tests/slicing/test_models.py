import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from scadbuddy.slicing.models import (
    SlicePlateInput,
    SlicePlateOutput,
    SliceProgress,
    TransferUrl,
)
from scadbuddy.store.content_models import SWEPT_KINDS

VECTORS = Path(__file__).parents[3] / "workers/rust/vectors/slice_plate.json"
AT = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _get(name: str) -> TransferUrl:
    return TransferUrl(
        url=f"http://api/api/v1/internal/blobs/slice/{name}",
        headers={"X-ScadBuddy-Transfer": "tok"},
        expires_at=AT,
    )


def _put(name: str) -> TransferUrl:
    return TransferUrl(
        url=f"http://garage/scadbuddy/prod/slice/{name}?X-Amz-Signature=sig",
        expires_at=AT,
    )


def test_slice_is_a_swept_kind() -> None:
    assert "slice" in SWEPT_KINDS


def test_vectors_round_trip() -> None:
    cases: dict[str, list[SlicePlateInput] | list[SliceProgress] | list[SlicePlateOutput]] = {
        "input": [
            SlicePlateInput(
                input_3mf=_get("in"),
                plate=1,
                machine_preset=_get("m"),
                process_preset=_get("p"),
                filament_presets=[_get("f1"), _get("f2")],
                sliced_3mf=_put("out"),
            ),
            SlicePlateInput(
                input_3mf=_get("in"),
                plate=0,
                machine_preset=_get("m"),
                process_preset=_get("p"),
                filament_presets=[_get("f1")],
                sliced_3mf=_put("out"),
                slicedata=_put("data"),
                arrange="auto",
                allow_newer_file=False,
                debug=3,
                filament_colours=["#FF0000"],
            ),
        ],
        "progress": [
            SliceProgress(
                plate_index=0,
                plate_count=2,
                plate_percent=50,
                total_percent=25,
                message="Slicing",
            )
        ],
        "output": [
            SlicePlateOutput(
                sliced_sha256="a" * 64,
                sliced_size=1234,
                warnings=["w"],
                exit_code=0,
                elapsed_seconds=12.5,
            ),
            SlicePlateOutput(
                sliced_sha256="b" * 64,
                sliced_size=1,
                slicedata_sha256="c" * 64,
                slicedata_size=9,
                exit_code=0,
                stderr_tail="tail",
                elapsed_seconds=0.5,
            ),
        ],
    }
    dumped = {k: [m.model_dump(mode="json") for m in v] for k, v in cases.items()}
    if os.environ.get("UPDATE_SLICE_VECTORS"):
        VECTORS.write_text(json.dumps(dumped, indent=2, sort_keys=True) + "\n")
    assert json.loads(VECTORS.read_text()) == dumped
    for cls, key in (
        (SlicePlateInput, "input"),
        (SliceProgress, "progress"),
        (SlicePlateOutput, "output"),
    ):
        for raw in dumped[key]:
            assert cls.model_validate(raw).model_dump(mode="json") == raw


def test_expires_at_is_normalised_to_utc() -> None:
    url = TransferUrl(
        url="http://api/x",
        expires_at=datetime(2026, 10, 16, 14, 0, tzinfo=timezone(timedelta(hours=2))),
    )
    assert url.model_dump(mode="json")["expires_at"] == "2026-10-16T12:00:00Z"


SECRET_URL = TransferUrl(
    url="https://garage.example:3900/bucket/slice/x?X-Amz-Signature=presigned-sig",
    headers={"X-ScadBuddy-Transfer": "bearer-token"},
    expires_at=AT,
)


def test_a_transfer_url_never_prints_its_secrets() -> None:
    for text in (repr(SECRET_URL), str(SECRET_URL), f"{SECRET_URL}", repr([SECRET_URL])):
        assert "presigned-sig" not in text
        assert "bearer-token" not in text
        assert "/bucket" not in text
        assert "https://garage.example" in text
        assert "X-ScadBuddy-Transfer" in text
    assert "presigned-sig" in SECRET_URL.url  # the value itself is untouched


def test_a_slice_input_never_prints_its_urls_secrets() -> None:
    raw = SlicePlateInput(
        input_3mf=SECRET_URL,
        plate=1,
        machine_preset=SECRET_URL,
        process_preset=SECRET_URL,
        filament_presets=[SECRET_URL],
        sliced_3mf=SECRET_URL,
    )
    assert "bearer-token" not in repr(raw)
    assert "presigned-sig" not in repr(raw)
    dumped = raw.model_dump(mode="json")
    assert dumped["input_3mf"]["headers"] == {"X-ScadBuddy-Transfer": "bearer-token"}


def test_a_validation_error_never_carries_a_token() -> None:
    url = SECRET_URL.model_dump(mode="json")
    for cls, payload in (
        (TransferUrl, {**url, "expires_at": "not a time"}),
        (TransferUrl, {"url": url["url"], "headers": url["headers"]}),
        (SlicePlateInput, {"input_3mf": {"url": url["url"], "headers": url["headers"]}}),
        (SlicePlateInput, {"input_3mf": url, "plate": -1, "sliced_3mf": [url]}),
    ):
        with pytest.raises(ValidationError) as caught:
            cls.model_validate(payload)
        message = str(caught.value)
        assert "bearer-token" not in message
        assert "presigned-sig" not in message
