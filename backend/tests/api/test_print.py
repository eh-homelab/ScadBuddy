"""Issue #86 — /api/v1/print/…, through the real app.

The picker's own pipeline-listing, pipeline-creation, preset-browsing and
eligibility-check routes are gone (spec 2026-09-27 §4): the dialog now derives every
slicer preset from the chosen spools, nozzles, quality and plate instead of picking a
Bambuddy pipeline. What is left here is what the picker still remembers for itself —
a model's printer and spools, and a printer's plate type — neither of which touches
Bambuddy. The helpers below (``printers_route``, ``presets_routes``) are imported by the
other print test modules.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from tests.api.test_send import BASE
from tests.bambuddy.conftest import recording

API = f"{BASE}/api/v1"


def printers_route() -> respx.Route:
    return respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )


def presets_routes() -> None:
    respx.get(f"{API}/slicer/presets").mock(
        return_value=httpx.Response(200, json=recording("slicer-presets.json"))
    )
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=recording("local-presets.json"))
    )


# --- the model's remembered printer and spools ---------------------------------------

#: The spool-first dialog's own remembered choices (spec 2026-09-27 §7), unset: what a
#: PUT that names only the printer and spools reads back with.
NO_DIALOG_CHOICES: dict[str, object] = {"nozzles": [], "tier": None, "process_name": None}


def test_the_models_printer_and_spools_are_remembered(
    client: TestClient, model: str, data_dir: Path
) -> None:
    """#78 — the picker reopens on the printer and spools it last chose for this model.

    Needs no Bambuddy: this is ScadBuddy's own preference, stored per slug. Verified by
    reading the persisted file back, independently of either write's own echoed
    response — a store that ignored the slug key would still echo each PUT correctly
    (it is just handed back what was sent) while silently sharing one entry between
    models, and only a real readback catches that.
    """
    choices_a = {
        **{"printer_id": 2, "filament_plan": [{"slot_id": 1, "spool_id": 9}]},
        **NO_DIALOG_CHOICES,
    }
    choices_b = {
        **{"printer_id": 3, "filament_plan": [{"slot_id": 1, "spool_id": 11}]},
        **NO_DIALOG_CHOICES,
    }

    assert client.put(f"/api/v1/print/models/{model}/choices", json=choices_a).json() == choices_a
    assert (
        client.put("/api/v1/print/models/some-other-model/choices", json=choices_b).json()
        == choices_b
    )

    stored = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert stored["model_print_choices"] == {model: choices_a, "some-other-model": choices_b}


def test_a_built_ins_choices_are_its_own(client: TestClient, data_dir: Path) -> None:
    """A built-in prints too (#155); its remembered choices are kept apart from a
    template of mine with the same slug. Verified by reading the persisted file back
    (see :func:`test_the_models_printer_and_spools_are_remembered` for why)."""
    builtin_choices = {
        **{"printer_id": 2, "filament_plan": [{"slot_id": 1, "spool_id": 9}]},
        **NO_DIALOG_CHOICES,
    }
    mine_choices = {
        **{"printer_id": 3, "filament_plan": [{"slot_id": 1, "spool_id": 11}]},
        **NO_DIALOG_CHOICES,
    }

    assert (
        client.put("/api/v1/print/models/builtin:keychain/choices", json=builtin_choices).json()
        == builtin_choices
    )
    assert (
        client.put("/api/v1/print/models/keychain/choices", json=mine_choices).json()
        == mine_choices
    )

    stored = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert stored["model_print_choices"] == {
        "builtin:keychain": builtin_choices,
        "keychain": mine_choices,
    }


def test_remembering_nothing_forgets_the_models_choices(client: TestClient, model: str) -> None:
    client.put(
        f"/api/v1/print/models/{model}/choices",
        json={"printer_id": 2, "filament_plan": [{"slot_id": 1, "spool_id": 9}]},
    )

    cleared = client.put(f"/api/v1/print/models/{model}/choices", json={})

    assert cleared.json() == {"printer_id": None, "filament_plan": [], **NO_DIALOG_CHOICES}
