"""The slice quote end to end (#4250, spec 2026-10-18 §5.2; plan Rulings 2, 3, 7, 12):
``POST /orders/{id}/items/{item}/quote`` answers 202 with the ``order_item_quote``
operation, whose run lands on the in-process ``library`` worker and slices every plate
through the print dialog's own background slice: Bambuddy's over the ``respx``
recordings ``tests/api/test_print.py`` uses, with ``tests/support/sliced3mf.py``'s files
as the sliced downloads, and the native slicer's with ``tests/support/fake_slicer.py``
in the Rust worker's place."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import trimesh
from fastapi.testclient import TestClient
from temporalio.exceptions import ApplicationError

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.outputs import OutputNotFoundError
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.orders import operations as order_operations
from scadbuddy.orders.lifecycle import QUOTE_HOLDER, Lifecycle
from scadbuddy.orders.models import Quote
from scadbuddy.orders.quote import quote_run
from scadbuddy.orders.reference import ResolvedReference
from scadbuddy.store.content_models import BlobMissingError
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
    ``files[plate]``. Each job names ``copy``, the uploaded file it slices."""

    def __init__(self, files: Mapping[int, bytes], *, copy: int = COPY) -> None:
        self.files = dict(files)
        self.copy = copy
        self.running = False
        self.failure: str | None = None
        self._jobs = itertools.count(9)
        self.jobs: dict[int, int] = {}
        self.posted = respx.route(
            method="POST", path__regex=r"/api/v1/library/files/\d+/slice"
        ).mock(side_effect=self._slice)
        #: Every read of a job: the start's own read, then the follow's polls, which come
        #: only once the start has recorded the job (`bambuddy/preview.py` `_start_plate`).
        self.read = respx.route(method="GET", path__regex=r"/api/v1/slice-jobs/\d+$").mock(
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
            "source_id": self.copy,
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


def test_a_quote_records_its_print_setup(bambuddy: TestClient) -> None:
    """#4673 (order operations spec §5.1): the route takes the tower, supports and
    sequence, and the item's quotes (`order_get`) list each quote's whole setup."""
    shop = Shop(bambuddy)
    slices = BambuddySlices({1: sliced_plate(1, 3600, [12.5, 3.0])})
    # A support setting is written in the preset's shape, so the preset is read (#1899).
    respx.get(f"{API}/slicer/preset-values").mock(
        return_value=httpx.Response(200, json={"resolved": True, "values": {"enable_support": "0"}})
    )
    setup: dict[str, Any] = {
        "prime_tower": {"enabled": True, "no_sparse_layers": True},
        "supports": {"enabled": True, "type": "tree", "threshold_angle": 40},
        "print_sequence": "by object",
    }
    assert shop.post(print_sequence="by plate").status_code == 422
    started = shop.post(**setup)
    assert started.status_code == 202, started.text
    op = follow_operation(bambuddy, started.json(), timeout=120, poll=0.2)
    assert op["status"] == "succeeded", op.get("error")
    # The quote's slice is the print's: the three reach Bambuddy's slicer.
    overrides = json.loads(slices.posted.calls.last.request.content)["process_overrides"]
    assert overrides["enable_prime_tower"] == "1"
    assert overrides["wipe_tower_no_sparse_layers"] == "1"
    assert overrides["enable_support"] == "1"
    assert overrides["print_sequence"] == "by object"
    [listed] = shop.get_item()["quotes"]
    for quote in (op["result"], listed):
        choices = quote["choices"]
        assert choices["prime_tower"] == setup["prime_tower"]
        assert choices["supports"] == {**setup["supports"], "on_build_plate_only": None}
        assert choices["print_sequence"] == "by object"
        assert choices["printer_id"] == shop.request()["printer_id"]
        assert quote["filament_plan"]["slots"] == shop.request()["filament_plan"]["slots"]


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


def worker_stopped() -> ApplicationError:
    """What an attempt's worker reports when its pod stops mid-run (a rollout): the SDK
    cancels the run, and core reports the cancel as a retryable ``WorkerShutdown``
    failure (`workflows/slicing.py` ``SLICE_RETRY``)."""
    return ApplicationError("Worker is shutting down", type="WorkerShutdown")


def first_attempt_dies(
    monkeypatch: pytest.MonkeyPatch,
    dying: Callable[[Callable[..., Awaitable[Quote]], tuple[Any, ...], dict[str, Any]], Any],
) -> list[str]:
    """The quote's run, its first attempt replaced by ``dying`` (which gets the real run
    and its arguments): the pod that held it stopped. The quote ids each attempt ran
    with."""
    real = quote_run
    ids: list[str] = []

    async def run(*args: Any, **kwargs: Any) -> Quote:
        ids.append(kwargs["quote_id"])
        if len(ids) == 1:
            await dying(real, args, kwargs)
            raise worker_stopped()
        return await real(*args, **kwargs)

    monkeypatch.setattr(order_operations, "quote_run", run)
    return ids


def test_a_quote_whose_pod_stopped_mid_slice_finishes_on_the_next_attempt(
    bambuddy: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5353: a rollout stops the API pod, and the ``library`` worker in it, while the
    quote slices. The operation is left running with no worker; the next attempt runs
    it to the end, and a re-send with the same key answers with the quote."""
    shop = Shop(bambuddy)
    slices = BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})

    async def mid_slice(
        real: Callable[..., Awaitable[Quote]], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        slices.running = True
        task = asyncio.ensure_future(real(*args, **kwargs))
        try:
            # Stopped once the slice is started and recorded: the follow's first poll
            # (#5442). Stopped as soon as Bambuddy took the job, the start may not have
            # recorded it yet, and the next attempt then slices again.
            while slices.read.call_count < 2 and not task.done():
                await asyncio.sleep(0.05)
            assert not task.done(), "the first attempt ended before its slice was followed"
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        finally:
            slices.running = False

    ids = first_attempt_dies(monkeypatch, mid_slice)
    started = shop.post("quote-rollout")
    assert started.status_code == 202, started.text
    op = follow_operation(bambuddy, started.json(), timeout=120)
    assert op["status"] == "succeeded", op
    quote = op["result"]
    assert len(ids) == 2 and ids[0] == ids[1] == quote["id"]
    # The next attempt follows the background slice the first one started
    # (`bambuddy/preview.py` reuses a recorded job of the same copy and presets).
    assert slices.posted.call_count == 1
    assert [q["id"] for q in shop.get_item()["quotes"]] == [quote["id"]]
    assert shop.holders() == {quote["id"]: quote["sliced_blob_key"]}

    again = shop.post("quote-rollout")
    assert again.status_code == 200, again.text
    assert again.json() == quote


def test_a_quote_whose_pod_stopped_after_recording_answers_with_that_quote(
    bambuddy: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5353: the pod stops after the quote's row committed, before its attempt
    reported. The next attempt finds the quote by the operation's id and answers with
    it: nothing is sliced or recorded twice, and its sliced file stays held."""
    shop = Shop(bambuddy)
    slices = BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    recorded: list[Quote] = []

    async def after_recording(
        real: Callable[..., Awaitable[Quote]], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        recorded.append(await real(*args, **kwargs))

    ids = first_attempt_dies(monkeypatch, after_recording)
    started = shop.post("quote-rollout")
    assert started.status_code == 202, started.text
    op = follow_operation(bambuddy, started.json(), timeout=120)
    assert op["status"] == "succeeded", op
    quote = op["result"]
    assert len(ids) == 2 and [r.id for r in recorded] == [quote["id"]]
    assert slices.posted.call_count == 1
    assert [q["id"] for q in shop.get_item()["quotes"]] == [quote["id"]]
    assert shop.holders() == {quote["id"]: quote["sliced_blob_key"]}

    again = shop.post("quote-rollout")
    assert again.status_code == 200, again.text
    assert again.json() == quote


def test_a_bug_in_the_quotes_run_is_not_tried_again(
    bambuddy: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5353 review: a failure that another attempt would only repeat (here a
    ``TypeError``) fails the operation on its first attempt, as the unexpected failure,
    instead of rendering and slicing three times."""
    shop = Shop(bambuddy)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    calls: list[str] = []

    async def buggy(*args: Any, **kwargs: Any) -> Quote:
        calls.append(kwargs["quote_id"])
        raise TypeError("a bug")

    monkeypatch.setattr(order_operations, "quote_run", buggy)
    op = shop.quote()
    assert op["status"] == "failed", op
    assert op["error"]["status"] == 500
    assert len(calls) == 1
    assert shop.get_item()["quotes"] == []


@pytest.mark.parametrize(
    "gone",
    [OutputNotFoundError("an output deleted mid-run"), BlobMissingError("a blob swept mid-run")],
    ids=["output", "blob"],
)
def test_an_output_or_blob_gone_mid_run_is_tried_again(
    bambuddy: TestClient, monkeypatch: pytest.MonkeyPatch, gone: Exception
) -> None:
    """#5415: an output deleted, or a blob swept, while the run read it is a
    ``LookupError`` but no bug: the next attempt saves and slices again."""
    shop = Shop(bambuddy)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    real = quote_run
    calls: list[str] = []

    async def run(*args: Any, **kwargs: Any) -> Quote:
        calls.append(kwargs["quote_id"])
        if len(calls) == 1:
            raise gone
        return await real(*args, **kwargs)

    monkeypatch.setattr(order_operations, "quote_run", run)
    op = shop.quote()
    assert op["status"] == "succeeded", op
    assert len(calls) == 2
    assert [q["id"] for q in shop.get_item()["quotes"]] == [op["result"]["id"]]


def test_a_quote_whose_pod_stopped_after_saving_the_output_saves_it_once(
    bambuddy: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5390: the pod stops after the item's output was saved and before the item's
    ``output_id`` write. The next attempt finds that output by the id the operation
    saves under, sets it on the item, and saves no second output."""
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    before = output_ids(bambuddy)
    real = Lifecycle.update_item
    stops: list[str] = []

    def update_item(self: Lifecycle, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("output_id") is not None and not stops:
            stops.append(kwargs["output_id"])
            raise worker_stopped()
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Lifecycle, "update_item", update_item)
    op = shop.quote()
    assert op["status"] == "succeeded", op
    assert len(stops) == 1
    added = [output for output in output_ids(bambuddy) if output not in before]
    assert added == stops
    assert shop.get_item()["output_id"] == stops[0]


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
    # A registry printer gone since the dialog named it, with no Bambuddy printer beside
    # it (#4625): refused, never priced at the default printer's rate.
    gone = shop.post(printer="00000000-0000-0000-0000-000000000000", printer_id=None)
    assert gone.status_code == 422, gone.text
    assert "no printer" in gone.json()["detail"]
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


def _slice(shop: Shop, **extra: Any) -> dict[str, Any]:
    """A slice quote posted with ``extra`` and followed to its end."""
    started = shop.post(**extra)
    assert started.status_code == 202, started.text
    op: dict[str, Any] = follow_operation(shop.client, started.json(), timeout=120, poll=0.2)
    assert op["status"] == "succeeded", op
    return op


WIDER = {"params": {"width": 14}}


def test_a_slice_with_new_inputs_quotes_a_new_revision(bambuddy: TestClient) -> None:
    """#4673 PR 3 (order operations spec §5.1): new inputs in a slice make a revision
    first, through the revise path, and the quote is of it. The same inputs again make
    none: only another print setup under the same revision."""
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    first = _slice(shop)["result"]
    [one] = shop.get_item()["revisions"]
    assert first["revision_id"] == one["id"]

    quote = _slice(shop, inputs=WIDER)["result"]
    item = shop.get_item()
    assert [r["number"] for r in item["revisions"]] == [1, 2]
    two = item["revisions"][-1]
    assert two["reference"]["inputs"]["params"]["width"] == 14
    assert item["reference"]["inputs"]["params"]["width"] == 14
    assert item["current_revision_id"] == two["id"]
    assert quote["revision_id"] == two["id"]

    again = _slice(shop, inputs=two["reference"]["inputs"])["result"]
    assert [r["number"] for r in shop.get_item()["revisions"]] == [1, 2]
    assert again["revision_id"] == two["id"]


def test_a_resent_slice_with_new_inputs_makes_one_revision(bambuddy: TestClient) -> None:
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    _slice(shop)
    key = "slice-wider"
    first = shop.post(key, inputs=WIDER)
    again = shop.post(key, inputs=WIDER)
    assert first.status_code == again.status_code == 202, again.text
    assert first.json()["id"] == again.json()["id"]
    follow_operation(bambuddy, first.json(), timeout=120, poll=0.2)
    assert [r["number"] for r in shop.get_item()["revisions"]] == [1, 2]


def test_a_slice_with_new_inputs_after_acceptance_revises_after_acceptance(
    bambuddy: TestClient,
) -> None:
    """On an accepted order the revision is #5350's: the accepted quote stays accepted,
    and the item is revised after acceptance, so the new quote can be accepted."""
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    accepted = _slice(shop)["result"]
    assert bambuddy.post(f"{shop.path}/quotes/{accepted['id']}/accept").status_code == 200
    assert bambuddy.post(f"/api/v1/orders/{shop.order['id']}/accept").status_code == 200

    quote = _slice(shop, inputs=WIDER)["result"]
    item = shop.get_item()
    assert item["revised_after_acceptance"] is True
    assert quote["revision_id"] == item["current_revision_id"]
    assert [q["id"] for q in item["quotes"] if q["accepted"]] == [accepted["id"]]
    events = bambuddy.get(f"/api/v1/orders/{shop.order['id']}/events").json()
    assert [e["kind"] for e in events].count("revision.added_after_acceptance") == 1
    assert bambuddy.post(f"{shop.path}/quotes/{quote['id']}/accept").status_code == 200


def test_a_slice_with_new_inputs_keeps_the_revise_paths_refusals(bambuddy: TestClient) -> None:
    """The revise path's refusals apply unchanged (§5.1): on hold, an accepted item takes
    no new design, so the slice starts nothing. Inputs its template cannot take are 422,
    as a revision's."""
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    accepted = _slice(shop)["result"]
    assert bambuddy.post(f"{shop.path}/quotes/{accepted['id']}/accept").status_code == 200
    assert bambuddy.post(f"/api/v1/orders/{shop.order['id']}/accept").status_code == 200
    assert bambuddy.post(f"/api/v1/orders/{shop.order['id']}/hold").status_code == 200
    held = shop.post(inputs=WIDER)
    assert held.status_code == 409, held.text
    assert held.json()["type"].endswith("/order_on_hold")
    assert [r["number"] for r in shop.get_item()["revisions"]] == [1]

    bad = shop.post(commit="0" * 40)
    assert bad.status_code == 422, bad.text


def test_a_refused_slice_with_new_inputs_writes_no_revision(bambuddy: TestClient) -> None:
    """#5761 review: the body's own checks (filaments, support slots, price book, machine
    rate) run before the revision, so a 422 writes nothing and an accepted item keeps
    its one revise after acceptance."""
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    accepted = _slice(shop)["result"]
    assert bambuddy.post(f"{shop.path}/quotes/{accepted['id']}/accept").status_code == 200
    assert bambuddy.post(f"/api/v1/orders/{shop.order['id']}/accept").status_code == 200
    archived = bambuddy.post(f"/api/v1/orders/price-book/filaments/{shop.petg}/archive")
    assert archived.status_code == 200, archived.text

    refused = shop.post(inputs=WIDER)
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"].endswith("/filaments")
    item = shop.get_item()
    assert [r["number"] for r in item["revisions"]] == [1]
    assert item["revised_after_acceptance"] is False


def test_a_replayed_slice_makes_no_revision_after_anothers(bambuddy: TestClient) -> None:
    """#5761 review: a replayed key is answered from its operation before any revision is
    made, even when the item's design has changed since."""
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    _slice(shop)
    first = shop.post("slice-replay", inputs=WIDER)
    assert first.status_code == 202, first.text
    op = follow_operation(bambuddy, first.json(), timeout=120, poll=0.2)
    reference = shop.get_item()["reference"]
    other = bambuddy.post(
        f"{shop.path}/revisions",
        json={
            "reference": {
                "slug": reference["slug"],
                "commit": reference["commit"],
                "inputs": {"params": {"width": 16}},
            }
        },
    )
    assert other.status_code == 201, other.text

    again = shop.post("slice-replay", inputs=WIDER)
    # The finished operation's own answer: its quote.
    assert again.status_code == 200, again.text
    assert again.json()["id"] == op["result"]["id"]
    assert [r["number"] for r in shop.get_item()["revisions"]] == [1, 2, 3]


def test_a_slice_with_new_inputs_and_no_filaments_keeps_the_items(bambuddy: TestClient) -> None:
    """#5761 review: the new revision has no slots until its render, so the item's own
    filaments go with the slice: one with new inputs and no `filaments` prices every
    slot as before, rather than failing after its revision was written."""
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    accepted = _slice(shop)["result"]
    assert bambuddy.post(f"{shop.path}/quotes/{accepted['id']}/accept").status_code == 200
    assert bambuddy.post(f"/api/v1/orders/{shop.order['id']}/accept").status_code == 200

    quote = _slice(shop, inputs=WIDER, filaments={})["result"]
    item = shop.get_item()
    assert quote["revision_id"] == item["current_revision_id"]
    assert [(c["slot"], c["filament_id"]) for c in item["colours"]] == [
        (0, shop.pla),
        (1, shop.petg),
    ]


def test_a_short_commit_of_the_items_design_makes_no_revision(bambuddy: TestClient) -> None:
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    _slice(shop)
    commit = shop.get_item()["reference"]["commit"]
    _slice(shop, commit=commit[:7])
    assert [r["number"] for r in shop.get_item()["revisions"]] == [1]
    malformed = shop.post(commit="not-a-commit")
    assert malformed.status_code == 422, malformed.text


def test_two_racing_slices_with_one_design_make_one_revision(
    bambuddy: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5761 review: two first sends both read the old design before either revises.
    The second one's revision, under the order's lock, finds the design already made
    and writes nothing, so the quote is of the one revision."""
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    _slice(shop)
    real = Lifecycle.add_revision
    raced: list[bool] = []

    def add_revision(self: Lifecycle, *args: Any, **kwargs: Any) -> Any:
        if not raced:
            raced.append(True)
            real(self, *args, **kwargs)  # the other send, which read the same old design
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Lifecycle, "add_revision", add_revision)
    quote = _slice(shop, inputs=WIDER)["result"]
    item = shop.get_item()
    assert [r["number"] for r in item["revisions"]] == [1, 2]
    assert quote["revision_id"] == item["current_revision_id"]


def test_a_slice_naming_a_slot_its_new_design_adds_quotes_it(
    bambuddy: TestClient, fake_openscad: str
) -> None:
    """#5761 review: the check before the revision judges only the filaments the body
    names, not the old design's slots, so a new design's added slot named in the body
    quotes on the first send."""
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    _slice(shop)
    directory = Path(fake_openscad).parent
    drawn = write_openscad_3mf(
        directory / "three.3mf",
        [
            ("Color 1", "#FF000000", trimesh.creation.box(extents=(10, 10, 5))),
            (
                "Color 2",
                "#00FF0000",
                trimesh.creation.box(extents=(4, 4, 8)).apply_translation((20, 0, 0)),
            ),
            (
                "Color 3",
                "#0000FF00",
                trimesh.creation.box(extents=(4, 4, 8)).apply_translation((40, 0, 0)),
            ),
        ],
    )
    set_fake_env(directory, "FAKE_3MF", str(drawn))
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0, 1.0])})

    plan = {"slots": [{"slot_id": n, "spool_id": spool} for n, spool in ((1, 9), (2, 5), (3, 9))]}
    quote = _slice(shop, inputs=WIDER, filaments={"2": shop.pla}, filament_plan=plan)["result"]
    item = shop.get_item()
    assert quote["revision_id"] == item["current_revision_id"]
    assert [(c["slot"], c["filament_id"]) for c in item["colours"]] == [
        (0, shop.pla),
        (1, shop.petg),
        (2, shop.pla),
    ]


def test_two_racing_slices_of_different_designs_revise_after_acceptance_once(
    bambuddy: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5761 review: the path is decided under the order's lock, so a slice that loses
    the race to another design's takes the plain revise path, as a later send would."""
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    accepted = _slice(shop)["result"]
    assert bambuddy.post(f"{shop.path}/quotes/{accepted['id']}/accept").status_code == 200
    assert bambuddy.post(f"/api/v1/orders/{shop.order['id']}/accept").status_code == 200
    real = Lifecycle.add_revision
    raced: list[bool] = []

    def add_revision(
        self: Lifecycle, order_id: str, item_id: str, by: Any, resolved: Any, **kw: Any
    ) -> Any:
        if not raced:
            raced.append(True)
            other = resolved.reference.model_copy(update={"inputs": {"params": {"width": 16}}})
            real(self, order_id, item_id, by, ResolvedReference(other, []), **kw)
        return real(self, order_id, item_id, by, resolved, **kw)

    monkeypatch.setattr(Lifecycle, "add_revision", add_revision)
    checks = Lifecycle._revisable_after_acceptance
    paths: list[str] = []

    def revisable(self: Lifecycle, *args: Any) -> None:
        paths.append("after acceptance")
        checks(self, *args)

    monkeypatch.setattr(Lifecycle, "_revisable_after_acceptance", revisable)
    quote = _slice(shop, inputs=WIDER)["result"]
    item = shop.get_item()
    assert [r["number"] for r in item["revisions"]] == [1, 2, 3]
    assert quote["revision_id"] == item["current_revision_id"]
    assert paths == ["after acceptance"]


def test_a_slices_support_slot_on_its_new_designs_colour_slot_is_refused(
    bambuddy: TestClient,
) -> None:
    """#5761 review: a support slot is judged against the new design's colour slots: one
    the body also names as a colour is refused before the revision, and one the render
    gives is refused by the run, never priced with the colour's filament silently."""
    shop = Shop(bambuddy, by_hand=True)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    _slice(shop)

    both = shop.post(inputs=WIDER, support_filaments={"0": shop.petg})
    assert both.status_code == 422, both.text
    assert "support_filaments" in both.text
    assert [r["number"] for r in shop.get_item()["revisions"]] == [1]

    started = shop.post(inputs=WIDER, filaments={}, support_filaments={"1": shop.pla})
    assert started.status_code == 202, started.text
    ended = follow_operation(bambuddy, started.json(), timeout=120, poll=0.2)
    assert ended["status"] == "failed", ended
    assert "support_filaments" in json.dumps(ended)
    # Named as the new design's colour slot, with the way out.
    assert "send the slice again without that support slot" in json.dumps(ended)


def test_a_slice_quotes_plate_preview_is_its_sliced_files_image(bambuddy: TestClient) -> None:
    """#4673 PR 4 (order operations spec §5.2): each plate of a slice quote shows the
    image its sliced file holds; a plate it lacks, or a quote with no slice, has none."""
    shop = Shop(bambuddy, by_hand=True)
    png = b"\x89PNG\r\n\x1a\nplate one"
    BambuddySlices(
        {
            1: sliced_3mf(
                plates={1: (600, [1.0, 1.0])},
                gcode={1: b"M620 S0A\nM620 S1A\n"},
                images={1: png},
            )
        }
    )
    quote = _slice(shop)["result"]
    url = f"{shop.path}/quotes/{quote['id']}/plates/1/thumbnail"
    shown = bambuddy.get(url)
    assert shown.status_code == 200, shown.text
    assert shown.headers["content-type"] == "image/png"
    assert shown.content == png
    assert bambuddy.get(f"{shop.path}/quotes/{quote['id']}/plates/2/thumbnail").status_code == 404
    assert bambuddy.get(f"{shop.path}/quotes/nobody/plates/1/thumbnail").status_code == 404
    elsewhere = f"/api/v1/orders/{shop.order['id']}/items/nobody/quotes/{quote['id']}"
    assert bambuddy.get(f"{elsewhere}/plates/1/thumbnail").status_code == 404
