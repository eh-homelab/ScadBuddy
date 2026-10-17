import json
import os
from datetime import UTC, datetime
from pathlib import Path

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
