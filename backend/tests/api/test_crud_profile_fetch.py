"""#6625 Task 3: the profile-fetch worker's CRUD routes and the import they end in. The
worker uploads a run's preset files; the API holds them as blobs, makes them ScadBuddy's
own presets (#7176), mirrors them to Bambuddy, and sets the type's presets and K values.
Bambuddy is respx only."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.filament import profile_fetch
from scadbuddy.filament.component import FILAMENT
from scadbuddy.filament.models import (
    SpoolCreate,
    SpoolKValueCreate,
    SpoolPresetCreate,
    TypeKValue,
    TypePreset,
)
from scadbuddy.filament.profile_fetch import HOLDER, ProfileFetches
from scadbuddy.orders.attachments import blob_path
from scadbuddy.printers.component import PRINTERS
from tests.api.test_send import API, configure
from tests.filament.test_types import _body

pytestmark = pytest.mark.requires_postgres

RUNS = "/api/v1/internal/crud/profile-fetch/runs"
EXPORT = Path(__file__).parents[1] / "profile_fetch" / "site" / "export-h2c-0.4.json"
#: A real export (#7029; `tests/filament/test_profile_fetch.py` `REAL_EXPORT` says which).
REAL_EXPORT = Path(__file__).parents[1] / "fixtures" / "3dfp" / "export-35902-h2c-0.4.json"
NOZZLES = ["0.2", "0.4", "0.6", "0.8"]
KPROFILES = re.compile(rf"{re.escape(API)}/printers/(\d+)/kprofiles/.*")


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def export(nozzle: str = "0.4", **over: Any) -> bytes:
    """The sample export, as 3DFP would give it for ``nozzle`` (each nozzle's preset has a
    name of its own)."""
    doc = json.loads(EXPORT.read_text())
    if nozzle != "0.4":
        doc["name"] = f"Inland PLA+ @BBL H2C {nozzle} nozzle"
        doc["compatible_printers"] = [f"Bambu Lab H2C {nozzle} nozzle"]
    doc.update(over)
    return json.dumps(doc).encode()


def nested(levels: int) -> Any:
    """A value of ``levels`` nested lists."""
    value: Any = []
    for _ in range(levels - 1):
        value = [value]
    return value


def name_of(body: bytes) -> str:
    name: str = json.loads(body)["name"]
    return name


@dataclass
class Fetch:
    client: TestClient
    type_id: UUID
    run_id: UUID
    printer_id: str
    bearer: dict[str, str]

    def put(
        self,
        body: bytes,
        model: str = "H2C",
        nozzle: str = "0.4",
        sha: str | None = None,
        bearer: dict[str, str] | None = None,
    ) -> Any:
        return self.client.put(
            f"{RUNS}/{self.run_id}/files/{sha or hashlib.sha256(body).hexdigest()}",
            params={"printer_model": model, "nozzle": nozzle},
            content=body,
            headers={**(bearer or self.bearer), "Content-Type": "application/json"},
        )

    def import_(self, failures: list[dict[str, str]] | None = None, key: str | None = "k1") -> Any:
        headers = {**self.bearer, **({"Idempotency-Key": key} if key else {})}
        return self.client.post(
            f"{RUNS}/{self.run_id}/import", json={"failures": failures or []}, headers=headers
        )

    def failed(self, reason: str) -> Any:
        return self.client.post(
            f"{RUNS}/{self.run_id}/failed", json={"reason": reason}, headers=self.bearer
        )

    def rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        with _state(self.client).settings_store.pool.connection() as conn:
            return list(conn.execute(sql, args).fetchall())

    def presets(self) -> list[tuple[str, str, str, str | None]]:
        rows = _state(self.client).components.get(FILAMENT).types.presets(self.type_id)
        return [
            (p.printer_model, p.nozzle_diameter, p.slicer_filament, p.slicer_filament_name)
            for p in rows
        ]

    def run(self) -> Any:
        return ProfileFetches(_state(self.client).settings_store.pool, lambda: NOZZLES).latest(
            self.type_id
        )


def bearer_of(client: TestClient, caller: str) -> dict[str, str]:
    issued = ServiceCredentialStore(_state(client).settings_store.pool).issue(
        caller,  # type: ignore[arg-type]
        "bootstrap",
        rate_limited=False,
    )
    return {"Authorization": f"Bearer {issued.token}"}


@pytest.fixture
def fetch(client: TestClient) -> Fetch:
    configure(client)
    state = _state(client)
    pool = state.settings_store.pool
    printer = state.components.get(PRINTERS).store.create_linked(
        serial="31B598000000", name="H2C", model="H2C", bambuddy_printer_id=1
    )
    type_id = state.components.get(FILAMENT).types.create(_body(pool, tdfp_id=30173)).id
    run, _ = ProfileFetches(pool, lambda: NOZZLES).open(type_id, actor="person:test")
    return Fetch(client, type_id, run.id, printer.id, bearer_of(client, "profile-fetch"))


def mirror(first_id: int = 101, status_code: int = 200) -> respx.Route:
    """Bambuddy's ``POST /local-presets/``, the mirror's create (#7176): each call answers
    the next id from ``first_id``, or ``status_code`` when it is not 200; and an empty
    ``GET /local-presets/``, the listing the mirror reads before a create."""
    ids = itertools.count(first_id)

    def answer(request: httpx.Request) -> httpx.Response:
        if status_code != 200:
            return httpx.Response(status_code, json={"detail": "Forbidden"})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": next(ids),
                "name": body["name"],
                "preset_type": "filament",
                "source": "local",
            },
        )

    # The mirror lists Bambuddy's presets once before its first create.
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json={"filament": [], "printer": [], "process": []})
    )
    return respx.post(f"{API}/local-presets/").mock(side_effect=answer)


def own_presets(fetch: Fetch) -> list[dict[str, Any]]:
    """ScadBuddy's presets (#7176), oldest first."""
    return fetch.rows(
        "SELECT id, name, source, version, printer_model, nozzle_diameter, fetch_run_id,"
        " bb_preset_id, blob_key, sha256, mirrored_sha256, mirror_error"
        " FROM filament_presets ORDER BY id"
    )


def own_doc(fetch: Fetch, preset: dict[str, Any]) -> Any:
    return json.loads(_blob(fetch, preset["blob_key"]).read_bytes())


def refs(fetch: Fetch) -> list[str]:
    return [p[2] for p in fetch.presets()]


def no_k_profiles() -> respx.Route:
    return respx.get(url__regex=KPROFILES.pattern).mock(
        return_value=httpx.Response(200, json={"profiles": []})
    )


def put_all(fetch: Fetch, nozzles: list[str] = NOZZLES) -> list[bytes]:
    bodies = [export(n) for n in nozzles]
    for nozzle, body in zip(nozzles, bodies, strict=True):
        r = fetch.put(body, nozzle=nozzle)
        assert r.status_code == 204, r.text
    return bodies


# -- the files ---------------------------------------------------------------------------


def test_a_file_is_stored_as_a_blob_held_by_its_run(fetch: Fetch) -> None:
    body = export()
    r = fetch.put(body)
    assert r.status_code == 204, r.text
    [row] = fetch.rows("SELECT files FROM filament_profile_fetches WHERE id = %s", fetch.run_id)
    entry = row["files"]["H2C"]["0.4"]
    assert entry["name"] == "Inland PLA+ @BBL H2C"
    refs = fetch.rows(
        "SELECT key, holder_kind, holder_id FROM blob_refs WHERE holder_kind = %s", HOLDER
    )
    assert refs == [{"key": entry["key"], "holder_kind": HOLDER, "holder_id": str(fetch.run_id)}]


def test_a_file_32_levels_deep_is_kept(fetch: Fetch) -> None:
    r = fetch.put(export(note=nested(31)))
    assert r.status_code == 204, r.text


def test_a_file_sent_again_replaces_the_key_and_its_hold(fetch: Fetch) -> None:
    assert fetch.put(export()).status_code == 204
    again = export(filament_vendor=["Inland 2"])
    assert fetch.put(again).status_code == 204
    [row] = fetch.rows("SELECT files FROM filament_profile_fetches WHERE id = %s", fetch.run_id)
    refs = fetch.rows("SELECT key FROM blob_refs WHERE holder_kind = %s", HOLDER)
    assert refs == [{"key": row["files"]["H2C"]["0.4"]["key"]}]


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("wrong sha256", 422),
        ("too large", 413),
        ("not json", 422),
        ("no name", 422),
        ("machine preset", 422),
        ("printer settings id", 422),
        ("print settings id", 422),
        ("inherits not a string", 422),
        ("too deep", 422),
        ("deep enough to blow the stack", 422),
        ("NaN", 422),
        ("Infinity", 422),
        ("-Infinity", 422),
        ("1e400", 422),
        ("not a target", 422),
        ("not running", 409),
    ],
)
def test_a_file_is_refused_unless_valid(fetch: Fetch, case: str, status: int) -> None:
    body = export()
    if case == "wrong sha256":
        r = fetch.put(body, sha="0" * 64)
    elif case == "too large":
        r = fetch.put(export(note="x" * (512 << 10)))
    elif case == "not json":
        r = fetch.put(b"<html>no</html>")
    elif case == "no name":
        r = fetch.put(json.dumps({"filament_id": "P1"}).encode())
    elif case == "machine preset":
        r = fetch.put(export(type="machine"))
    elif case == "printer settings id":
        r = fetch.put(export(printer_settings_id="Bambu Lab H2C 0.4 nozzle"))
    elif case == "print settings id":
        r = fetch.put(export(print_settings_id="0.20mm Standard @BBL H2C"))
    elif case == "inherits not a string":
        r = fetch.put(export(inherits=["Generic PLA @BBL H2C"]))
    elif case == "too deep":
        # The object itself is level 1; 32 more lists make 33 levels.
        r = fetch.put(export(note=nested(32)))
    elif case == "deep enough to blow the stack":
        deep = b"[" * 200_000 + b"]" * 200_000
        r = fetch.put(export(note=0).replace(b'"note": 0', b'"note": ' + deep))
    elif case in {"NaN", "Infinity", "-Infinity", "1e400"}:
        r = fetch.put(export(note=0).replace(b'"note": 0', b'"note": ' + case.encode()))
    elif case == "not a target":
        r = fetch.put(body, model="X1C")
    else:
        assert fetch.failed("gave up").status_code == 204
        r = fetch.put(body)
        assert r.json()["code"] == "not_running"
    assert r.status_code == status, r.text
    if case != "not running":
        assert fetch.rows("SELECT key FROM blob_refs WHERE holder_kind = %s", HOLDER) == []


GCODE: dict[str, Any] = {
    "filament_start_gcode": ["G28\nM104 S300"],
    "filament_end_gcode": ["M84"],
    "machine_end_gcode": "M84",
    "post_process": ["/bin/sh -c x"],
}


def _blob(fetch: Fetch, key: str) -> Path:
    path = asyncio.run(blob_path(_state(fetch.client).store.blobs, key))
    assert path is not None
    return path


def test_site_gcode_is_stripped_before_the_file_is_kept(fetch: Fetch) -> None:
    body = export(**GCODE)
    r = fetch.put(body)
    assert r.status_code == 204, r.text
    [row] = fetch.rows("SELECT files FROM filament_profile_fetches WHERE id = %s", fetch.run_id)
    entry = row["files"]["H2C"]["0.4"]
    kept = _blob(fetch, entry["key"]).read_bytes()
    assert entry["sha256"] == hashlib.sha256(kept).hexdigest()
    assert json.loads(kept) == json.loads(export())


@pytest.mark.parametrize("inherits", ["", None])
def test_a_file_with_no_base_is_kept(fetch: Fetch, inherits: str | None) -> None:
    """#7029: a flattened preset names no base, by an empty ``inherits`` or none."""
    doc = json.loads(export(inherits=inherits))
    if inherits is None:
        del doc["inherits"]
    r = fetch.put(json.dumps(doc).encode())
    assert r.status_code == 204, r.text
    [row] = fetch.rows("SELECT files FROM filament_profile_fetches WHERE id = %s", fetch.run_id)
    assert json.loads(_blob(fetch, row["files"]["H2C"]["0.4"]["key"]).read_bytes()) == doc


@respx.mock
def test_a_real_3dfp_export_is_kept_and_imported(fetch: Fetch) -> None:
    """#7029: a real 3DFP export (flattened, ``"inherits": ""``) is kept without its
    G-code and made a ScadBuddy preset as it was kept (#7176), and the type's 0.4 mm preset
    is set to it."""
    body = REAL_EXPORT.read_bytes()
    doc = json.loads(body)
    r = fetch.put(body)
    assert r.status_code == 204, r.text
    [row] = fetch.rows("SELECT files FROM filament_profile_fetches WHERE id = %s", fetch.run_id)
    entry = row["files"]["H2C"]["0.4"]
    kept = json.loads(_blob(fetch, entry["key"]).read_bytes())
    gcode = {"filament_start_gcode", "filament_end_gcode"}
    assert kept == {k: v for k, v in doc.items() if k not in gcode}
    assert (entry["name"], kept["inherits"]) == (doc["name"], "")

    sent = mirror()
    no_k_profiles()
    r = fetch.import_(
        [{"printer_model": "H2C", "nozzle": n, "reason": "x"} for n in ("0.2", "0.6", "0.8")]
    )
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["presets"]) == ("partial", 1)
    [preset] = own_presets(fetch)
    assert own_doc(fetch, preset) == kept
    assert json.loads(sent.calls.last.request.content)["setting"] == kept
    assert fetch.presets() == [("H2C", "0.4", f"sb-{preset['id']}", doc["name"])]


def test_an_unknown_run_is_404(fetch: Fetch) -> None:
    fetch.run_id = UUID(int=5)
    r = fetch.put(export())
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "run_not_found"


def test_another_caller_gets_403(fetch: Fetch) -> None:
    other = bearer_of(fetch.client, "intake")
    assert fetch.put(export(), bearer=other).status_code == 403
    fetch.bearer = other
    assert fetch.import_().status_code == 403
    assert fetch.failed("x").status_code == 403


# -- the import --------------------------------------------------------------------------


@respx.mock
def test_import_makes_scadbuddy_presets_and_mirrors_them(fetch: Fetch) -> None:
    """#7176: each file is a ``3dfp`` ScadBuddy preset of the type's key, the type names it
    ``sb-<id>``, Bambuddy gets no import, and the mirror sends each one after the commit."""
    bodies = put_all(fetch)
    sent = mirror()
    imported = respx.post(f"{API}/local-presets/import")
    no_k_profiles()

    r = fetch.import_()

    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "imported",
        "presets": 4,
        "k_values": 0,
        "missing": [],
        "notes": [],
    }
    made = own_presets(fetch)
    assert [(p["source"], p["nozzle_diameter"], p["version"]) for p in made] == [
        ("3dfp", n, 1) for n in NOZZLES
    ]
    assert {p["fetch_run_id"] for p in made} == {fetch.run_id}
    assert fetch.presets() == [
        ("H2C", n, f"sb-{made[i]['id']}", name_of(bodies[i])) for i, n in enumerate(NOZZLES)
    ]
    assert (imported.call_count, sent.call_count) == (0, 4)
    assert [p["bb_preset_id"] for p in made] == [101, 102, 103, 104]
    assert all(p["mirrored_sha256"] == p["sha256"] for p in made)
    run = fetch.run()
    assert (run.status, run.finished_at is not None) == ("imported", True)
    [row] = fetch.rows("SELECT files FROM filament_profile_fetches WHERE id = %s", fetch.run_id)
    assert row["files"]["H2C"]["0.4"]["preset_id"] == f"sb-{made[1]['id']}"


@respx.mock
def test_the_first_presets_an_import_sets_fill_the_type(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-6714-a: after the import commits; a type that has presets is not filled again."""
    from scadbuddy.filament.profile_fill import TypeFiller

    asked: list[list[Any]] = []

    async def spy(self: Any, settings: Any, type_ids: Any, **kwargs: Any) -> list[Any]:
        asked.append(list(type_ids))
        return []

    monkeypatch.setattr(TypeFiller, "fill", spy)
    put_all(fetch)
    mirror()
    no_k_profiles()
    assert fetch.import_().status_code == 200
    assert asked == [[fetch.type_id]]


@respx.mock
def test_a_filler_lookup_that_fails_after_the_commit_never_fails_the_import(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R-6714-f: the lookup of the filler runs inside `fill_quietly`'s guard."""
    from scadbuddy.core.components import Key
    from scadbuddy.filament import component

    # The import's own lookup now names a component nothing registers, so it raises.
    monkeypatch.setattr(component, "FILAMENT", Key("filament-gone"))
    put_all(fetch)
    mirror()
    no_k_profiles()
    with caplog.at_level("WARNING"):
        r = fetch.import_()
    assert r.status_code == 200, r.text
    assert fetch.run().status == "imported"
    [warning] = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and getattr(r, "error", None) == "KeyError"
    ]
    assert warning.levelname == "WARNING"


@respx.mock
def test_the_preset_carries_no_site_gcode(fetch: Fetch) -> None:
    body = export(**GCODE)
    assert fetch.put(body).status_code == 204
    # The kept blob changed under the run (defence in depth: the import strips again).
    [row] = fetch.rows("SELECT files FROM filament_profile_fetches WHERE id = %s", fetch.run_id)
    path = _blob(fetch, row["files"]["H2C"]["0.4"]["key"])
    path.chmod(0o644)
    # Other bytes than the kept file's: a content-addressed blob the preset would share
    # otherwise.
    path.write_bytes(export(**GCODE, filament_notes=["tampered"]))
    mirror()
    no_k_profiles()
    r = fetch.import_(
        [{"printer_model": "H2C", "nozzle": n, "reason": "x"} for n in ("0.2", "0.6", "0.8")]
    )
    assert r.status_code == 200, r.text
    [preset] = own_presets(fetch)
    doc = own_doc(fetch, preset)
    assert [k for k in doc if k.endswith("gcode") or k == "post_process"] == []
    assert (doc["name"], doc["filament_notes"]) == (name_of(body), ["tampered"])


def _next_run(fetch: Fetch) -> None:
    run, _ = ProfileFetches(_state(fetch.client).settings_store.pool, lambda: NOZZLES).open(
        fetch.type_id, actor="person:test"
    )
    fetch.run_id = run.id


ONLY_04 = [{"printer_model": "H2C", "nozzle": n, "reason": "x"} for n in ("0.2", "0.6", "0.8")]


@respx.mock
def test_a_refetch_of_changed_content_is_a_new_version_sent_in_place(fetch: Fetch) -> None:
    """#7176 Decision 3 and #7174: a re-fetch whose cleaned document differs is version 2
    of the same preset (the type's ref stays), its notes name the keys that changed, and
    the mirror rewrites Bambuddy's preset in place under the id spools and slots name."""
    [body] = put_all(fetch, ["0.4"])
    created = mirror(first_id=41)
    no_k_profiles()
    assert fetch.import_(ONLY_04).status_code == 200
    [first] = own_presets(fetch)
    ref = refs(fetch)

    _next_run(fetch)
    changed = export("0.4", nozzle_temperature=["225"], filament_flow_ratio=["0.95"])
    assert fetch.put(changed, nozzle="0.4").status_code == 204
    updated = respx.put(f"{API}/local-presets/41").mock(
        return_value=httpx.Response(
            200, json={"id": 41, "name": name_of(body), "preset_type": "filament"}
        )
    )

    r = fetch.import_(ONLY_04, key="k2")

    assert r.status_code == 200, r.text
    assert r.json()["notes"] == [
        f"H2C 0.4 mm: version 2 of {name_of(body)}; changed filament_flow_ratio, nozzle_temperature"
    ]
    [now] = own_presets(fetch)
    assert (now["id"], now["version"], now["bb_preset_id"]) == (first["id"], 2, 41)
    assert own_doc(fetch, now)["nozzle_temperature"] == ["225"]
    assert refs(fetch) == ref
    assert (created.call_count, updated.call_count) == (1, 1)
    assert json.loads(updated.calls.last.request.content)["setting"]["nozzle_temperature"] == [
        "225"
    ]
    versions = fetch.rows(
        "SELECT version FROM filament_preset_versions WHERE preset_id = %s ORDER BY version",
        first["id"],
    )
    assert [v["version"] for v in versions] == [1, 2]


@respx.mock
def test_a_refetch_of_the_same_content_changes_nothing(fetch: Fetch) -> None:
    [body] = put_all(fetch, ["0.4"])
    created = mirror(first_id=41)
    no_k_profiles()
    assert fetch.import_(ONLY_04).status_code == 200

    _next_run(fetch)
    assert fetch.put(body, nozzle="0.4").status_code == 204
    r = fetch.import_(ONLY_04, key="k2")

    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["presets"], r.json()["notes"]) == ("partial", 1, [])
    [preset] = own_presets(fetch)
    assert preset["version"] == 1
    assert created.call_count == 1


@respx.mock
def test_a_mirror_refusal_never_fails_the_import(fetch: Fetch) -> None:
    """#7176 Decision 11: Bambuddy refusing the copy is kept on the preset; the run ends
    as it would have, and the next refresh sends it again."""
    put_all(fetch, ["0.4"])
    mirror(status_code=403)
    no_k_profiles()
    r = fetch.import_(ONLY_04)
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["presets"]) == ("partial", 1)
    [preset] = own_presets(fetch)
    assert preset["bb_preset_id"] is None and preset["mirrored_sha256"] is None
    assert preset["mirror_error"] is not None
    assert refs(fetch) == [f"sb-{preset['id']}"]


@respx.mock
def test_no_mirror_when_the_setting_is_off(fetch: Fetch) -> None:
    configure(fetch.client, filament_preset_mirror=False)
    put_all(fetch, ["0.4"])
    sent = mirror()
    no_k_profiles()
    assert fetch.import_(ONLY_04).status_code == 200
    assert sent.call_count == 0
    [preset] = own_presets(fetch)
    assert preset["bb_preset_id"] is None


@respx.mock
def test_the_types_own_temperatures_win(fetch: Fetch) -> None:
    """#7142: the type's nozzle range replaces 3DFP's and the nozzle temperatures are
    clamped into it, each a note."""
    fetch.rows(
        "UPDATE filament_spool_types SET nozzle_temp_min = 200, nozzle_temp_max = 220"
        " WHERE id = %s RETURNING id",
        fetch.type_id,
    )
    body = export(
        nozzle_temperature_range_low=["190"],
        nozzle_temperature_range_high=["230"],
        nozzle_temperature=["225"],
        nozzle_temperature_initial_layer=["195"],
    )
    assert fetch.put(body).status_code == 204
    mirror()
    no_k_profiles()
    r = fetch.import_(ONLY_04)
    assert r.status_code == 200, r.text
    [preset] = own_presets(fetch)
    doc = own_doc(fetch, preset)
    assert (
        doc["nozzle_temperature_range_low"],
        doc["nozzle_temperature_range_high"],
        doc["nozzle_temperature"],
        doc["nozzle_temperature_initial_layer"],
    ) == (["200"], ["220"], ["220"], ["200"])
    assert r.json()["notes"] == [
        "H2C 0.4 mm: nozzle_temperature_range_low is the type's 200, not 3DFP's 190",
        "H2C 0.4 mm: nozzle_temperature_range_high is the type's 220, not 3DFP's 230",
        "H2C 0.4 mm: nozzle_temperature is 220, inside the type's range, not 3DFP's 225",
        "H2C 0.4 mm: nozzle_temperature_initial_layer is 200, inside the type's range,"
        " not 3DFP's 195",
    ]


@respx.mock
def test_a_missing_key_makes_the_run_partial(fetch: Fetch) -> None:
    put_all(fetch, ["0.4", "0.6", "0.8"])
    mirror()
    no_k_profiles()
    reason = "3DFP offers no 0.2 mm nozzle for H2C"
    r = fetch.import_([{"printer_model": "H2C", "nozzle": "0.2", "reason": reason}])
    assert r.status_code == 200, r.text
    missing = [{"printer_model": "H2C", "nozzle_diameter": "0.2", "reason": reason}]
    assert (r.json()["status"], r.json()["presets"], r.json()["missing"]) == ("partial", 3, missing)
    run = fetch.run()
    assert (run.status, [m.model_dump() for m in run.missing]) == ("partial", missing)
    assert [p[1] for p in fetch.presets()] == ["0.4", "0.6", "0.8"]


@respx.mock
def test_other_keys_are_kept(fetch: Fetch) -> None:
    types = _state(fetch.client).components.get(FILAMENT).types
    types.set_presets(
        fetch.type_id,
        [
            TypePreset(
                type_id=fetch.type_id,
                printer_model="X1C",
                nozzle_diameter="0.4",
                slicer_filament="7",
                slicer_filament_name="mine",
            )
        ],
        actor="ann",
    )
    put_all(fetch)
    mirror()
    no_k_profiles()
    assert fetch.import_().status_code == 200
    assert ("X1C", "0.4", "7", "mine") in fetch.presets()
    assert len(fetch.presets()) == 5


def _profile(filament_id: str, k: str, slot: int = 5, extruder: int = 0) -> dict[str, Any]:
    return {
        "slot_id": slot,
        "extruder_id": extruder,
        "nozzle_id": "HS00-0.4",
        "nozzle_diameter": "0.4",
        "filament_id": filament_id,
        "name": "Inland PLA+",
        "k_value": k,
        "setting_id": "PFUS9",
    }


@respx.mock
def test_k_values_need_an_exact_non_gf_match(fetch: Fetch) -> None:
    state = _state(fetch.client)
    other = state.components.get(PRINTERS).store.create_linked(
        serial="31B598000001", name="H2C 2", model="H2C", bambuddy_printer_id=2
    )
    bodies = [export("0.2", filament_id="GFL99"), export("0.4"), export("0.6"), export("0.8")]
    for nozzle, body in zip(NOZZLES, bodies, strict=True):
        assert fetch.put(body, nozzle=nozzle).status_code == 204
    mirror()
    tables = {
        "0.2": httpx.Response(200, json={"profiles": [_profile("GFL99", "0.03")]}),
        "0.4": httpx.Response(
            200, json={"profiles": [_profile("P1a2b3c4", "0.025"), _profile("GFA00", "0.04")]}
        ),
        "0.6": httpx.Response(
            200,
            json={"profiles": [_profile("P1a2b3c4", "0.02"), _profile("P1a2b3c4", "0.03", 6)]},
        ),
        "0.8": httpx.Response(502, json={"detail": "printer offline"}),
    }

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/printers/2/kprofiles/"):
            return httpx.Response(403, json={"detail": "Forbidden"})
        return tables[request.url.params["nozzle_diameter"]]

    respx.get(url__regex=KPROFILES.pattern).mock(side_effect=answer)

    r = fetch.import_()

    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["k_values"]) == ("imported", 1)
    [k] = state.components.get(FILAMENT).types.k_values(fetch.type_id)
    assert (
        str(k.printer_id),
        k.extruder,
        k.nozzle_diameter,
        k.nozzle_type,
        k.k_value,
        k.cali_idx,
        k.name,
        k.setting_id,
    ) == (fetch.printer_id, 0, "0.4", "HS00-0.4", Decimal("0.0250"), 5, "Inland PLA+", "PFUS9")
    notes = r.json()["notes"]
    assert any("0.8" in n and "H2C" in n for n in notes), notes
    # 0.2's preset has a Bambu system id: no printer is asked for it.
    assert sum(1 for n in notes if "H2C 2" in n) == 3, notes
    assert other.id != fetch.printer_id


def _k_row(fetch: Fetch, printer_id: str, nozzle: str, k: str, extruder: int = 0) -> TypeKValue:
    return TypeKValue(
        type_id=fetch.type_id,
        printer_id=UUID(printer_id),
        extruder=extruder,
        nozzle_diameter=nozzle,
        nozzle_type=None,
        k_value=Decimal(k),
        name="old",
        cali_idx=None,
        setting_id=None,
    )


def _seed_k(fetch: Fetch, rows: list[tuple[TypeKValue, str]]) -> None:
    """The type's K rows, each with the ``source`` given (a person's save, then each row
    re-tagged, as the fill, a calibration or an earlier fetch would have left it)."""
    state = _state(fetch.client)
    state.components.get(FILAMENT).types.set_k_values(
        fetch.type_id, [row for row, _ in rows], actor="ann"
    )
    with state.settings_store.pool.connection() as conn:
        for row, source in rows:
            conn.execute(
                "UPDATE filament_spool_type_k_values SET source = %s WHERE type_id = %s"
                " AND printer_id = %s AND extruder = %s AND nozzle_diameter = %s",
                (source, fetch.type_id, row.printer_id, row.extruder, row.nozzle_diameter),
            )


def _k_table_answer(request: httpx.Request) -> httpx.Response:
    """The H2C's K table: 0.4 matches the fetched preset, 0.8 does not answer, the rest
    hold no match."""
    nozzle = request.url.params["nozzle_diameter"]
    if nozzle == "0.8":
        return httpx.Response(502, json={"detail": "printer offline"})
    if nozzle == "0.4":
        return httpx.Response(200, json={"profiles": [_profile("P1a2b3c4", "0.025")]})
    return httpx.Response(200, json={"profiles": []})


@respx.mock
def test_a_stale_k_value_of_a_fetched_key_is_removed_and_named(fetch: Fetch) -> None:
    """R-F4, narrowed by Decision 127 (#6857): a fetch replaces its own (``profile_fetch``)
    K rows of every key it fetched. Its old row of a fetched (printer, extruder, nozzle) key
    the new preset does not match is removed, and the run's notes name it; a key whose K
    table did not answer, and a key not fetched, keep theirs. A row a person, the fill or a
    printer calibration set is never removed."""
    state = _state(fetch.client)
    types = state.components.get(FILAMENT).types
    x1c = state.components.get(PRINTERS).store.create_linked(
        serial="31B598000002", name="X1C", model="X1C", bambuddy_printer_id=3
    )
    _seed_k(
        fetch,
        [
            (_k_row(fetch, fetch.printer_id, "0.2", "0.03"), "profile_fetch"),  # GF now: removed
            (_k_row(fetch, fetch.printer_id, "0.4", "0.05", extruder=1), "profile_fetch"),  # gone
            (_k_row(fetch, fetch.printer_id, "0.4", "0.04"), "profile_fetch"),  # matched: replaced
            (_k_row(fetch, fetch.printer_id, "0.8", "0.06"), "profile_fetch"),  # K table failed
            (_k_row(fetch, x1c.id, "0.4", "0.07"), "profile_fetch"),  # not fetched: kept
            (_k_row(fetch, fetch.printer_id, "0.2", "0.08", extruder=1), "person"),  # kept
            (_k_row(fetch, fetch.printer_id, "0.6", "0.09"), "printer_calibration"),  # kept
            (_k_row(fetch, fetch.printer_id, "0.6", "0.01", extruder=1), "profile"),  # kept
        ],
    )
    bodies = [export("0.2", filament_id="GFL99"), export("0.4"), export("0.6"), export("0.8")]
    for nozzle, body in zip(NOZZLES, bodies, strict=True):
        assert fetch.put(body, nozzle=nozzle).status_code == 204
    mirror()
    respx.get(url__regex=KPROFILES.pattern).mock(side_effect=_k_table_answer)

    r = fetch.import_()

    assert r.status_code == 200, r.text
    kept = {
        (str(k.printer_id), k.extruder, k.nozzle_diameter): (k.k_value, k.source)
        for k in types.k_values(fetch.type_id)
    }
    assert kept == {
        (fetch.printer_id, 0, "0.4"): (Decimal("0.0250"), "profile_fetch"),
        (fetch.printer_id, 0, "0.8"): (Decimal("0.0600"), "profile_fetch"),
        (x1c.id, 0, "0.4"): (Decimal("0.0700"), "profile_fetch"),
        (fetch.printer_id, 1, "0.2"): (Decimal("0.0800"), "person"),
        (fetch.printer_id, 0, "0.6"): (Decimal("0.0900"), "printer_calibration"),
        (fetch.printer_id, 1, "0.6"): (Decimal("0.0100"), "profile"),
    }
    removed = [n for n in r.json()["notes"] if n.startswith("removed")]
    assert sorted(removed) == [
        "removed the old K value 0.0300 of H2C (H2C), extruder 0, 0.2 mm:"
        " the fetched preset does not match it",
        "removed the old K value 0.0500 of H2C (H2C), extruder 1, 0.4 mm:"
        " the fetched preset does not match it",
    ]
    assert fetch.run().notes == r.json()["notes"]


@respx.mock
@pytest.mark.parametrize(
    ("source", "why"),
    [
        ("person", "a person set it"),
        ("profile", "it came from the linked Bambuddy spools"),
        ("printer_calibration", "it is the printer's calibration"),
    ],
)
def test_a_fetch_keeps_a_k_value_it_did_not_set(fetch: Fetch, source: str, why: str) -> None:
    """#6857 (Decision 127): a fetched K value never replaces a row of its key that a person,
    the fill or a printer calibration set: the row keeps its value and its source, the
    run's notes say so, and the run counts only the K values it wrote."""
    types = _state(fetch.client).components.get(FILAMENT).types
    _seed_k(fetch, [(_k_row(fetch, fetch.printer_id, "0.4", "0.04"), source)])
    put_all(fetch)
    mirror()
    respx.get(url__regex=KPROFILES.pattern).mock(side_effect=_k_table_answer)

    r = fetch.import_()

    assert r.status_code == 200, r.text
    assert r.json()["k_values"] == 0
    [k] = types.k_values(fetch.type_id)
    assert (k.k_value, k.source, k.name) == (Decimal("0.0400"), source, "old")
    assert (
        f"kept the K value 0.0400 of H2C (H2C), extruder 0, 0.4 mm, not the fetched 0.025: {why}"
    ) in r.json()["notes"]
    assert fetch.run().k_values == 0


@respx.mock
def test_import_twice_with_one_key_imports_once(fetch: Fetch) -> None:
    put_all(fetch)
    sent = mirror()
    no_k_profiles()
    first = fetch.import_()
    again = fetch.import_()
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert sent.call_count == 4
    # Another key, after the run ended: the stored outcome, and nothing written again.
    other = fetch.import_(key="k2")
    assert (other.status_code, other.json()) == (200, first.json())
    assert sent.call_count == 4
    assert len(own_presets(fetch)) == 4


@respx.mock
def test_a_retried_import_of_an_imported_run_calls_bambuddy_no_more(fetch: Fetch) -> None:
    """R-F2: a run already ``imported`` answers its stored outcome, under its first key or
    another one, and Bambuddy is not asked anything again."""
    put_all(fetch)
    mirror()
    no_k_profiles()
    first = fetch.import_()
    assert first.json()["status"] == "imported"
    calls = respx.calls.call_count
    for key in ("k1", "k2"):
        again = fetch.import_(key=key)
        assert (again.status_code, again.json()) == (200, first.json())
    assert respx.calls.call_count == calls


def _slow_k_tables(reached: threading.Event, release: threading.Event) -> respx.Route:
    """The printers' K tables, the import's one slow step: the first read waits."""

    async def slow(request: httpx.Request) -> httpx.Response:
        reached.set()
        await asyncio.to_thread(release.wait, 30)
        return httpx.Response(200, json={"profiles": []})

    return respx.get(url__regex=KPROFILES.pattern).mock(side_effect=slow)


@respx.mock
def test_two_concurrent_imports_of_a_run_write_once(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-F2: the worker's client retries a slow import with the same key while the first
    request still runs. The second waits for the run's import lease and then answers the first
    one's outcome: one set of K table reads and one write, never two in parallel."""
    from scadbuddy.filament.profile_fill import TypeFiller

    async def no_fill(self: Any, settings: Any, type_ids: Any, **kwargs: Any) -> list[Any]:
        return []

    # The first presets also fill the type (more K table reads): not what this counts.
    monkeypatch.setattr(TypeFiller, "fill", no_fill)
    put_all(fetch)
    reached, release = threading.Event(), threading.Event()
    tables = _slow_k_tables(reached, release)
    mirror()
    replies: dict[str, Any] = {}

    def call(name: str) -> None:
        replies[name] = fetch.import_()

    first = threading.Thread(target=call, args=("first",))
    first.start()
    assert reached.wait(30)
    second = threading.Thread(target=call, args=("second",))
    second.start()
    # Long enough for an unlocked second request to reach Bambuddy as well.
    second.join(timeout=1.5)
    release.set()
    first.join(timeout=60)
    second.join(timeout=60)
    assert replies["first"].status_code == 200, replies["first"].text
    assert replies["second"].status_code == 200, replies["second"].text
    assert replies["second"].json() == replies["first"].json()
    assert replies["first"].json()["status"] == "imported"
    assert tables.call_count == 4
    assert [p["version"] for p in own_presets(fetch)] == [1, 1, 1, 1]


@respx.mock
def test_a_waiting_import_holds_no_pool_connection(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-PR3: a request that finds the run's import lease held waits with no pooled
    connection (it polls with short statements), and the holder holds none while Bambuddy
    takes its time. Each checkout of the pool is timed: over the wait, no one checkout
    covers more than half of it (a held connection would cover all of it). The longest,
    not the sum: on a slow runner the short polls add up to more than the wait (#7593)."""
    monkeypatch.setattr(profile_fetch, "LEASE_POLL_SECONDS", 0.1)
    pool = _state(fetch.client).settings_store.pool
    put_all(fetch)
    reached, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    open_since: dict[int, float] = {}
    held: list[tuple[float, float]] = []
    ids = itertools.count()
    real_connection = pool.connection

    @contextlib.contextmanager
    def timed(*args: Any, **kwargs: Any) -> Iterator[Any]:
        token = next(ids)
        began = time.monotonic()
        with lock:
            open_since[token] = began
        try:
            with real_connection(*args, **kwargs) as conn:
                yield conn
        finally:
            with lock:
                del open_since[token]
                held.append((began, time.monotonic()))

    monkeypatch.setattr(pool, "connection", timed)
    _slow_k_tables(reached, release)
    mirror()
    replies: dict[str, Any] = {}

    def call(name: str) -> None:
        replies[name] = fetch.import_()

    def polls() -> int:
        with lock:
            return len(held)

    def until(done: Callable[[], bool]) -> bool:
        deadline = time.monotonic() + 30
        while not done():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    first = threading.Thread(target=call, args=("first",))
    first.start()
    assert reached.wait(30)
    # The holder now waits on Bambuddy with no checkout, so every new one is the waiter's.
    seen = polls()
    second = threading.Thread(target=call, args=("second",))
    second.start()
    # The window opens at the waiter's first poll: on a loaded runner its request can take
    # longer than the window to reach the lease at all (#8010).
    assert until(lambda: polls() > seen), "the waiting import never polled"

    def longest(since: float) -> float:
        """The longest part of the time since ``since`` that one checkout covered."""
        with lock:
            now = time.monotonic()
            spans = [*held, *((began, now) for began in open_since.values())]
        overlaps = [min(end, now) - max(began, since) for began, end in spans if end > since]
        return max(overlaps, default=0.0)

    wait = 1.5
    polls_before, start = polls(), time.monotonic()
    time.sleep(wait)
    covered = longest(start)
    # It keeps polling; how many polls fit in the window depends on the runner's load.
    kept_polling = until(lambda: polls() - polls_before >= 3)
    polls_after = polls()
    release.set()
    first.join(timeout=60)
    second.join(timeout=60)
    assert replies["second"].json() == replies["first"].json()
    assert kept_polling, (polls_before, polls_after)
    assert covered < wait / 2, covered


@respx.mock
def test_an_expired_lease_is_claimed(fetch: Fetch, monkeypatch: pytest.MonkeyPatch) -> None:
    """R-PR3: a lease a crashed request left behind holds the import off only until it
    expires; then the waiting request claims it and imports, and the end clears it."""
    monkeypatch.setattr(profile_fetch, "LEASE_POLL_SECONDS", 0.05)
    put_all(fetch)
    mirror()
    no_k_profiles()
    fetch.rows(
        "UPDATE filament_profile_fetches SET import_lease_until = now() + interval '1 second'"
        " WHERE id = %s RETURNING id",
        fetch.run_id,
    )
    began = time.monotonic()

    r = fetch.import_()

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "imported"
    assert time.monotonic() - began >= 0.5
    assert len(own_presets(fetch)) == 4
    [row] = fetch.rows(
        "SELECT import_lease_until FROM filament_profile_fetches WHERE id = %s", fetch.run_id
    )
    assert row["import_lease_until"] is None


def test_import_needs_its_key(fetch: Fetch) -> None:
    assert fetch.import_(key=None).status_code == 428


@respx.mock
def test_failed_ends_the_run_with_its_reason(fetch: Fetch) -> None:
    r = fetch.failed("no profile-fetch worker picked the fetch up")
    assert r.status_code == 204, r.text
    run = fetch.run()
    assert (run.status, run.error) == ("failed", "no profile-fetch worker picked the fetch up")
    assert fetch.failed("again").status_code == 409


# -- the import fans out to the type's spools (R-T9-3) ---------------------------------------


def _k_table(request: httpx.Request) -> httpx.Response:
    """One K row, for the 0.4 nozzle, that the fetched preset matches."""
    if request.url.params["nozzle_diameter"] == "0.4":
        return httpx.Response(200, json={"profiles": [_profile("P1a2b3c4", "0.025")]})
    return httpx.Response(200, json={"profiles": []})


def _edits(fetch: Fetch, spool_id: UUID) -> list[Any]:
    spools = _state(fetch.client).components.get(FILAMENT).spools
    return [e for e in spools.events(spool_id) if e.kind == "edited"]


@respx.mock
def test_an_import_writes_edited_on_spools_that_inherit(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A type edit fans out (spec §5.7): the import's preset and K-value change each write
    one ``edited`` event, as the run's starter, on a spool that inherits it; a spool that
    sets every changed key itself gets none. The image's vendor tree is left out: with it
    (in CI's image), the type's fill from its new ScadBuddy preset writes a third edit of
    its own (the fill after the commit, `test_the_first_presets_an_import_sets_fill_the_type`)."""
    filament = _state(fetch.client).components.get(FILAMENT)
    monkeypatch.setattr(filament, "profile_table", lambda: None)
    spools = filament.spools
    heir = spools.create(SpoolCreate(type_id=fetch.type_id), actor="ann").id
    own = spools.create(SpoolCreate(type_id=fetch.type_id), actor="ann").id
    spools.set_presets(
        own,
        [
            SpoolPresetCreate(printer_model="H2C", nozzle_diameter=n, slicer_filament="MINE")
            for n in NOZZLES
        ],
        actor="ann",
    )
    spools.set_k_values(
        own,
        [
            SpoolKValueCreate(
                printer_id=UUID(fetch.printer_id),
                extruder=0,
                nozzle_diameter="0.4",
                k_value=Decimal("0.01"),
            )
        ],
        actor="ann",
    )
    before = {s: len(_edits(fetch, s)) for s in (heir, own)}
    put_all(fetch)
    mirror()
    respx.get(url__regex=KPROFILES.pattern).mock(side_effect=_k_table)

    r = fetch.import_()

    assert r.status_code == 200, r.text
    assert r.json()["k_values"] == 1
    events = _edits(fetch, heir)[before[heir] :]
    assert [sorted(e.data["changes"]) for e in events] == [["presets"], ["k_values"]]
    assert {e.actor for e in events} == {"person:test"}
    assert {e.data["type_id"] for e in events} == {str(fetch.type_id)}
    old, new = events[0].data["changes"]["presets"]
    assert old == []
    assert [p["slicer_filament"] for p in new] == [f"sb-{p['id']}" for p in own_presets(fetch)]
    old, new = events[1].data["changes"]["k_values"]
    assert old == []
    assert [Decimal(str(k["k_value"])) for k in new] == [Decimal("0.025")]
    assert _edits(fetch, own)[before[own] :] == []


@respx.mock
def test_an_import_that_changes_nothing_writes_no_event(fetch: Fetch) -> None:
    bodies = put_all(fetch)
    mirror()
    no_k_profiles()
    assert fetch.import_().status_code == 200
    spools = _state(fetch.client).components.get(FILAMENT).spools
    heir = spools.create(SpoolCreate(type_id=fetch.type_id), actor="ann").id
    before = len(_edits(fetch, heir))

    _next_run(fetch)
    for nozzle, body in zip(NOZZLES, bodies, strict=True):
        assert fetch.put(body, nozzle=nozzle).status_code == 204
    r = fetch.import_(key="k2")

    assert r.status_code == 200, r.text
    assert len(fetch.presets()) == 4
    assert _edits(fetch, heir)[before:] == []
