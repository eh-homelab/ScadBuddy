"""#7216: saved label sheet grids (`POST`, `PUT`, `DELETE /filament/label-sheets`, the
`saved` list of `GET /filament/label-templates`, and `template: "saved"` in `POST
/filament/labels`; plan `docs/superpowers/plans/2026-11-25-label-outputs.md` R-7216-11 to
R-7216-13)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.filament import NOT_NUDGED
from scadbuddy.filament.component import FILAMENT
from scadbuddy.filament.label_sheets import LabelSheetIn, SheetLayout

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament"
SHEETS = f"{URL}/label-sheets"
PDF = f"{URL}/labels"
SETTINGS = "/api/v1/settings"
MM = 72 / 25.4

AVERY_5163 = dict(
    page="letter",
    width_mm=101.6,
    height_mm=50.8,
    cols=2,
    rows=5,
    top_mm=12.7,
    left_mm=4.0,
    col_gap_mm=4.8,
)


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def _save(client: TestClient, name: str, **layout: object) -> dict[str, object]:
    made = client.post(SHEETS, json={"name": name, "layout": AVERY_5163 | layout})
    assert made.status_code == 201, made.text
    row: dict[str, object] = made.json()
    return row


def _spools(client: TestClient, count: int) -> list[str]:
    material = next(
        row["id"] for row in client.get(f"{URL}/catalog/materials").json() if row["name"] == "PLA"
    )
    ids = []
    for _ in range(count):
        made = client.post(f"{URL}/spools", json={"material_id": material})
        assert made.status_code == 201, made.text
        ids.append(made.json()["id"])
    return ids


def _letter_pages(pdf: bytes) -> int:
    boxes = re.findall(rb"/MediaBox \[ 0 0 ([\d.]+) ([\d.]+) \]", pdf)
    for w, h in boxes:
        assert (float(w), float(h)) == pytest.approx((215.9 * MM, 279.4 * MM), abs=0.01)
    return len(boxes)


def test_a_saved_sheet_is_listed_with_the_templates(client: TestClient) -> None:
    made = _save(client, "Avery 5163")
    listed = client.get(f"{URL}/label-templates")
    assert listed.status_code == 200, listed.text
    [saved] = listed.json()["saved"]
    assert saved["id"] == made["id"]
    assert saved["name"] == "Avery 5163"
    assert saved["layout"] == AVERY_5163 | {"row_gap_mm": 0.0}


def test_the_saved_sheets_are_listed_by_name_without_case(client: TestClient) -> None:
    for name in ("beta", "Alpha", "gamma"):
        _save(client, name)
    names = [row["name"] for row in client.get(f"{URL}/label-templates").json()["saved"]]
    assert names == ["Alpha", "beta", "gamma"]


def test_a_name_taken_in_another_case_is_a_409(client: TestClient) -> None:
    _save(client, "Avery 5163")
    taken = client.post(SHEETS, json={"name": "avery 5163", "layout": AVERY_5163})
    assert taken.status_code == 409, taken.text
    assert taken.json()["code"] == "label_sheet_name_taken"
    other = _save(client, "Other")
    renamed = client.put(
        f"{SHEETS}/{other['id']}", json={"name": "AVERY 5163", "layout": AVERY_5163}
    )
    assert renamed.status_code == 409, renamed.text
    assert renamed.json()["code"] == "label_sheet_name_taken"
    # A sheet keeps its own name in another case.
    same = client.put(f"{SHEETS}/{other['id']}", json={"name": "OTHER", "layout": AVERY_5163})
    assert same.status_code == 200, same.text


def test_a_grid_that_does_not_fit_is_a_422(client: TestClient) -> None:
    wide = client.post(SHEETS, json={"name": "Wide", "layout": AVERY_5163 | {"cols": 3}})
    assert wide.status_code == 422, wide.text


def test_the_hundred_and_first_sheet_is_a_409(client: TestClient) -> None:
    store = _state(client).components.get(FILAMENT).label_sheets
    layout = SheetLayout(**AVERY_5163)  # type: ignore[arg-type]
    for at in range(100):
        store.create(LabelSheetIn(name=f"Sheet {at}", layout=layout))
    refused = client.post(SHEETS, json={"name": "One more", "layout": AVERY_5163})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "too_many_label_sheets"


def test_replace_and_delete(client: TestClient) -> None:
    made = _save(client, "Avery 5163")
    at = f"{SHEETS}/{made['id']}"
    changed = client.put(at, json={"name": "Avery 5163", "layout": AVERY_5163 | {"cols": 1}})
    assert changed.status_code == 200, changed.text
    assert changed.json()["layout"]["cols"] == 1
    assert changed.json()["created_at"] == made["created_at"]
    [saved] = client.get(f"{URL}/label-templates").json()["saved"]
    assert saved["layout"]["cols"] == 1
    assert client.delete(at).status_code == 204
    assert client.get(f"{URL}/label-templates").json()["saved"] == []
    for answer in (
        client.put(at, json={"name": "Avery 5163", "layout": AVERY_5163}),
        client.delete(at),
    ):
        assert answer.status_code == 404, answer.text
        assert f"no label sheet {made['id']}" in answer.text


def test_a_saved_choice_prints_on_the_saved_grid(client: TestClient) -> None:
    made = _save(client, "Avery 5163")
    body = {
        "spool_ids": _spools(client, 11),
        "template": {"template": "saved", "sheet_id": made["id"]},
    }
    answer = client.post(PDF, json=body)
    assert answer.status_code == 200, answer.text
    # 2 x 5 labels a Letter page: 11 labels take two pages.
    assert _letter_pages(answer.content) == 2
    png = client.post(PDF, json=body | {"format": "png", "dpi": 203})
    assert png.status_code == 200, png.text
    assert png.headers["content-type"] == "application/zip"


def test_an_edit_applies_to_the_next_print(client: TestClient) -> None:
    made = _save(client, "Avery 5163")
    spools = _spools(client, 11)
    edited = client.put(
        f"{SHEETS}/{made['id']}", json={"name": "Avery 5163", "layout": AVERY_5163 | {"cols": 1}}
    )
    assert edited.status_code == 200, edited.text
    body = {"spool_ids": spools, "template": {"template": "saved", "sheet_id": made["id"]}}
    answer = client.post(PDF, json=body)
    assert answer.status_code == 200, answer.text
    # 1 x 5 labels a page now: 11 labels take three pages.
    assert _letter_pages(answer.content) == 3


def test_a_choice_of_a_deleted_sheet_is_a_409(client: TestClient) -> None:
    made = _save(client, "Avery 5163")
    stored = {"template": "saved", "sheet_id": made["id"]}
    assert client.put(SETTINGS, json={"filament_label_template": stored}).status_code == 200
    assert client.get(SETTINGS).json()["filament_label_template"] == stored | {
        "width_mm": None,
        "height_mm": None,
    }
    assert client.delete(f"{SHEETS}/{made['id']}").status_code == 204
    spools = _spools(client, 1)
    for form in ("pdf", "png"):
        gone = client.post(PDF, json={"spool_ids": spools, "format": form})
        assert gone.status_code == 409, gone.text
        assert gone.json()["code"] == "label_sheet_gone"


def test_the_sheet_writes_do_not_nudge_the_sync(
    client: TestClient, filament_nudges: list[bool]
) -> None:
    assert {"create_label_sheet", "replace_label_sheet", "delete_label_sheet"} <= NOT_NUDGED
    made = _save(client, "Avery 5163")
    at = f"{SHEETS}/{made['id']}"
    assert client.put(at, json={"name": "Avery 5163", "layout": AVERY_5163}).status_code == 200
    assert client.delete(at).status_code == 204
    assert filament_nudges == []
