"""#4971 (tier 3b, part 2): the per-printer routes take a registry id, so a printer with
no Bambuddy id (LAN-only) is remembered by it; a printer that has one is still stored
under its Bambuddy id, which the previous build reads."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.printers.component import PRINTERS, Printers
from tests.api.conftest import read_stored


def _rows(client: TestClient) -> tuple[str, str]:
    """A LAN-only registry printer and one linked to Bambuddy printer 4."""
    lan = client.post("/api/v1/printers", json={"serial": "0938AB000000031", "name": "LAN"})
    assert lan.status_code == 201, lan.text
    linked = client.post("/api/v1/printers", json={"serial": "0938AB000000032", "name": "Both"})
    registry: Printers = getattr(client.app.state, STATE_ATTR).components.get(PRINTERS)  # type: ignore[attr-defined]
    registry.store.link_bambuddy(linked.json()["id"], 4)
    return lan.json()["id"], linked.json()["id"]


def _algorithm(client: TestClient, printer: str, algorithm: str | None) -> Any:
    answer = client.put(
        f"/api/v1/print/printers/{printer}/rack-algorithm", json={"algorithm": algorithm}
    )
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_the_rack_algorithm_is_kept_by_registry_id_only_without_a_bambuddy_one(
    client: TestClient,
) -> None:
    lan, linked = _rows(client)

    assert _algorithm(client, lan, "newest_first") == {
        "printer_id": None,
        "printer": lan,
        "algorithm": "newest_first",
    }
    assert _algorithm(client, linked, "oldest_first") == {
        "printer_id": 4,
        "printer": None,
        "algorithm": "oldest_first",
    }

    remembered = client.get("/api/v1/settings/remembered").json()
    assert remembered["printer_rack_algorithms"] == {lan: "newest_first", "4": "oldest_first"}
    assert remembered["printer_names"] == {lan: "LAN"}
    missing = client.put(
        "/api/v1/print/printers/nope/rack-algorithm", json={"algorithm": "newest_first"}
    )
    assert missing.status_code == 404, missing.text


def test_a_printers_print_options_are_kept_by_registry_id_only_without_a_bambuddy_one(
    client: TestClient,
) -> None:
    lan, linked = _rows(client)
    for key in (lan, linked):
        answer = client.put(
            "/api/v1/settings/print-options",
            json={"scope": "printer", "key": key, "options": {"timelapse": True}},
        )
        assert answer.status_code == 200, answer.text
    assert set(client.get("/api/v1/settings/print-options").json()["printers"]) == {lan, "4"}


def test_the_default_printer_is_one_key_or_the_other(client: TestClient, settings: Any) -> None:
    lan, linked = _rows(client)

    body = client.put("/api/v1/settings", json={"default_printer": lan}).json()
    assert (body["printer_id"], body["default_printer"]) == (None, lan)

    # A printer with a Bambuddy id is the previous build's `printer_id`.
    body = client.put("/api/v1/settings", json={"default_printer": linked}).json()
    assert (body["printer_id"], body.get("default_printer")) == (4, None)
    assert "default_printer" not in read_stored(settings.database_url)

    client.put("/api/v1/settings", json={"default_printer": lan})
    body = client.put("/api/v1/settings", json={"printer_id": 7}).json()
    assert (body["printer_id"], body.get("default_printer")) == (7, None)

    refused = client.put("/api/v1/settings", json={"default_printer": "nope"})
    assert refused.status_code == 422, refused.text


def test_a_subjects_remembered_printer_keeps_a_registry_id_only_without_a_bambuddy_one(
    client: TestClient,
) -> None:
    lan, _ = _rows(client)
    kept = client.put("/api/v1/print/models/gear/choices", json={"printer": lan})
    assert kept.json()["printer"] == lan
    both = client.put("/api/v1/print/models/gear/choices", json={"printer_id": 4, "printer": lan})
    assert (both.json()["printer_id"], both.json()["printer"]) == (4, None)


def test_a_lan_only_entry_is_dropped_once_its_printer_is_linked(client: TestClient) -> None:
    """#5170 review: a printer saved under its registry id and later linked to Bambuddy
    keeps no registry-id entry past its next write or forget, and an entry of a printer
    the registry no longer holds can still be forgotten by its key."""
    lan, _ = _rows(client)
    _algorithm(client, lan, "newest_first")
    saved = client.put(
        "/api/v1/settings/print-options",
        json={"scope": "printer", "key": lan, "options": {"timelapse": True}},
    )
    assert saved.status_code == 200, saved.text
    registry: Printers = getattr(client.app.state, STATE_ATTR).components.get(PRINTERS)  # type: ignore[attr-defined]
    registry.store.link_bambuddy(lan, 5)

    _algorithm(client, lan, None)
    forgot = client.put(
        "/api/v1/settings/print-options", json={"scope": "printer", "key": lan, "options": {}}
    )
    assert forgot.status_code == 200, forgot.text
    assert client.get("/api/v1/settings/remembered").json()["printer_rack_algorithms"] == {}
    assert client.get("/api/v1/settings/print-options").json()["printers"] == {}

    # A deleted printer's entry: no registry row, and the forget still removes it.
    gone = client.post("/api/v1/printers", json={"serial": "0938AB000000033", "name": "Gone"})
    gone_id = gone.json()["id"]
    _algorithm(client, gone_id, "oldest_first")
    assert client.delete(f"/api/v1/printers/{gone_id}").status_code == 204
    _algorithm(client, gone_id, None)
    assert client.get("/api/v1/settings/remembered").json()["printer_rack_algorithms"] == {}
    # A save naming it is refused, as the rack algorithm's is.
    refused = client.put(
        "/api/v1/settings/print-options",
        json={"scope": "printer", "key": gone_id, "options": {"timelapse": True}},
    )
    assert refused.status_code == 404, refused.text
