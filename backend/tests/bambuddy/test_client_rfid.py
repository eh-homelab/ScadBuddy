"""#6264 Task 3: the client calls for tags, auto-add, slots and SpoolBuddy. Bodies and
routes come from ``recordings/openapi/rfid-routes.json`` (Bambuddy 1.3.1)."""

from __future__ import annotations

import inspect
import json
import re
import typing

import httpx
import pytest
import respx
from pydantic import ValidationError

from scadbuddy.bambuddy import client as client_module
from scadbuddy.bambuddy.client import BambuddyClient, Operation
from scadbuddy.bambuddy.errors import (
    SCOPE_PROBLEM,
    Scope,
    TagAlreadyLinkedError,
)
from scadbuddy.bambuddy.models import SlotConfigure
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL, RECORDINGS, shaped

API = f"{BASE_URL}/api/v1"
RFID = json.loads((RECORDINGS / "openapi" / "rfid-routes.json").read_text(encoding="utf-8"))


def _spool(**values: object) -> dict[str, object]:
    return shaped("SpoolResponse", id=3, material="PLA", **values)


def _slot(**values: object) -> SlotConfigure:
    base: dict[str, object] = {
        "tray_info_idx": "GFA00",
        "tray_type": "PLA",
        "tray_sub_brands": "Basic",
        "tray_color": "FF0000FF",
        "nozzle_temp_min": 190,
        "nozzle_temp_max": 230,
        "nozzle_diameter": "0.4",
    }
    return SlotConfigure.model_validate({**base, **values})


@respx.mock
async def test_link_tag_sends_the_recorded_body(bambuddy: BambuddyClient) -> None:
    route = respx.patch(f"{API}/inventory/spools/3/link-tag").mock(
        return_value=httpx.Response(200, json=_spool(tag_uid="04AABB", data_origin="nfc_link"))
    )

    spool = await bambuddy.link_tag(3, tag_uid="04AABB", tray_uuid=None)

    assert json.loads(route.calls.last.request.content) == {
        "tag_uid": "04AABB",
        "tray_uuid": None,
        "tag_type": None,
        "data_origin": "nfc_link",
    }
    assert route.calls.last.request.headers["X-API-Key"] == "s3cret"
    assert spool.tag_uid == "04AABB"
    assert spool.data_origin == "nfc_link"


@respx.mock
async def test_a_409_names_the_holder(bambuddy: BambuddyClient) -> None:
    detail = {**RFID["conflict_409"], "spool_id": 7, "field": "tray_uuid"}
    respx.patch(f"{API}/inventory/spools/3/link-tag").mock(
        return_value=httpx.Response(409, json={"detail": detail})
    )

    with pytest.raises(TagAlreadyLinkedError) as caught:
        await bambuddy.link_tag(3, tag_uid=None, tray_uuid="UUID")

    assert caught.value.holder_id == 7
    assert caught.value.field == "tray_uuid"
    assert caught.value.status == 409


@respx.mock
async def test_another_409_is_the_generic_mapped_error(bambuddy: BambuddyClient) -> None:
    respx.patch(f"{API}/inventory/spools/3/link-tag").mock(
        return_value=httpx.Response(409, json={"detail": "busy"})
    )

    with pytest.raises(ApiError) as caught:
        await bambuddy.link_tag(3, tag_uid="04AABB", tray_uuid=None)

    assert not isinstance(caught.value, TagAlreadyLinkedError)
    assert caught.value.status == 409


@respx.mock
async def test_clear_spool_tag_sends_two_nulls(bambuddy: BambuddyClient) -> None:
    route = respx.patch(f"{API}/inventory/spools/3").mock(
        return_value=httpx.Response(200, json=_spool())
    )

    await bambuddy.clear_spool_tag(3)

    assert json.loads(route.calls.last.request.content) == {"tag_uid": None, "tray_uuid": None}


@respx.mock
async def test_by_tag_404_is_none_and_200_is_a_spool(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/inventory/spools/by-tag").mock(
        side_effect=[
            httpx.Response(404, json={"detail": "Spool not found"}),
            httpx.Response(200, json=_spool(tray_uuid="UUID")),
        ]
    )

    assert await bambuddy.spool_by_tag(tray_uuid="UUID", tag_uid=None) is None
    found = await bambuddy.spool_by_tag(tray_uuid="UUID", tag_uid=None)

    assert found is not None
    assert found.id == 3
    assert dict(route.calls.last.request.url.params) == {"tray_uuid": "UUID"}


@respx.mock
async def test_spool_row_reads_one_spool(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/inventory/spools/3").mock(
        return_value=httpx.Response(200, json=_spool(tag_type="ntag"))
    )

    assert (await bambuddy.spool_row(3)).tag_type == "ntag"


@respx.mock
async def test_auto_add_reads_the_boolean_and_refuses_a_missing_key(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/settings/").mock(
        side_effect=[
            httpx.Response(200, json=shaped("AppSettings", auto_add_unknown_rfid=True)),
            httpx.Response(200, json={"capture_finish_photo": True}),
        ]
    )

    assert await bambuddy.auto_add_unknown_rfid() is True
    with pytest.raises(ApiError) as caught:
        await bambuddy.auto_add_unknown_rfid()
    assert caught.value.status == 502


@respx.mock
async def test_setting_auto_add_sends_the_value(bambuddy: BambuddyClient) -> None:
    route = respx.patch(f"{API}/settings/").mock(return_value=httpx.Response(200, json={}))

    await bambuddy.set_auto_add_unknown_rfid(False)

    assert json.loads(route.calls.last.request.content) == {"auto_add_unknown_rfid": False}


@respx.mock
async def test_setting_auto_add_with_auth_on_is_a_scope_409(bambuddy: BambuddyClient) -> None:
    respx.patch(f"{API}/settings/").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )

    with pytest.raises(ApiError) as caught:
        await bambuddy.set_auto_add_unknown_rfid(True)

    assert caught.value.status == 409
    assert caught.value.type == SCOPE_PROBLEM
    assert caught.value.extensions["required_scope"] == Scope.ADMIN_SETTINGS.value
    assert caught.value.extensions["required_scope"] == "Settings (admin only)"


@respx.mock
async def test_slot_configure_puts_the_values_in_the_query(bambuddy: BambuddyClient) -> None:
    route = respx.post(f"{API}/printers/1/slots/0/2/configure").mock(
        return_value=httpx.Response(200, json={})
    )

    await bambuddy.configure_slot(1, 0, 2, _slot(k_value=0.02))

    assert dict(route.calls.last.request.url.params) == {
        "tray_info_idx": "GFA00",
        "tray_type": "PLA",
        "tray_sub_brands": "Basic",
        "tray_color": "FF0000FF",
        "nozzle_temp_min": "190",
        "nozzle_temp_max": "230",
        "nozzle_diameter": "0.4",
        "k_value": "0.02",
    }


def test_the_route_s_required_query_parameters_are_required_fields() -> None:
    required = {
        p["name"]
        for r in RFID["routes"]
        if r["path"].endswith("/configure")
        for p in r["parameters"]
        if p["in"] == "query" and p["required"]
    }
    base = _slot().model_dump()
    for name in required & set(SlotConfigure.model_fields):
        with pytest.raises(ValidationError):
            SlotConfigure.model_validate({k: v for k, v in base.items() if k != name})
    assert {"tray_sub_brands", "nozzle_temp_min", "nozzle_temp_max"} <= required


def test_a_digest_changes_with_any_field() -> None:
    base = _slot()
    assert base.digest() == _slot().digest()
    for field, value in {
        "tray_info_idx": "GFB00",
        "tray_type": "PETG",
        "tray_sub_brands": "Plus",
        "tray_color": "00FF00FF",
        "nozzle_temp_min": 200,
        "nozzle_temp_max": 250,
        "cali_idx": 4,
        "nozzle_diameter": "0.6",
        "setting_id": "S1",
        "k_value": 0.5,
    }.items():
        assert _slot(**{field: value}).digest() != base.digest(), field


@pytest.mark.parametrize(
    "name",
    ["next_tag_scan", "queue_tag_write", "cancel_tag_write", "spoolbuddy_devices", "ws_token"],
)
def test_the_client_makes_no_spoolbuddy_call(name: str) -> None:
    assert not hasattr(BambuddyClient, name)


def test_every_operation_the_client_passes_is_a_declared_name() -> None:
    passed = set(re.findall(r'operation="([a-z_.]+)"', inspect.getsource(client_module)))
    declared = set(typing.get_args(Operation))
    new = {
        "spools.link_tag",
        "spools.clear_tag",
        "spools.by_tag",
        "spools.read",
        "settings.update",
        "printers.slot_configure",
    }
    assert new <= passed
    assert passed <= declared
