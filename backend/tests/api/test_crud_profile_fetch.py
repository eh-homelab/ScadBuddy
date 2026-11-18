"""#6625 Task 3: the profile-fetch worker's CRUD routes and the import they end in. The
worker uploads a run's preset files; the API holds them as blobs, imports them into
Bambuddy as one zip, and sets the type's presets and K values. Bambuddy is respx only."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
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
from scadbuddy.filament.component import FILAMENT
from scadbuddy.filament.models import TypePreset
from scadbuddy.filament.profile_fetch import HOLDER, ProfileFetches
from scadbuddy.printers.component import PRINTERS
from tests.api.test_send import API, configure
from tests.filament.test_types import _body

pytestmark = pytest.mark.requires_postgres

RUNS = "/api/v1/internal/crud/profile-fetch/runs"
EXPORT = Path(__file__).parents[1] / "profile_fetch" / "site" / "export-h2c-0.4.json"
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


def local_presets(names: list[str], first_id: int = 101) -> respx.Route:
    rows = [
        {"id": first_id + i, "name": n, "preset_type": "filament", "source": "import"}
        for i, n in enumerate(names)
    ]
    return respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json={"filament": rows, "printer": [], "process": []})
    )


def bb_import(imported: int, skipped: int = 0, status: int = 200) -> respx.Route:
    body: Any = (
        {"success": True, "imported": imported, "skipped": skipped, "errors": []}
        if status == 200
        else {"detail": "Forbidden"}
    )
    return respx.post(f"{API}/local-presets/import").mock(
        return_value=httpx.Response(status, json=body)
    )


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
    elif case == "not a target":
        r = fetch.put(body, model="X1C")
    else:
        assert fetch.failed("gave up").status_code == 204
        r = fetch.put(body)
        assert r.json()["code"] == "not_running"
    assert r.status_code == status, r.text
    if case != "not running":
        assert fetch.rows("SELECT key FROM blob_refs WHERE holder_kind = %s", HOLDER) == []


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
def test_import_posts_one_zip_and_sets_the_type_presets(fetch: Fetch) -> None:
    bodies = put_all(fetch)
    sent = bb_import(4)
    local_presets([name_of(b) for b in bodies])
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
    archive = zipfile.ZipFile(io.BytesIO(_zip_of(sent.calls.last.request)))
    assert sorted(archive.namelist()) == sorted(
        f"{hashlib.sha256(b).hexdigest()}.json" for b in bodies
    )
    assert fetch.presets() == [
        ("H2C", n, str(101 + i), name_of(bodies[i])) for i, n in enumerate(NOZZLES)
    ]
    run = fetch.run()
    assert (run.status, run.finished_at is not None) == ("imported", True)


def _zip_of(request: httpx.Request) -> bytes:
    """The ``file`` part of a multipart request."""
    body = request.content
    start = body.index(b"PK\x03\x04")
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    end = body.index(b"\r\n--" + boundary, start)
    return body[start:end]


@respx.mock
def test_a_name_bb_already_has_maps_to_its_id(fetch: Fetch) -> None:
    [body] = put_all(fetch, ["0.4"])
    bb_import(0, skipped=1)
    local_presets(["Other", name_of(body)], first_id=40)
    no_k_profiles()
    r = fetch.import_(
        [{"printer_model": "H2C", "nozzle": n, "reason": "x"} for n in ("0.2", "0.6", "0.8")]
    )
    assert r.status_code == 200, r.text
    assert ("H2C", "0.4", "41", name_of(body)) in fetch.presets()


@respx.mock
def test_a_missing_key_makes_the_run_partial(fetch: Fetch) -> None:
    bodies = put_all(fetch, ["0.4", "0.6", "0.8"])
    bb_import(3)
    local_presets([name_of(b) for b in bodies])
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
    )
    bodies = put_all(fetch)
    bb_import(4)
    local_presets([name_of(b) for b in bodies])
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
    bb_import(4)
    local_presets([name_of(b) for b in bodies])
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


@respx.mock
def test_bb_refusing_the_import_fails_the_run(fetch: Fetch) -> None:
    put_all(fetch)
    bb_import(0, status=403)
    r = fetch.import_()
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["presets"]) == ("failed", 0)
    run = fetch.run()
    assert run.status == "failed"
    assert run.error is not None and "Manage Library" in run.error
    assert fetch.presets() == []


@respx.mock
def test_import_twice_with_one_key_imports_once(fetch: Fetch) -> None:
    bodies = put_all(fetch)
    sent = bb_import(4)
    local_presets([name_of(b) for b in bodies])
    no_k_profiles()
    first = fetch.import_()
    again = fetch.import_()
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert sent.call_count == 1
    # Another key, after the run ended: the stored outcome, and no second import.
    other = fetch.import_(key="k2")
    assert (other.status_code, other.json()) == (200, first.json())
    assert sent.call_count == 1


def test_import_needs_its_key(fetch: Fetch) -> None:
    assert fetch.import_(key=None).status_code == 428


@respx.mock
def test_the_held_copy_is_refreshed_after_import(fetch: Fetch) -> None:
    state = _state(fetch.client)
    state.settings = state.settings.model_copy(update={"spool_overlay_refresh_seconds": 300})
    bodies = put_all(fetch)
    bb_import(4)
    local_presets([name_of(b) for b in bodies])
    no_k_profiles()
    details = respx.get(url__regex=rf"{re.escape(API)}/local-presets/(\d+)$").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "id": int(request.url.path.rsplit("/", 1)[1]),
                "name": "n",
                "preset_type": "filament",
                "source": "import",
                "setting": {"a": "b"},
            },
        )
    )
    assert fetch.import_().status_code == 200
    assert details.call_count == 4
    held = fetch.rows("SELECT id FROM held_local_presets ORDER BY id")
    assert [row["id"] for row in held] == [101, 102, 103, 104]


@respx.mock
def test_failed_ends_the_run_with_its_reason(fetch: Fetch) -> None:
    r = fetch.failed("no profile-fetch worker picked the fetch up")
    assert r.status_code == 204, r.text
    run = fetch.run()
    assert (run.status, run.error) == ("failed", "no profile-fetch worker picked the fetch up")
    assert fetch.failed("again").status_code == 409
