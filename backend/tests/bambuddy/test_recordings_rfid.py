"""The RFID recording (#6264) holds the Bambuddy facts the tag work rests on."""

from __future__ import annotations

import json
from typing import Any

from tests.bambuddy.conftest import RECORDINGS


def _rfid() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (RECORDINGS / "openapi" / "rfid-routes.json").read_text(encoding="utf-8")
    )
    return data


def test_the_rfid_recording_holds_what_the_plan_rests_on() -> None:
    rec = _rfid()
    schemas = rec["components"]["schemas"]
    assert {"tag_uid", "tray_uuid", "tag_type", "data_origin"} <= set(
        schemas["LinkTagRequest"]["properties"]
    )
    conflict = rec["conflict_409"]
    assert conflict["code"] == "tag_already_linked"
    assert set(conflict) == {"code", "message", "spool_id", "field"}
    assert {"device_id", "spool_id"} <= set(schemas["WriteTagRequest"]["required"])
    assert "auto_add_unknown_rfid" in schemas["AppSettingsUpdate"]["properties"]
    assert {"spoolbuddy_tag_matched", "spoolbuddy_unknown_tag"} <= set(rec["ws_events"])
    assert {"tag_type", "data_origin"} <= set(schemas["SpoolResponse"]["properties"])
    origins = rec["data_origin_values"]
    for value in ("rfid_auto", "rfid_linked"):
        assert origins[value]["source"].rsplit("#L", 1)[1][0].isdigit()
