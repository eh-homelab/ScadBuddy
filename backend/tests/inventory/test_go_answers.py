"""Go's recorded answers fit the public models (#6949 K1, R-K1-21): each file Go's
`TestTheAnswersMatchTheirGoldenFiles` writes validates with its pydantic model, and the
model writes it back unchanged. Go's `inventory_operation.changed` notice decodes as
Python's event."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from scadbuddy.api.inventory import ItemList, KindList, KindSchema
from scadbuddy.api.inventory_stock import LotList, MoveList, UnitList
from scadbuddy.core.events import InventoryOperationEvent, decode_event
from scadbuddy.core.problems import ApiError
from scadbuddy.entities.problems import bad_property_value, missing_required
from scadbuddy.entities.properties import MissingRequiredProperties
from scadbuddy.inventory.models import Item, ItemKind, Lot, Moved, RecostResult, Unit

ROOT = Path(__file__).resolve().parents[3]
ANSWERS = ROOT / "tools/go/core/internal/inventory/activities/testdata/answers"
NOTICE = ROOT / "tools/go/core/internal/publish/testdata/inventory_operation_changed.json"

#: Each recorded answer's public model: the activity's, or the read family's.
MODELS: dict[str, type[BaseModel]] = {
    "Count": MoveList,
    "GiveBack": MoveList,
    "ItemArchive": Item,
    "ItemCreate": Item,
    "Item": Item,
    "ItemKind": ItemKind,
    "ItemList": ItemList,
    "ItemRestore": Item,
    "ItemUpdate": Item,
    "KindArchive": ItemKind,
    "KindCreate": ItemKind,
    "KindList": KindList,
    "KindRestore": ItemKind,
    "KindSchema": KindSchema,
    "KindUpdate": ItemKind,
    "LotAdjust": Lot,
    "Lot": Lot,
    "LotList": LotList,
    "LotUpdate": Lot,
    "MoveList": MoveList,
    "ReceiveLot": Lot,
    "ReceiveUnits": UnitList,
    "Recost": RecostResult,
    "RecostResult": RecostResult,
    "Repoint": Moved,
    "Take": MoveList,
    "Unit": Unit,
    "UnitList": UnitList,
    "UnitStatus": Unit,
    "UnitUpdate": Unit,
}


#: Each recorded refusal, as Python's own builder of that problem writes it (R-K1-33): Go's
#: port answers what `apply_on` and `check_row` answer.
PROBLEMS: dict[str, ApiError] = {
    "ItemCreateMissingProperties": missing_required(MissingRequiredProperties(("material",))),
    "ItemCreateBadPropertyValue": bad_property_value("material", "does not fit 'type'"),
}


def test_every_answer_has_a_model() -> None:
    assert {p.stem for p in ANSWERS.glob("*.json")} == set(MODELS) | set(PROBLEMS)


@pytest.mark.parametrize("name", sorted(PROBLEMS))
def test_gos_refusals_are_pythons_problems(name: str) -> None:
    recorded = json.loads((ANSWERS / f"{name}.json").read_text(encoding="utf-8"))
    error = PROBLEMS[name]

    assert recorded == {
        "type": error.type,
        "title": error.title,
        "status": error.status,
        "detail": error.detail,
        **error.extensions,
    }


@pytest.mark.parametrize("name", sorted(MODELS))
def test_go_answers_fit_the_public_models(name: str) -> None:
    recorded: Any = json.loads((ANSWERS / f"{name}.json").read_text(encoding="utf-8"))

    model = MODELS[name].model_validate(recorded)

    assert model.model_dump(mode="json", by_alias=True) == recorded


def test_gos_notice_decodes_as_the_event() -> None:
    event = decode_event(NOTICE.read_text(encoding="utf-8"))

    assert isinstance(event, InventoryOperationEvent)
    assert event.operation_id == "browser:op:k"
