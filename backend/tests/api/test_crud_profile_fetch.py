"""#6625 Task 3: the profile-fetch worker's CRUD routes and the import they end in. The
worker uploads a run's preset files; the API holds them as blobs, imports them into
Bambuddy as one zip, and sets the type's presets and K values. Bambuddy is respx only."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import itertools
import json
import re
import threading
import time
import zipfile
from collections.abc import Iterator
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


def _rows(names: list[str], first_id: int) -> list[dict[str, Any]]:
    return [
        {"id": first_id + i, "name": n, "preset_type": "filament", "source": "import"}
        for i, n in enumerate(names)
    ]


def local_presets(
    names: list[str], first_id: int = 101, before: list[dict[str, Any]] | None = None
) -> respx.Route:
    """Bambuddy's local presets: ``before`` (none by default) on the first listing, the
    import's run reads before it imports; ``before`` and ``names`` (ids from
    ``first_id``) on every later one."""
    return _listing(respx.get(f"{API}/local-presets/"), before or [], _rows(names, first_id))


def _listing(
    route: respx.Route, before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> respx.Route:
    calls = iter([before])

    def answer(request: httpx.Request) -> httpx.Response:
        rows = next(calls, before + after)
        return httpx.Response(200, json={"filament": rows, "printer": [], "process": []})

    return route.mock(side_effect=answer)


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
        ("no inherits", 422),
        ("empty inherits", 422),
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
    elif case == "no inherits":
        doc = json.loads(export())
        del doc["inherits"]
        r = fetch.put(json.dumps(doc).encode())
    elif case == "empty inherits":
        r = fetch.put(export(inherits=""))
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
    [row] = fetch.rows("SELECT files FROM filament_profile_fetches WHERE id = %s", fetch.run_id)
    assert sorted(archive.namelist()) == sorted(
        f"{e['sha256']}.json" for e in row["files"]["H2C"].values()
    )
    assert fetch.presets() == [
        ("H2C", n, str(101 + i), name_of(bodies[i])) for i, n in enumerate(NOZZLES)
    ]
    run = fetch.run()
    assert (run.status, run.finished_at is not None) == ("imported", True)
    # R-PR1: what the import made, kept in a commit of its own.
    [made] = fetch.rows(
        "SELECT bambuddy_made FROM filament_profile_fetches WHERE id = %s", fetch.run_id
    )
    assert made["bambuddy_made"] == [101, 102, 103, 104]


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
    bodies = put_all(fetch)
    bb_import(4)
    local_presets([name_of(b) for b in bodies])
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
    bodies = put_all(fetch)
    bb_import(4)
    local_presets([name_of(b) for b in bodies])
    no_k_profiles()
    with caplog.at_level("WARNING"):
        r = fetch.import_()
    assert r.status_code == 200, r.text
    assert fetch.run().status == "imported"
    [warning] = [r for r in caplog.records if r.levelname == "WARNING" and hasattr(r, "error")]
    assert warning.error == "KeyError"


def _zip_of(request: httpx.Request) -> bytes:
    """The ``file`` part of a multipart request."""
    body = request.content
    start = body.index(b"PK\x03\x04")
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    end = body.index(b"\r\n--" + boundary, start)
    return body[start:end]


@respx.mock
def test_the_zip_carries_no_site_gcode(fetch: Fetch) -> None:
    body = export(**GCODE)
    assert fetch.put(body).status_code == 204
    # The kept blob changed under the run (defence in depth: the import strips again).
    [row] = fetch.rows("SELECT files FROM filament_profile_fetches WHERE id = %s", fetch.run_id)
    path = _blob(fetch, row["files"]["H2C"]["0.4"]["key"])
    path.chmod(0o644)
    path.write_bytes(body)
    sent = bb_import(1)
    local_presets([name_of(body)])
    no_k_profiles()
    r = fetch.import_(
        [{"printer_model": "H2C", "nozzle": n, "reason": "x"} for n in ("0.2", "0.6", "0.8")]
    )
    assert r.status_code == 200, r.text
    archive = zipfile.ZipFile(io.BytesIO(_zip_of(sent.calls.last.request)))
    [entry] = archive.namelist()
    doc = json.loads(archive.read(entry))
    assert [k for k in doc if k.endswith("gcode") or k == "post_process"] == []
    assert doc["name"] == name_of(body)


@respx.mock
def test_a_hand_made_preset_of_the_same_name_is_not_bound(fetch: Fetch) -> None:
    clash, fresh = put_all(fetch, ["0.4", "0.6"])
    bb_import(1, skipped=1)
    local_presets([name_of(fresh)], first_id=101, before=_rows(["Other", name_of(clash)], 40))
    no_k_profiles()
    r = fetch.import_(
        [{"printer_model": "H2C", "nozzle": n, "reason": "x"} for n in ("0.2", "0.8")]
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "partial"
    reason = f"Bambuddy already has a preset named {name_of(clash)} that no fetch of this type made"
    assert {"printer_model": "H2C", "nozzle_diameter": "0.4", "reason": reason} in r.json()[
        "missing"
    ]
    assert fetch.presets() == [("H2C", "0.6", "101", name_of(fresh))]


@respx.mock
def test_a_preset_an_earlier_fetch_set_is_bound_again(fetch: Fetch) -> None:
    failures = [{"printer_model": "H2C", "nozzle": n, "reason": "x"} for n in ("0.2", "0.6", "0.8")]
    [body] = put_all(fetch, ["0.4"])
    bb_import(1)
    listing = local_presets([name_of(body)], first_id=41, before=_rows(["Other"], 40))
    no_k_profiles()
    first = fetch.import_(failures)
    assert first.json()["notes"] == [], first.text
    assert fetch.presets() == [("H2C", "0.4", "41", name_of(body))]

    run, _ = ProfileFetches(_state(fetch.client).settings_store.pool, lambda: NOZZLES).open(
        fetch.type_id, actor="person:test"
    )
    fetch.run_id = run.id
    assert fetch.put(body, nozzle="0.4").status_code == 204
    bb_import(0, skipped=1)
    both = _rows(["Other", name_of(body)], 40)
    _listing(listing, both, [])

    r = fetch.import_(failures, key="k2")

    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["presets"]) == ("partial", 1), r.text
    assert r.json()["notes"] == [
        f"Bambuddy kept its existing preset {name_of(body)}; its content was not updated"
    ]
    assert fetch.presets() == [("H2C", "0.4", "41", name_of(body))]


@respx.mock
def test_a_retry_binds_what_its_lost_first_attempt_made(fetch: Fetch) -> None:
    bodies = put_all(fetch)
    sent = bb_import(0, status=502)
    listing = local_presets([])
    assert fetch.import_().status_code == 502
    # Bambuddy made the presets, but its answer was lost; the retry finds them listed.
    sent.mock(
        return_value=httpx.Response(
            200, json={"success": True, "imported": 0, "skipped": 4, "errors": []}
        )
    )
    _listing(listing, _rows([name_of(b) for b in bodies], 101), [])
    no_k_profiles()

    r = fetch.import_()

    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["presets"]) == ("imported", 4)
    assert [p[2] for p in fetch.presets()] == ["101", "102", "103", "104"]


@respx.mock
def test_presets_an_unfinished_import_made_are_bound_by_the_next_run(fetch: Fetch) -> None:
    """R-PR1: Bambuddy takes the zip, then every listing after it answers 5xx until the
    run fails. The presets it made sit in the next run's own ``bambuddy_before``; that run
    still binds them, as presets a fetch of this type made."""
    bodies = put_all(fetch)
    sent = bb_import(4)
    names = [name_of(b) for b in bodies]
    other, made = _rows(["Other"], 40), _rows(names, 101)
    answers = iter([other])

    def down(request: httpx.Request) -> httpx.Response:
        rows = next(answers, None)
        if rows is None:
            return httpx.Response(502, json={"detail": "Bad Gateway"})
        return httpx.Response(200, json={"filament": rows, "printer": [], "process": []})

    listing = respx.get(f"{API}/local-presets/").mock(side_effect=down)
    no_k_profiles()
    for _ in range(2):
        assert fetch.import_().status_code == 502
    [row] = fetch.rows(
        "SELECT import_lease_until FROM filament_profile_fetches WHERE id = %s", fetch.run_id
    )
    assert row["import_lease_until"] is None
    assert fetch.failed("the import did not finish").status_code == 204
    assert fetch.presets() == []

    run, _ = ProfileFetches(_state(fetch.client).settings_store.pool, lambda: NOZZLES).open(
        fetch.type_id, actor="person:test"
    )
    fetch.run_id = run.id
    put_all(fetch)
    sent.mock(
        return_value=httpx.Response(
            200, json={"success": True, "imported": 0, "skipped": 4, "errors": []}
        )
    )
    listing.mock(
        side_effect=lambda request: httpx.Response(
            200, json={"filament": other + made, "printer": [], "process": []}
        )
    )

    r = fetch.import_(key="k2")

    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["presets"]) == ("imported", 4), r.text
    assert [p[2] for p in fetch.presets()] == ["101", "102", "103", "104"]


@pytest.mark.parametrize(
    "refusal",
    [
        httpx.Response(403, json={"detail": "Forbidden"}),
        httpx.Response(
            200, json={"success": False, "imported": 0, "skipped": 0, "errors": ["bad zip"]}
        ),
    ],
    ids=["403", "not-success"],
)
@respx.mock
def test_a_refused_import_made_nothing_a_later_fetch_binds(
    fetch: Fetch, refusal: httpx.Response
) -> None:
    """R-PR5: Bambuddy refuses run A's import, so A made nothing. A person then makes a
    preset of one of A's file names; run B does not bind it as a fetch's."""
    [body] = put_all(fetch, ["0.4"])
    sent = respx.post(f"{API}/local-presets/import").mock(return_value=refusal)
    listing = local_presets([], before=_rows(["Other"], 40))
    first = fetch.import_()
    assert first.json()["status"] == "failed", first.text
    [row] = fetch.rows(
        "SELECT bambuddy_made FROM filament_profile_fetches WHERE id = %s", fetch.run_id
    )
    assert row["bambuddy_made"] == []

    run, _ = ProfileFetches(_state(fetch.client).settings_store.pool, lambda: NOZZLES).open(
        fetch.type_id, actor="person:test"
    )
    fetch.run_id = run.id
    assert fetch.put(body, nozzle="0.4").status_code == 204
    sent.mock(
        return_value=httpx.Response(
            200, json={"success": True, "imported": 0, "skipped": 1, "errors": []}
        )
    )
    _listing(listing, _rows(["Other", name_of(body)], 40), [])
    no_k_profiles()

    r = fetch.import_(key="k2")

    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["presets"]) == ("failed", 0), r.text
    reason = f"Bambuddy already has a preset named {name_of(body)} that no fetch of this type made"
    assert {"printer_model": "H2C", "nozzle_diameter": "0.4", "reason": reason} in r.json()[
        "missing"
    ]
    assert fetch.presets() == []


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
        actor="ann",
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
    bb_import(4)
    local_presets([name_of(b) for b in bodies])
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
    bb_import(4)
    local_presets([name_of(export(n)) for n in NOZZLES])
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
def test_bb_refusing_the_import_fails_the_run(fetch: Fetch) -> None:
    put_all(fetch)
    bb_import(0, status=403)
    local_presets([])
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


@respx.mock
def test_a_retried_import_of_an_imported_run_calls_bambuddy_no_more(fetch: Fetch) -> None:
    """R-F2: a run already ``imported`` answers its stored outcome, under its first key or
    another one, and Bambuddy is not asked anything again."""
    bodies = put_all(fetch)
    bb_import(4)
    local_presets([name_of(b) for b in bodies])
    no_k_profiles()
    first = fetch.import_()
    assert first.json()["status"] == "imported"
    calls = respx.calls.call_count
    for key in ("k1", "k2"):
        again = fetch.import_(key=key)
        assert (again.status_code, again.json()) == (200, first.json())
    assert respx.calls.call_count == calls


@respx.mock
def test_two_concurrent_imports_of_a_run_call_bambuddy_once(fetch: Fetch) -> None:
    """R-F2: the worker's client retries a slow import with the same key while the first
    request still runs. The second waits for the run's import lease and then answers the first
    one's outcome: one zip import, one set of listings, never two imports in parallel."""
    bodies = put_all(fetch)
    reached, release = threading.Event(), threading.Event()

    async def slow_import(request: httpx.Request) -> httpx.Response:
        reached.set()
        await asyncio.to_thread(release.wait, 30)
        return httpx.Response(
            200, json={"success": True, "imported": 4, "skipped": 0, "errors": []}
        )

    sent = respx.post(f"{API}/local-presets/import").mock(side_effect=slow_import)
    listing = local_presets([name_of(b) for b in bodies])
    no_k_profiles()
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
    assert sent.call_count == 1
    assert listing.call_count == 2


@respx.mock
def test_a_waiting_import_holds_no_pool_connection(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-PR3: a request that finds the run's import lease held waits with no pooled
    connection (it polls with short statements), and the holder holds none while Bambuddy
    takes its time. Each checkout of the pool is timed: over the wait, the time any
    connection is checked out stays a small part of it (a held connection would fill it)."""
    monkeypatch.setattr(profile_fetch, "LEASE_POLL_SECONDS", 0.1)
    pool = _state(fetch.client).settings_store.pool
    bodies = put_all(fetch)
    reached, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    open_since: dict[int, float] = {}
    held: list[float] = []
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
                held.append(time.monotonic() - began)

    monkeypatch.setattr(pool, "connection", timed)

    async def slow_import(request: httpx.Request) -> httpx.Response:
        reached.set()
        await asyncio.to_thread(release.wait, 30)
        return httpx.Response(
            200, json={"success": True, "imported": 4, "skipped": 0, "errors": []}
        )

    respx.post(f"{API}/local-presets/import").mock(side_effect=slow_import)
    local_presets([name_of(b) for b in bodies])
    no_k_profiles()
    replies: dict[str, Any] = {}

    def call(name: str) -> None:
        replies[name] = fetch.import_()

    first = threading.Thread(target=call, args=("first",))
    first.start()
    assert reached.wait(30)
    second = threading.Thread(target=call, args=("second",))
    second.start()

    def busy() -> tuple[float, int]:
        with lock:
            now = time.monotonic()
            return sum(held) + sum(now - t for t in open_since.values()), len(held)

    wait = 1.5
    before, polls_before = busy()
    time.sleep(wait)
    after, polls_after = busy()
    release.set()
    first.join(timeout=60)
    second.join(timeout=60)
    assert replies["second"].json() == replies["first"].json()
    assert polls_after - polls_before >= 3, (polls_before, polls_after)
    assert after - before < wait / 4, (before, after)


@respx.mock
def test_an_expired_lease_is_claimed(fetch: Fetch, monkeypatch: pytest.MonkeyPatch) -> None:
    """R-PR3: a lease a crashed request left behind holds the import off only until it
    expires; then the waiting request claims it and imports, and the end clears it."""
    monkeypatch.setattr(profile_fetch, "LEASE_POLL_SECONDS", 0.05)
    bodies = put_all(fetch)
    sent = bb_import(4)
    local_presets([name_of(b) for b in bodies])
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
    assert sent.call_count == 1
    [row] = fetch.rows(
        "SELECT import_lease_until FROM filament_profile_fetches WHERE id = %s", fetch.run_id
    )
    assert row["import_lease_until"] is None


def test_import_needs_its_key(fetch: Fetch) -> None:
    assert fetch.import_(key=None).status_code == 428


@respx.mock
def test_the_held_copy_is_refreshed_after_import(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scadbuddy.filament.profile_fill import TypeFiller

    async def no_fill(self: Any, settings: Any, type_ids: Any, **kwargs: Any) -> list[Any]:
        return []

    # The first presets also fill the type (one more local-preset read): not what this counts.
    monkeypatch.setattr(TypeFiller, "fill", no_fill)
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
def test_an_import_writes_edited_on_spools_that_inherit(fetch: Fetch) -> None:
    """A type edit fans out (spec §5.7): the import's preset and K-value change each write
    one ``edited`` event, as the run's starter, on a spool that inherits it; a spool that
    sets every changed key itself gets none."""
    spools = _state(fetch.client).components.get(FILAMENT).spools
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
    bodies = put_all(fetch)
    bb_import(4)
    local_presets([name_of(b) for b in bodies])
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
    assert [p["slicer_filament"] for p in new] == ["101", "102", "103", "104"]
    old, new = events[1].data["changes"]["k_values"]
    assert old == []
    assert [Decimal(str(k["k_value"])) for k in new] == [Decimal("0.025")]
    assert _edits(fetch, own)[before[own] :] == []


@respx.mock
def test_an_import_that_changes_nothing_writes_no_event(fetch: Fetch) -> None:
    bodies = put_all(fetch)
    types = _state(fetch.client).components.get(FILAMENT).types
    types.set_presets(
        fetch.type_id,
        [
            TypePreset(
                type_id=fetch.type_id,
                printer_model="H2C",
                nozzle_diameter=n,
                slicer_filament=str(101 + i),
                slicer_filament_name=name_of(bodies[i]),
            )
            for i, n in enumerate(NOZZLES)
        ],
        actor="ann",
    )
    spools = _state(fetch.client).components.get(FILAMENT).spools
    heir = spools.create(SpoolCreate(type_id=fetch.type_id), actor="ann").id
    before = len(_edits(fetch, heir))
    bb_import(4)
    local_presets([name_of(b) for b in bodies])
    no_k_profiles()

    r = fetch.import_()

    assert r.status_code == 200, r.text
    assert len(fetch.presets()) == 4
    assert _edits(fetch, heir)[before:] == []
