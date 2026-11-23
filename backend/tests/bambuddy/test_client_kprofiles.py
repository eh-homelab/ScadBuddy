"""#7154: a printer's K-profiles through Bambuddy: set, batch set, delete and notes.
The bodies are Bambuddy's ``backend/app/schemas/kprofile.py`` and the routes its
``backend/app/api/routes/kprofiles.py`` (maziggy/bambuddy v1.3.1). Writes are tested with
respx only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import KPROFILE_WRITE_TIMEOUT, BambuddyClient
from scadbuddy.bambuddy.errors import Scope
from scadbuddy.bambuddy.models import KProfile, KProfileNotes, KProfileWriteResult
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL

API = f"{BASE_URL}/api/v1"
SPEC = Path(__file__).parents[1] / "fixtures" / "bambuddy-openapi-1.2.6b1.json"

ROW: dict[str, Any] = {
    "slot_id": 0,
    "extruder_id": 1,
    "nozzle_id": "HH00-0.4",
    "nozzle_diameter": "0.4",
    "filament_id": "GFA00",
    "name": "High Flow_Bambu PLA Basic",
    "k_value": "0.020000",
    "setting_id": None,
}
SENT: dict[str, Any] = {
    "success": True,
    "confirmed": True,
    "message": "K-profile added successfully",
}


def test_a_k_row_keeps_bambuddys_whole_shape() -> None:
    """Bambuddy's ``KProfile`` also has ``n_coef``, ``ams_id`` and ``tray_id``, with
    defaults; SB keeps them so the page can show them."""
    row = KProfile.model_validate({**ROW, "n_coef": "1.400000", "ams_id": 2, "tray_id": 3})
    assert (row.n_coef, row.ams_id, row.tray_id) == ("1.400000", 2, 3)
    bare = KProfile.model_validate(ROW)
    assert (bare.n_coef, bare.ams_id, bare.tray_id) == ("0.000000", 0, -1)


@respx.mock
async def test_set_posts_one_profile(bambuddy: BambuddyClient) -> None:
    route = respx.post(f"{API}/printers/3/kprofiles/").mock(
        return_value=httpx.Response(200, json=SENT)
    )

    result = await bambuddy.set_k_profile(3, ROW)

    assert result == KProfileWriteResult.model_validate(SENT)
    request = route.calls.last.request
    assert json.loads(request.content) == ROW
    assert request.extensions["timeout"]["read"] == KPROFILE_WRITE_TIMEOUT


@respx.mock
async def test_an_unconfirmed_write_says_so(bambuddy: BambuddyClient) -> None:
    respx.post(f"{API}/printers/3/kprofiles/").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "confirmed": False,
                "message": "K-profile sent; the printer did not confirm it",
            },
        )
    )
    result = await bambuddy.set_k_profile(3, ROW)
    assert result.confirmed is False


@respx.mock
async def test_batch_posts_the_list(bambuddy: BambuddyClient) -> None:
    route = respx.post(f"{API}/printers/3/kprofiles/batch").mock(
        return_value=httpx.Response(
            200, json={"success": True, "confirmed": True, "message": "Added 2 K-profiles"}
        )
    )
    rows = [{**ROW, "extruder_id": 0}, ROW]

    result = await bambuddy.set_k_profiles_batch(3, rows)

    assert result.message == "Added 2 K-profiles"
    assert json.loads(route.calls.last.request.content) == rows


@respx.mock
async def test_delete_sends_its_body_with_the_delete(bambuddy: BambuddyClient) -> None:
    route = respx.delete(f"{API}/printers/3/kprofiles/").mock(
        return_value=httpx.Response(
            200, json={"success": True, "confirmed": True, "message": "K-profile deleted"}
        )
    )
    body = {
        "slot_id": 4,
        "extruder_id": 0,
        "nozzle_id": "HS00-0.4",
        "nozzle_diameter": "0.4",
        "filament_id": "GFA00",
        "setting_id": "PFUS1",
    }

    await bambuddy.delete_k_profile(3, body)

    assert json.loads(route.calls.last.request.content) == body


@respx.mock
@pytest.mark.parametrize(
    ("method", "path", "call"),
    [
        ("POST", "/printers/3/kprofiles/", lambda c: c.set_k_profile(3, ROW)),
        ("POST", "/printers/3/kprofiles/batch", lambda c: c.set_k_profiles_batch(3, [ROW])),
        ("DELETE", "/printers/3/kprofiles/", lambda c: c.delete_k_profile(3, ROW)),
        ("PUT", "/printers/3/kprofiles/notes", lambda c: c.set_k_profile_note(3, "PFUS1", "x")),
        (
            "DELETE",
            "/printers/3/kprofiles/notes/PFUS1",
            lambda c: c.delete_k_profile_note(3, "PFUS1"),
        ),
    ],
)
async def test_every_write_names_the_admin_only_scope(
    bambuddy: BambuddyClient, method: str, path: str, call: Any
) -> None:
    """Bambuddy denies ``KPROFILES_CREATE``, ``_UPDATE`` and ``_DELETE`` to every API key
    (``_APIKEY_DENIED_PERMISSIONS``, ``core/auth.py`` lines 305-307, v1.3.1): its 403 is
    the scope problem that says no key can, R-7154-3."""
    respx.route(method=method, url=f"{API}{path}").mock(
        return_value=httpx.Response(403, json={"detail": "Not enough permissions"})
    )

    with pytest.raises(ApiError) as caught:
        await call(bambuddy)

    assert caught.value.status == 409
    assert caught.value.extensions["required_scope"] == Scope.ADMIN_KPROFILES.value
    assert "auth is on" in caught.value.detail
    assert "K-profiles" in caught.value.detail


@respx.mock
async def test_notes_are_read_as_a_map(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/printers/3/kprofiles/notes").mock(
        return_value=httpx.Response(200, json={"notes": {"PFUS1": "good on the H2C"}})
    )
    assert await bambuddy.k_profile_notes(3) == KProfileNotes(notes={"PFUS1": "good on the H2C"})


@respx.mock
async def test_a_note_is_put_with_its_key(bambuddy: BambuddyClient) -> None:
    route = respx.put(f"{API}/printers/3/kprofiles/notes").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "Note saved"})
    )
    result = await bambuddy.set_k_profile_note(3, "name_PLA_GFA00", "dry it first")
    assert result.message == "Note saved"
    assert json.loads(route.calls.last.request.content) == {
        "setting_id": "name_PLA_GFA00",
        "note": "dry it first",
    }


@respx.mock
async def test_a_note_key_is_quoted_in_the_path(bambuddy: BambuddyClient) -> None:
    route = respx.delete(f"{API}/printers/3/kprofiles/notes/name_PLA%20Basic_GFA00").mock(
        return_value=httpx.Response(200, json={"success": True, "message": "Note deleted"})
    )
    await bambuddy.delete_k_profile_note(3, "name_PLA Basic_GFA00")
    assert route.called


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/v1/printers/{printer_id}/kprofiles/", "post"),
        ("/api/v1/printers/{printer_id}/kprofiles/", "delete"),
        ("/api/v1/printers/{printer_id}/kprofiles/batch", "post"),
        ("/api/v1/printers/{printer_id}/kprofiles/notes", "get"),
        ("/api/v1/printers/{printer_id}/kprofiles/notes", "put"),
        ("/api/v1/printers/{printer_id}/kprofiles/notes/{setting_id}", "delete"),
    ],
)
def test_the_routes_are_in_bambuddys_openapi(path: str, method: str) -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    assert method in spec["paths"][path]
