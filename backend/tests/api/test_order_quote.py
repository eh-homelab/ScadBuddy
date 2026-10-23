"""The slice quote end to end (#4250, spec 2026-10-18 §5.2; plan Rulings 2, 3, 7, 12):
``POST /orders/{id}/items/{item}/quote`` answers 202 with the ``order_item_quote``
operation, whose run lands on the in-process ``library`` worker and slices every plate
through the print dialog's own background slice: Bambuddy's over the ``respx``
recordings ``tests/api/test_print.py`` uses, with ``tests/support/sliced3mf.py``'s files
as the sliced downloads, and the native slicer's with ``tests/support/fake_slicer.py``
in the Rust worker's place."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.orders.lifecycle import QUOTE_HOLDER
from tests.api.conftest import follow_operation, set_fake_env
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item
from tests.api.test_print_preview_native import (
    _open,
    _settings,
    copy_routes,
    dialog_profiles,
    kek_file,  # noqa: F401
)
from tests.api.test_print_run_choices import body, run_routes
from tests.api.test_send import API, configure, make_output, upload_route
from tests.api.test_settings_runtime import _state
from tests.api.test_versions import SLUG, upload
from tests.conftest import write_openscad_3mf
from tests.support.fake_slicer import FakeSlicer, fake_slicer_worker
from tests.support.operations import press
from tests.support.sliced3mf import sliced_3mf
from tests.test_bambu3mf import add_plate

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_git]

#: The copy the dialog's path uploads (``upload_route``).
COPY = 41
CREATED = "2026-10-22T10:00:00.000000"
BOOK = "/api/v1/orders/price-book"


def sliced_plate(plate: int, seconds: int, grams: list[float]) -> bytes:
    return sliced_3mf(plates={plate: (seconds, grams)}, gcode={plate: b"M620 S0A\nM620 S1A\n"})


def two_colours(fake_openscad: str) -> None:
    """The fake openscad renders a red and a green box: two colour slots."""
    directory = Path(fake_openscad).parent
    drawn = write_openscad_3mf(
        directory / "two.3mf",
        [
            ("Color 1", "#FF000000", trimesh.creation.box(extents=(10, 10, 5))),
            (
                "Color 2",
                "#00FF0000",
                trimesh.creation.box(extents=(4, 4, 8)).apply_translation((20, 0, 0)),
            ),
        ],
    )
    set_fake_env(directory, "FAKE_3MF", str(drawn))


def price_book(client: TestClient, **policy: str) -> tuple[str, str]:
    """A machine rate and two filaments (PLA at 20 a kilogram, PETG at 30)."""
    saved = client.put(
        BOOK,
        json={
            "machine_rate_per_hour": "1.5",
            "setup_fee": "0.5",
            "labour_per_item": "0",
            "markup_percent": "0",
            "rounding": "to_minor_unit",
            **policy,
        },
    )
    assert saved.status_code == 200, saved.text
    ids = []
    for material, colour, cost in (("PLA", "#FF0000", "20"), ("PETG", "#00FF00", "30")):
        made = client.post(
            f"{BOOK}/filaments", json={"material": material, "colour": colour, "cost_per_kg": cost}
        )
        assert made.status_code == 201, made.text
        ids.append(made.json()["id"])
    return ids[0], ids[1]


class Shop:
    """An order of one item from a two-colour output, and the book to price it."""

    def __init__(self, client: TestClient, *, plates: int = 1, by_hand: bool = False) -> None:
        self.client = client
        upload(client)
        self.pla, self.petg = price_book(client)
        customer = create_customer(client)
        self.order = create_order(client, customer["id"])
        if by_hand:
            commit = client.get(f"/api/v1/models/{SLUG}").json()["version"]
            self.item = add_item(
                client,
                self.order["id"],
                slug=SLUG,
                commit=commit,
                inputs={"params": {"width": 12}},
                quantity=2,
            )
        else:
            output_id = make_output(client, SLUG)
            if plates == 2:
                paths: DataPaths = _state(client).paths
                [model_3mf] = paths.outputs.glob(f"*/{output_id}/model.3mf")
                add_plate(model_3mf, 2)
            self.item = add_item(client, self.order["id"], output_id=output_id, quantity=2)
        self.path = f"/api/v1/orders/{self.order['id']}/items/{self.item['id']}"

    def request(self, **extra: Any) -> dict[str, Any]:
        dialog = {name: value for name, value in body().items() if name != "copies"}
        return {**dialog, "filaments": {"0": self.pla, "1": self.petg}, **extra}

    def post(self, key: str | None = None, **extra: Any) -> httpx.Response:
        headers = {"Idempotency-Key": key} if key else press()
        response: httpx.Response = self.client.post(
            f"{self.path}/quote", json=self.request(**extra), headers=headers
        )
        return response

    def quote(self, key: str | None = None) -> dict[str, Any]:
        """Posted and followed: the operation as it ended."""
        started = self.post(key)
        assert started.status_code == 202, started.text
        return follow_operation(self.client, started.json(), timeout=120, poll=0.2)

    def get_item(self) -> dict[str, Any]:
        detail = self.client.get(f"/api/v1/orders/{self.order['id']}").json()
        item: dict[str, Any] = next(i for i in detail["items"] if i["id"] == self.item["id"])
        return item

    def holders(self) -> dict[str, str]:
        refs = _state(self.client).refs
        with refs.pool.connection() as conn:
            rows = conn.execute(
                "SELECT holder_id, key FROM blob_refs WHERE holder_kind = %s", (QUOTE_HOLDER,)
            ).fetchall()
        return {row["holder_id"]: row["key"] for row in rows}


class BambuddySlices:
    """Bambuddy's slicer as the dialog's background slice reads it: a job per plate
    posted, completed (unless ``running``) with a sliced library file whose download is
    ``files[plate]``."""

    def __init__(self, files: Mapping[int, bytes]) -> None:
        self.files = dict(files)
        self.running = False
        self.failure: str | None = None
        self._jobs = itertools.count(9)
        self.jobs: dict[int, int] = {}
        self.posted = respx.route(
            method="POST", path__regex=r"/api/v1/library/files/\d+/slice"
        ).mock(side_effect=self._slice)
        respx.route(method="GET", path__regex=r"/api/v1/slice-jobs/\d+$").mock(
            side_effect=self._job
        )
        for plate, payload in self.files.items():
            sliced = 300 + plate
            name = f"plate-{plate}.gcode.3mf"
            respx.get(f"{API}/library/files/{sliced}").mock(
                return_value=httpx.Response(200, json={"id": sliced, "filename": name})
            )
            respx.get(f"{API}/library/files/{sliced}/download").mock(
                return_value=httpx.Response(200, content=payload)
            )

    def _slice(self, request: httpx.Request) -> httpx.Response:
        plate = json.loads(request.content)["plate"] or 1
        job_id = next(self._jobs)
        self.jobs[job_id] = plate
        return httpx.Response(200, json={"job_id": job_id, "status": "pending"})

    def _job(self, request: httpx.Request) -> httpx.Response:
        job_id = int(request.url.path.rsplit("/", 1)[-1])
        plate = self.jobs[job_id]
        answer: dict[str, Any] = {
            "job_id": job_id,
            "kind": "library_file",
            "source_id": COPY,
            "created_at": CREATED,
            "status": "running" if self.running else "completed",
        }
        if self.failure is not None:
            answer.update(status="failed", error_status=422, error_detail=self.failure)
        elif not self.running:
            answer["result"] = {"library_file_id": 300 + plate, "name": f"plate-{plate}.gcode.3mf"}
        return httpx.Response(200, json=answer)


@pytest.fixture
def bambuddy(client: TestClient, fake_openscad: str) -> Iterator[TestClient]:
    with respx.mock as router:
        router.route(host="testserver").pass_through()
        two_colours(fake_openscad)
        configure(client)
        upload_route(COPY)
        run_routes()
        yield client


def output_ids(client: TestClient) -> list[str]:
    return [output["id"] for output in client.get(f"/api/v1/models/{SLUG}/outputs").json()]


def lines_of(quote: dict[str, Any]) -> list[tuple[str, int]]:
    return [(line["kind"], line["amount_minor"]) for line in quote["lines"]]


# -- Bambuddy's slicer -------------------------------------------------------------------------


def test_a_quote_from_a_slice_on_bambuddy(bambuddy: TestClient) -> None:
    shop = Shop(bambuddy)
    # One payload: a zip's entries carry the time it was written.
    payload = sliced_plate(1, 3600, [12.5, 3.0])
    slices = BambuddySlices({1: payload})
    op = shop.quote()
    assert op["status"] == "succeeded", op
    quote = op["result"]
    assert quote["source"] == "slice"
    assert lines_of(quote) == [
        ("filament", 25),  # 12.5 g x 20 / 1000 = 0.25
        ("filament", 9),  # 3.0 g x 30 / 1000 = 0.09
        ("machine_time", 150),  # 1 h x 1.5
        ("setup", 50),
    ]
    assert (quote["unit_price_minor"], quote["quantity"], quote["total_minor"]) == (234, 2, 468)
    assert quote["sliced_sha256"] == hashlib.sha256(payload).hexdigest()
    assert quote["plates"] == [
        {
            "index": 1,
            "print_seconds": 3600,
            "filaments": [{"slot": 0, "used_g": "12.5"}, {"slot": 1, "used_g": "3.0"}],
            "blob_key": quote["sliced_blob_key"],
            "sha256": quote["sliced_sha256"],
        }
    ]
    assert shop.holders() == {quote["id"]: quote["sliced_blob_key"]}
    assert slices.posted.call_count == 1
    item = shop.get_item()
    assert item["status"] == "quoted"
    assert [slot["filament_id"] for slot in item["colours"]] == [shop.pla, shop.petg]
    assert [q["id"] for q in item["quotes"]] == [quote["id"]]

    # Review Focus 2: the book moving afterwards never moves the recorded quote.
    assert bambuddy.put(BOOK, json={"machine_rate_per_hour": "99"}).status_code == 200
    assert (
        bambuddy.patch(f"{BOOK}/filaments/{shop.pla}", json={"cost_per_kg": "99"}).status_code
        == 200
    )
    again = shop.get_item()["quotes"][0]
    assert again["lines"] == quote["lines"] and again["unit_price_minor"] == 234

    # A second quote supersedes the first's hold; the first row stays.
    assert bambuddy.put(BOOK, json={"machine_rate_per_hour": "1.5"}).status_code == 200
    second = shop.quote()
    assert second["status"] == "succeeded", second
    assert set(shop.holders()) == {second["result"]["id"]}
    assert len(shop.get_item()["quotes"]) == 2


def test_a_two_plate_item_is_quoted_on_both_plates(bambuddy: TestClient) -> None:
    """Review Focus 9: both plates' grams and seconds, and a setup line each."""
    shop = Shop(bambuddy, plates=2)
    slices = BambuddySlices(
        {1: sliced_plate(1, 3600, [12.5, 3.0]), 2: sliced_plate(2, 1800, [7.5, 2.0])}
    )
    op = shop.quote()
    assert op["status"] == "succeeded", op
    quote = op["result"]
    assert [plate["index"] for plate in quote["plates"]] == [1, 2]
    assert [plate["print_seconds"] for plate in quote["plates"]] == [3600, 1800]
    assert lines_of(quote) == [
        ("filament", 40),  # 20 g x 20 / 1000
        ("filament", 15),  # 5 g x 30 / 1000
        ("machine_time", 225),  # 1.5 h x 1.5
        ("setup", 50),
        ("setup", 50),
    ]
    assert slices.posted.call_count == 2
    keys = {plate["blob_key"] for plate in quote["plates"]}
    assert len(keys) == 2
    holders = shop.holders()
    assert set(holders) == {quote["id"]}


def test_an_unknown_plate_fails_the_quote(bambuddy: TestClient) -> None:
    """Review Focus 3: the second plate's sliced file cannot be read; nothing is
    recorded and the first plate's hold is dropped."""
    shop = Shop(bambuddy, plates=2)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0]), 2: b"not a sliced file"})
    op = shop.quote()
    assert op["status"] == "failed", op
    assert "plate 2" in op["error"]["detail"]
    assert shop.get_item()["quotes"] == []
    assert shop.holders() == {}


def test_a_slot_at_zero_grams_fails_the_quote(bambuddy: TestClient) -> None:
    shop = Shop(bambuddy, plates=2)
    # 0.01 g reads as 0.0 g: Bambu Studio's `used_g` read to the tenth.
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0]), 2: sliced_plate(2, 600, [1.0, 0.01])})
    op = shop.quote()
    assert op["status"] == "failed", op
    assert "plate 2" in op["error"]["detail"] and "slot 1" in op["error"]["detail"]
    assert shop.get_item()["quotes"] == []
    assert shop.holders() == {}


def test_a_failed_slice_records_a_failed_operation_and_no_quote(bambuddy: TestClient) -> None:
    shop = Shop(bambuddy)
    slices = BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    slices.failure = "no preset for slot 2"
    op = shop.quote()
    assert op["status"] == "failed", op
    assert "no preset for slot 2" in op["error"]["detail"]
    assert shop.get_item()["quotes"] == []


def test_a_resent_quote_request_joins_the_run(bambuddy: TestClient) -> None:
    """Review Focus 5: the same key while the slice still runs is the same operation."""
    shop = Shop(bambuddy)
    slices = BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    slices.running = True
    first = shop.post("quote-1")
    assert first.status_code == 202, first.text
    again = shop.post("quote-1")
    assert again.status_code == 202, again.text
    assert again.json()["id"] == first.json()["id"]
    slices.running = False
    op = follow_operation(bambuddy, first.json(), timeout=120)
    assert op["status"] == "succeeded", op
    assert slices.posted.call_count == 1
    assert len(shop.get_item()["quotes"]) == 1


def test_a_quote_renders_when_the_item_has_no_output(bambuddy: TestClient) -> None:
    """Review Focus 7: an item made by hand renders at its commit, saves an output,
    takes its colour slots and the body's filaments, and the next quote reuses it."""
    shop = Shop(bambuddy, by_hand=True)
    assert shop.item["output_id"] is None and shop.item["colours"] == []
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    op = shop.quote()
    assert op["status"] == "succeeded", op
    item = shop.get_item()
    assert item["output_id"] is not None
    assert [(c["slot"], c["filament_id"]) for c in item["colours"]] == [
        (0, shop.pla),
        (1, shop.petg),
    ]
    output = bambuddy.get(f"/api/v1/outputs/{item['output_id']}").json()
    assert output["model_version"] == item["reference"]["commit"]
    assert output["name"] == f"{shop.order['number']} {item['title']}"

    outputs = output_ids(bambuddy)
    again = shop.quote()
    assert again["status"] == "succeeded", again
    assert shop.get_item()["output_id"] == item["output_id"]
    assert output_ids(bambuddy) == outputs


def test_a_quote_reuses_a_matching_output(bambuddy: TestClient) -> None:
    shop = Shop(bambuddy)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    outputs = output_ids(bambuddy)
    op = shop.quote()
    assert op["status"] == "succeeded", op
    assert shop.get_item()["output_id"] == shop.item["output_id"]
    assert output_ids(bambuddy) == outputs


def test_a_quote_on_a_held_order_is_allowed(bambuddy: TestClient) -> None:
    shop = Shop(bambuddy)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    assert bambuddy.post(f"/api/v1/orders/{shop.order['id']}/hold").status_code == 200
    op = shop.quote()
    assert op["status"] == "succeeded", op


def test_a_quote_is_refused_before_anything_is_written(bambuddy: TestClient) -> None:
    shop = Shop(bambuddy)
    slices = BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    missing = bambuddy.post(
        f"/api/v1/orders/{shop.order['id']}/items/nobody/quote",
        json=shop.request(),
        headers=press(),
    )
    assert missing.status_code == 404, missing.text
    no_filament = shop.post(filaments={"0": shop.pla})
    assert no_filament.status_code == 422, no_filament.text
    assert no_filament.json()["type"].endswith("/filaments")
    free = bambuddy.post(
        f"{BOOK}/filaments", json={"material": "PLA", "colour": "#FFFFFF", "cost_per_kg": "0"}
    ).json()["id"]
    assert bambuddy.put(BOOK, json={}).status_code == 200
    nothing = shop.post(filaments={"0": free, "1": free})
    assert nothing.status_code == 422, nothing.text
    assert nothing.json()["type"].endswith("/price_book")
    assert shop.post(created_by={"kind": "agent"}).status_code == 422
    assert slices.posted.call_count == 0
    item = shop.get_item()
    assert item["quotes"] == [] and item["status"] == "draft"
    assert [slot["filament_id"] for slot in item["colours"]] == [None, None]


# -- the native slicer ------------------------------------------------------------------------


@pytest.fixture
def native(
    settings: Settings,
    s3: S3Settings,
    kek_file: Path,  # noqa: F811
    tmp_path: Path,
    fake_openscad: str,
) -> Iterator[tuple[TestClient, FakeSlicer]]:
    profiles = dialog_profiles(tmp_path / "profiles")
    with respx.mock as router:
        router.route(host="127.0.0.1").pass_through()
        router.route(host="testserver").pass_through()
        upload_route(COPY)
        run_routes()
        copy_routes(router, COPY, b"PK\x03\x04 the item's laid-out copy")
        two_colours(fake_openscad)
        for client in _open(_settings(settings, s3, kek_file, profiles)):
            configure(client)
            with fake_slicer_worker(client, settings.temporal_task_queue_slicer) as fake:
                yield client, fake


def test_a_quote_from_a_slice_on_the_native_slicer(
    native: tuple[TestClient, FakeSlicer],
) -> None:
    client, slicer = native
    shop = Shop(client)
    slicer.sliced = sliced_plate(1, 3600, [12.5, 3.0])
    op = shop.quote()
    assert op["status"] == "succeeded", op
    quote = op["result"]
    assert lines_of(quote)[:3] == [("filament", 25), ("filament", 9), ("machine_time", 150)]
    assert quote["sliced_sha256"] == hashlib.sha256(slicer.sliced).hexdigest()
    assert shop.holders() == {quote["id"]: quote["sliced_blob_key"]}
    assert slicer.calls == 1


def test_a_two_plate_item_is_quoted_on_both_plates_natively(
    native: tuple[TestClient, FakeSlicer],
) -> None:
    client, slicer = native
    shop = Shop(client, plates=2)
    slicer.sliced_by_plate = {
        1: sliced_plate(1, 3600, [12.5, 3.0]),
        2: sliced_plate(2, 1800, [7.5, 2.0]),
    }
    op = shop.quote()
    assert op["status"] == "succeeded", op
    quote = op["result"]
    assert [plate["index"] for plate in quote["plates"]] == [1, 2]
    assert lines_of(quote) == [
        ("filament", 40),
        ("filament", 15),
        ("machine_time", 225),
        ("setup", 50),
        ("setup", 50),
    ]
    assert slicer.calls == 2
