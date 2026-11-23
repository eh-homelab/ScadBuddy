"""#7157: `POST /filament/labels` (the labels PDF), `GET /filament/label-templates` and the
setting `filament_label_template` (plan
`docs/superpowers/plans/2026-11-23-label-templates-pdf.md`)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from scadbuddy.filament import label_pdf
from scadbuddy.filament.label_pdf import Label, Layout
from scadbuddy.filament.label_templates import LabelTemplate

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament"
PDF = f"{URL}/labels"
SETTINGS = "/api/v1/settings"
MM = 72 / 25.4


def _material_id(client: TestClient) -> str:
    found: str = next(
        row["id"] for row in client.get(f"{URL}/catalog/materials").json() if row["name"] == "PLA"
    )
    return found


def _spool(client: TestClient, **over: object) -> dict[str, object]:
    made = client.post(f"{URL}/spools", json={"material_id": _material_id(client)} | over)
    assert made.status_code == 201, made.text
    row: dict[str, object] = made.json()
    return row


def _place(client: TestClient, name: str) -> dict[str, object]:
    kind = next(
        r["id"] for r in client.get(f"{URL}/catalog/location-kinds").json() if r["name"] == "Shelf"
    )
    made = client.post(f"{URL}/locations", json={"name": name, "kind_id": kind})
    assert made.status_code == 201, made.text
    row: dict[str, object] = made.json()
    return row


@pytest.fixture
def drawn(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[Label], LabelTemplate, Layout]]:
    """What the route asked `render_pdf` to draw."""
    calls: list[tuple[list[Label], LabelTemplate, Layout]] = []
    real = label_pdf.render_pdf

    def spy(labels: list[Label], template: LabelTemplate, layout: Layout) -> bytes:
        calls.append((list(labels), template, layout))
        return real(labels, template, layout)

    monkeypatch.setattr(label_pdf, "render_pdf", spy)
    return calls


def test_the_templates_are_listed_with_the_custom_bounds(client: TestClient) -> None:
    answer = client.get(f"{URL}/label-templates")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    ids = [row["id"] for row in body["templates"]]
    assert ids[0] == "roll_50x25"
    assert {"box_40x30", "box_62x29", "ams_holder_74x33", "avery_5160", "avery_l7160"} <= set(ids)
    avery = next(row for row in body["templates"] if row["id"] == "avery_5160")
    assert avery["sheet"]["page"] == "letter"
    assert (avery["sheet"]["cols"], avery["sheet"]["rows"]) == (3, 10)
    assert body["custom"] == {
        "min_width_mm": 40.0,
        "min_height_mm": 25.0,
        "max_width_mm": 190.0,
        "max_height_mm": 190.0,
    }


def test_the_template_setting_round_trips(client: TestClient) -> None:
    name = "filament_label_template"
    assert client.get(SETTINGS).json()[name] == {
        "template": "roll_50x25",
        "width_mm": None,
        "height_mm": None,
    }
    custom = {"template": "custom", "width_mm": 80, "height_mm": 40}
    assert client.put(SETTINGS, json={name: custom}).status_code == 200
    assert client.get(SETTINGS).json()[name] == custom
    assert client.put(SETTINGS, json={name: {"template": "custom"}}).status_code == 422
    assert client.put(SETTINGS, json={name: None}).status_code == 200
    assert client.get(SETTINGS).json()[name]["template"] == "roll_50x25"


def test_a_spool_label_pdf_carries_the_spools_own_data(
    client: TestClient, drawn: list[tuple[list[Label], LabelTemplate, Layout]]
) -> None:
    spool = _spool(client, colour_name="Jade White")
    answer = client.post(PDF, json={"spool_ids": [spool["id"]]})
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"] == "application/pdf"
    assert 'filename="labels.pdf"' in answer.headers["content-disposition"]
    assert answer.content.startswith(b"%PDF-")
    [(labels, template, layout)] = drawn
    assert labels == [
        label_pdf.SpoolLabel(
            number=int(str(spool["number"])),
            material="PLA",
            colour="Jade White",
            brand=None,
            qr=f"scadbuddy:spool:{spool['id']}",
        )
    ]
    # No template sent: the stored setting's, the 50 x 25 mm roll by default.
    assert (template.id, layout) == ("roll_50x25", "roll")


def test_the_pdf_takes_the_stored_template_or_the_one_sent(
    client: TestClient, drawn: list[tuple[list[Label], LabelTemplate, Layout]]
) -> None:
    spool = _spool(client)
    custom = {"template": "custom", "width_mm": 80, "height_mm": 40}
    assert client.put(SETTINGS, json={"filament_label_template": custom}).status_code == 200
    assert client.post(PDF, json={"spool_ids": [spool["id"]]}).status_code == 200
    assert (drawn[-1][1].width_mm, drawn[-1][1].height_mm) == (80, 40)
    sent = {"spool_ids": [spool["id"]], "template": {"template": "avery_l7160"}, "layout": "sheet"}
    assert client.post(PDF, json=sent).status_code == 200
    assert (drawn[-1][1].id, drawn[-1][2]) == ("avery_l7160", "sheet")


def test_a_place_label_opens_the_label_address(
    client: TestClient, drawn: list[tuple[list[Label], LabelTemplate, Layout]]
) -> None:
    place = _place(client, "Dry box")
    body = {"location_ids": [place["id"]]}
    refused = client.post(PDF, json=body)
    # No label address and no public URL: no label, never the request's own host.
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "no_label_base"
    assert (
        client.put(SETTINGS, json={"filament_label_base_url": "https://sb.example.com"}).status_code
        == 200
    )
    assert client.post(PDF, json=body).status_code == 200
    code = f"L-{place['number']}"
    assert drawn[-1][0] == [
        label_pdf.PlaceLabel(code=code, name="Dry box", qr=f"https://sb.example.com/x/{code}")
    ]


def test_the_labels_keep_the_request_order(
    client: TestClient, drawn: list[tuple[list[Label], LabelTemplate, Layout]]
) -> None:
    first, second, third = (_spool(client) for _ in range(3))
    shelf, box = _place(client, "Shelf 1"), _place(client, "Box 2")
    assert (
        client.put(SETTINGS, json={"filament_label_base_url": "https://sb.example.com"}).status_code
        == 200
    )
    body = {
        "spool_ids": [third["id"], first["id"], second["id"]],
        "location_ids": [box["id"], shelf["id"]],
    }
    assert client.post(PDF, json=body).status_code == 200
    qrs = [label.qr for label in drawn[-1][0]]
    assert qrs == [
        f"scadbuddy:spool:{third['id']}",
        f"scadbuddy:spool:{first['id']}",
        f"scadbuddy:spool:{second['id']}",
        f"https://sb.example.com/x/L-{box['number']}",
        f"https://sb.example.com/x/L-{shelf['number']}",
    ]


def test_an_unknown_id_is_a_404_that_names_it(client: TestClient) -> None:
    gone = str(uuid4())
    answer = client.post(PDF, json={"spool_ids": [gone]})
    assert answer.status_code == 404, answer.text
    assert gone in answer.text
    # Among known ones, of either kind, too.
    spool, place = _spool(client), _place(client, "Shelf 9")
    base = {"filament_label_base_url": "https://sb.example.com"}
    assert client.put(SETTINGS, json=base).status_code == 200
    answer = client.post(
        PDF, json={"spool_ids": [spool["id"]], "location_ids": [place["id"], gone]}
    )
    assert answer.status_code == 404, answer.text
    assert gone in answer.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"spool_ids": []},
        {"spool_ids": [str(uuid4()) for _ in range(101)]},
        {"spool_ids": [str(uuid4())], "layout": "fan"},
        {"spool_ids": [str(uuid4())], "template": {"template": "custom", "width_mm": 30}},
    ],
)
def test_a_bad_body_is_a_422(client: TestClient, body: dict[str, object]) -> None:
    assert client.post(PDF, json=body).status_code == 422
