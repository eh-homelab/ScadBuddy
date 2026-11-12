"""#4971 (tier 3b, part 2) and its contract step #5167: the per-printer routes take a
registry id or a Bambuddy id, and keep every per-printer entry under the registry id
only. A Bambuddy id is resolved through the registry."""

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


def test_the_rack_algorithm_is_kept_by_registry_id(client: TestClient) -> None:
    lan, linked = _rows(client)

    assert _algorithm(client, lan, "newest_first") == {
        "printer_id": None,
        "printer": lan,
        "algorithm": "newest_first",
    }
    # A Bambuddy id names the registry printer that links it.
    assert _algorithm(client, "4", "oldest_first") == {
        "printer_id": 4,
        "printer": linked,
        "algorithm": "oldest_first",
    }

    remembered = client.get("/api/v1/settings/remembered").json()
    assert remembered["printer_rack_algorithms"] == {lan: "newest_first", linked: "oldest_first"}
    assert remembered["printer_names"] == {lan: "LAN", linked: "Both"}
    for missing in ("nope", "9"):
        refused = client.put(
            f"/api/v1/print/printers/{missing}/rack-algorithm", json={"algorithm": "newest_first"}
        )
        assert refused.status_code == 404, refused.text
    # A forget of a Bambuddy id no registry printer links has nothing to remove.
    assert _algorithm(client, "9", None) == {
        "printer_id": None,
        "printer": None,
        "algorithm": "least_used",
    }


def test_print_options_for_an_unknown_printer_are_refused_but_may_be_forgotten(
    client: TestClient,
) -> None:
    """A save names a printer the registry has (a registry id, or a Bambuddy id the
    registry links): anything else is 404, so no entry nothing reads is written. A forget
    of one is answered, for an entry written before its printer went."""
    for key in ("nope", "9", "00000000-0000-4000-8000-0000000000ff"):
        refused = client.put(
            "/api/v1/settings/print-options",
            json={"scope": "printer", "key": key, "options": {"timelapse": True}},
        )
        assert refused.status_code == 404, refused.text
        forgot = client.put(
            "/api/v1/settings/print-options",
            json={"scope": "printer", "key": key, "options": {}},
        )
        assert forgot.status_code == 200, forgot.text
    assert client.get("/api/v1/settings/print-options").json()["printers"] == {}


def test_a_printers_print_options_are_kept_by_registry_id(client: TestClient) -> None:
    lan, linked = _rows(client)
    for key in (lan, "4"):
        answer = client.put(
            "/api/v1/settings/print-options",
            json={"scope": "printer", "key": key, "options": {"timelapse": True}},
        )
        assert answer.status_code == 200, answer.text
    assert set(client.get("/api/v1/settings/print-options").json()["printers"]) == {lan, linked}


def test_the_default_printer_is_kept_by_registry_id(client: TestClient, settings: Any) -> None:
    lan, linked = _rows(client)

    body = client.put("/api/v1/settings", json={"default_printer": lan}).json()
    assert (body["printer_id"], body["default_printer"]) == (None, lan)

    body = client.put("/api/v1/settings", json={"default_printer": linked}).json()
    assert (body["printer_id"], body["default_printer"]) == (4, linked)
    assert "printer_id" not in read_stored(settings.database_url)

    # An older client's Bambuddy id is kept as the registry printer that links it.
    client.put("/api/v1/settings", json={"default_printer": lan})
    body = client.put("/api/v1/settings", json={"printer_id": 4}).json()
    assert (body["printer_id"], body["default_printer"]) == (4, linked)
    options = client.get("/api/v1/settings/print-options").json()
    assert (options["printer_id"], options["default_printer"]) == (4, linked)

    for refused in ({"default_printer": "nope"}, {"printer_id": 7}):
        answer = client.put("/api/v1/settings", json=refused)
        assert answer.status_code == 422, answer.text


def test_a_subjects_remembered_printer_is_kept_by_registry_id(client: TestClient) -> None:
    lan, linked = _rows(client)
    kept = client.put("/api/v1/print/models/gear/choices", json={"printer": lan})
    assert (kept.json()["printer_id"], kept.json()["printer"]) == (None, lan)
    both = client.put(
        "/api/v1/print/models/gear/choices", json={"printer_id": 4, "printer": linked}
    )
    assert (both.json()["printer_id"], both.json()["printer"]) == (4, linked)
    # An older client sends the Bambuddy id only.
    old = client.put("/api/v1/print/models/gear/choices", json={"printer_id": 4})
    assert (old.json()["printer_id"], old.json()["printer"]) == (4, linked)


def test_an_entry_keeps_its_key_once_its_printer_is_linked(client: TestClient) -> None:
    """A printer saved under its registry id and later linked to Bambuddy keeps the same
    entry, which a forget by either id removes; an entry of a printer the registry no
    longer holds can still be forgotten by its key."""
    lan, _ = _rows(client)
    _algorithm(client, lan, "newest_first")
    saved = client.put(
        "/api/v1/settings/print-options",
        json={"scope": "printer", "key": lan, "options": {"timelapse": True}},
    )
    assert saved.status_code == 200, saved.text
    registry: Printers = getattr(client.app.state, STATE_ATTR).components.get(PRINTERS)  # type: ignore[attr-defined]
    registry.store.link_bambuddy(lan, 5)

    assert client.get("/api/v1/settings/remembered").json()["printer_rack_algorithms"] == {
        lan: "newest_first"
    }
    _algorithm(client, "5", None)
    forgot = client.put(
        "/api/v1/settings/print-options", json={"scope": "printer", "key": "5", "options": {}}
    )
    assert forgot.status_code == 200, forgot.text
    assert client.get("/api/v1/settings/remembered").json()["printer_rack_algorithms"] == {}
    assert client.get("/api/v1/settings/print-options").json()["printers"] == {}

    # A deleted printer's entry is gone with it, and a forget of its id still answers.
    gone = client.post("/api/v1/printers", json={"serial": "0938AB000000033", "name": "Gone"})
    gone_id = gone.json()["id"]
    _algorithm(client, gone_id, "oldest_first")
    assert client.delete(f"/api/v1/printers/{gone_id}").status_code == 204
    _algorithm(client, gone_id, None)
    assert client.get("/api/v1/settings/remembered").json()["printer_rack_algorithms"] == {}
    refused = client.put(
        "/api/v1/settings/print-options",
        json={"scope": "printer", "key": gone_id, "options": {"timelapse": True}},
    )
    assert refused.status_code == 404, refused.text
