"""#7025: the routes that search 3DFP and add a filament to it, over a fake runner
(`api.filament.tdfp` patched): the answer, each failure's status and code, the request key
the create needs, the type's ``tdfp_id`` set from the new id, and nothing stored of the
identifiers."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api import filament as filament_api
from scadbuddy.profile_fetch.wire import (
    CreateAnswer,
    CreateAsk,
    LikelyDuplicate,
    SearchAsk,
    TdfpOutcome,
)
from tests.api.test_filament_profile_fetch import _type
from tests.profile_fetch.test_activities import SEARCH_ANSWER

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament"
KEY = {"Idempotency-Key": "create-7025-1"}
BODY: dict[str, Any] = {
    "brand": "SUNLU",
    "material": "PLA+/Pro",
    "material_type": "Silk",
    "colour": "Copper",
    "hexes": ["#b87333"],
    "gtin": "06975373321234",
    "mpn": "SL-SILK-CU",
}


class FakeTdfp:
    """`filament.tdfp.Tdfp`: each call recorded, each answer set by the test."""

    def __init__(self) -> None:
        self.worker = True
        self.searches: list[SearchAsk] = []
        self.creates: list[tuple[CreateAsk, str]] = []
        self.search_outcome = TdfpOutcome(search=SEARCH_ANSWER)
        self.create_outcome = TdfpOutcome(create=CreateAnswer(status="created", tdfp_id=40000))
        self.ran: set[str] = set()
        self.slow = False

    async def has_worker(self) -> bool:
        return self.worker

    async def search(self, ask: SearchAsk, *, wait: float) -> TdfpOutcome:
        self.searches.append(ask)
        if self.slow:
            raise TimeoutError
        return self.search_outcome

    async def started(self, ask: CreateAsk, *, key: str) -> bool:
        return key in self.ran

    async def create(self, ask: CreateAsk, *, key: str, wait: float) -> TdfpOutcome:
        self.creates.append((ask, key))
        self.ran.add(key)
        if self.slow:
            raise TimeoutError
        return self.create_outcome


@pytest.fixture(autouse=True)
def runner(monkeypatch: pytest.MonkeyPatch) -> FakeTdfp:
    fake = FakeTdfp()
    monkeypatch.setattr(filament_api, "tdfp", lambda state: fake)
    return fake


def test_a_search_answers_the_candidates(client: TestClient, runner: FakeTdfp) -> None:
    r = client.post(f"{URL}/tdfp/search", json={"brand": "SUNLU", "colour": "Bronze"})
    assert r.status_code == 200, r.text
    assert r.json() == SEARCH_ANSWER.model_dump(mode="json")
    assert runner.searches == [SearchAsk(brand="SUNLU", colour="Bronze")]


def test_a_search_with_no_worker_is_503(client: TestClient, runner: FakeTdfp) -> None:
    runner.worker = False
    r = client.post(f"{URL}/tdfp/search", json={"brand": "SUNLU"})
    assert (r.status_code, r.json()["code"]) == (503, "no_worker")
    assert runner.searches == []


@pytest.mark.parametrize(
    ("kind", "code"),
    [("not_started", 503), ("bot_check", 502), ("timeout", 502), ("login_failed", 502)],
)
def test_a_failed_search_answers_its_kind(
    client: TestClient, runner: FakeTdfp, kind: str, code: int
) -> None:
    runner.search_outcome = TdfpOutcome(kind=kind, reason="why it failed")
    r = client.post(f"{URL}/tdfp/search", json={"brand": "SUNLU"})
    assert r.status_code == code
    assert r.json()["code"] == kind
    assert r.json()["detail"] == "why it failed"


def test_a_search_with_no_outcome_in_time_is_504(client: TestClient, runner: FakeTdfp) -> None:
    runner.slow = True
    r = client.post(f"{URL}/tdfp/search", json={"brand": "SUNLU"})
    assert (r.status_code, r.json()["code"]) == (504, "no_outcome")


def test_a_create_needs_a_request_key(client: TestClient, runner: FakeTdfp) -> None:
    r = client.post(f"{URL}/tdfp/filaments", json=BODY)
    assert r.status_code == 428
    assert runner.creates == []


def test_a_create_sets_the_types_tdfp_id(client: TestClient, runner: FakeTdfp) -> None:
    type_id = _type(client, code="PLA-CU", tdfp_id=None)
    r = client.post(f"{URL}/tdfp/filaments", json={**BODY, "type_id": type_id}, headers=KEY)
    assert r.status_code == 200, r.text
    assert r.json()["create"]["tdfp_id"] == 40000
    assert r.json()["type_tdfp_id_set"] is True
    [(ask, key)] = runner.creates
    assert ask.hexes == ["#B87333"]
    assert ask.identifiers() == {"mpn": "SL-SILK-CU", "gtin": "06975373321234"}
    # The caller's key, under its own name.
    assert key.endswith("\x1fcreate-7025-1")
    stored = client.get(f"{URL}/types/{type_id}").json()
    assert stored["tdfp_id"] == 40000
    # R-7025-6: the identifiers are stored nowhere on the type.
    assert "06975373321234" not in str(stored)
    assert "SL-SILK-CU" not in str(stored)


def test_a_create_sent_again_answers_again_and_keeps_the_type(
    client: TestClient, runner: FakeTdfp
) -> None:
    """The type has the id from the first answer: the resend under its key is no conflict."""
    type_id = _type(client, code="PLA-CU", tdfp_id=None)
    body = {**BODY, "type_id": type_id}
    first = client.post(f"{URL}/tdfp/filaments", json=body, headers=KEY)
    again = client.post(f"{URL}/tdfp/filaments", json=body, headers=KEY)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()


def test_a_type_with_a_tdfp_id_is_refused(client: TestClient, runner: FakeTdfp) -> None:
    type_id = _type(client, code="PLA-WHT", tdfp_id=30173)
    r = client.post(f"{URL}/tdfp/filaments", json={**BODY, "type_id": type_id}, headers=KEY)
    assert (r.status_code, r.json()["code"]) == (409, "type_has_tdfp_id")
    assert runner.creates == []


def test_a_type_that_took_another_id_meanwhile_keeps_it(
    client: TestClient, runner: FakeTdfp
) -> None:
    type_id = _type(client, code="PLA-CU", tdfp_id=None)
    runner.create_outcome = TdfpOutcome(create=CreateAnswer(status="created", tdfp_id=40001))
    other = client.patch(f"{URL}/types/{type_id}", json={"tdfp_id": 12345})
    assert other.status_code == 200, other.text
    # The type got its id after this request's create started: a resend finds it started.
    runner.started = _always_started  # type: ignore[method-assign]
    r = client.post(f"{URL}/tdfp/filaments", json={**BODY, "type_id": type_id}, headers=KEY)
    assert r.status_code == 200, r.text
    assert r.json()["type_tdfp_id_set"] is False
    assert client.get(f"{URL}/types/{type_id}").json()["tdfp_id"] == 12345


async def _always_started(ask: CreateAsk, *, key: str) -> bool:
    return True


def test_no_such_type_is_404(client: TestClient, runner: FakeTdfp) -> None:
    r = client.post(
        f"{URL}/tdfp/filaments",
        json={**BODY, "type_id": "0f0e0d0c-0000-4000-8000-000000007025"},
        headers=KEY,
    )
    assert r.status_code == 404
    assert runner.creates == []


def test_a_likely_duplicate_answers_the_list_and_sets_nothing(
    client: TestClient, runner: FakeTdfp
) -> None:
    type_id = _type(client, code="PLA-CU", tdfp_id=None)
    runner.create_outcome = TdfpOutcome(
        create=CreateAnswer(
            status="duplicate",
            duplicate_text="Avoid creating duplicates SUNLU Copper",
            likely=[LikelyDuplicate(tdfp_id=1708, reason="a name close to 'Copper'")],
        )
    )
    r = client.post(f"{URL}/tdfp/filaments", json={**BODY, "type_id": type_id}, headers=KEY)
    assert r.status_code == 200, r.text
    assert r.json()["create"]["status"] == "duplicate"
    assert r.json()["type_tdfp_id_set"] is False
    assert client.get(f"{URL}/types/{type_id}").json()["tdfp_id"] is None


@pytest.mark.parametrize(
    ("kind", "code"),
    [("no_defaults", 422), ("option_missing", 422), ("not_created", 502), ("not_started", 503)],
)
def test_a_failed_create_answers_its_kind(
    client: TestClient, runner: FakeTdfp, kind: str, code: int
) -> None:
    runner.create_outcome = TdfpOutcome(kind=kind, reason="why it failed")
    r = client.post(f"{URL}/tdfp/filaments", json=BODY, headers=KEY)
    assert (r.status_code, r.json()["code"]) == (code, kind)


def test_a_create_with_no_outcome_in_time_says_to_send_it_again(
    client: TestClient, runner: FakeTdfp
) -> None:
    runner.slow = True
    r = client.post(f"{URL}/tdfp/filaments", json=BODY, headers=KEY)
    assert (r.status_code, r.json()["code"]) == (504, "no_outcome")
    assert "same Idempotency-Key" in r.json()["detail"]
