# ruff: noqa: F811  (pytest fixtures imported by name, then taken as arguments)
"""A pipeline template on the storefront (#5701, spec 2026-11-17): the shop fixes the
pipeline's own inputs on the listing, publish checks them against the shape the template
declares, and every render takes them whole. No render of one is priced yet: no path starts
a slice, and each line is priced by hand."""

from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.storefront import prepare
from scadbuddy.render.inputs import InputsError, inputs_key
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from scadbuddy.workflows.models import MigrateResult
from tests.api.conftest import follow_operation
from tests.api.test_storefront_listings import (  # noqa: F401  (fixtures)
    LISTINGS,
    SOURCE,
    _state,
    client,
    create,
    kek_file,
    listing_body,
    listing_holds,
    pla_red,
    publish,
    template,
)
from tests.api.test_storefront_prepare import pla_blue, prepare_path, wait_ready  # noqa: F401
from tests.api.test_storefront_public import CRUD, TOKEN_A
from tests.api.test_storefront_renders import (  # noqa: F401  (fixtures)
    CrudCaller,
    FakeSlicer,
    crud,
    dense_red,
    render_body,
    render_until_done,
    sliced,
)
from tests.api.test_storefront_warm_prices import blue_renders_blue
from tests.support.operations import press

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_git]

#: Each render also emits the inputs it ran with, so a test reads what reached the pipeline.
PIPELINE = (
    "import json\n"
    "INPUTS_VERSION = 1\n\n"
    "def migrate(inputs, v):\n"
    "    return {**inputs, 'house': {'cols': inputs.get('cols', 1), 'storeys': 1}}\n\n"
    "async def run(ctx, inputs):\n"
    "    part = await ctx.render('model.scad', **inputs['params'])\n"
    "    await ctx.output(plates=await ctx.pack([part]),"
    " files={'inputs.json': json.dumps(inputs)})\n"
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "house": {
            "type": "object",
            "properties": {
                "cols": {"type": "integer", "minimum": 1, "maximum": 4},
                "storeys": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            "required": ["cols", "storeys"],
            "additionalProperties": False,
        },
        "cols": {"type": "integer"},
    },
    "required": ["house"],
    "additionalProperties": False,
}
HOUSE: dict[str, Any] = {"house": {"cols": 2, "storeys": 1}}


def write_pipeline(
    directory: Path, *, sets_params: list[str] | None = None, declared: bool = True
) -> None:
    (directory / "pipeline").mkdir(exist_ok=True)
    (directory / "pipeline" / "pipeline.py").write_text(PIPELINE, encoding="utf-8")
    (directory / "pipeline" / "inputs.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    meta = json.loads((directory / "model.json").read_text(encoding="utf-8"))
    meta["pipeline"] = {
        "module": "pipeline/pipeline.py",
        "api": 1,
        "sets_params": sets_params or [],
        **({"inputs": "pipeline/inputs.json"} if declared else {}),
    }
    (directory / "model.json").write_text(json.dumps(meta), encoding="utf-8")


def committed(client: TestClient, slug: str, **fields: Any) -> str:
    """The template ``slug`` made a pipeline, and the commit that did it."""
    state = _state(client)
    write_pipeline(state.paths.model_dir(slug), **fields)
    commit = state.history.commit("pipeline", slug)
    assert commit is not None
    return commit


@pytest.fixture
def kit(client: TestClient, template: dict[str, Any]) -> dict[str, Any]:
    return {**template, "commit": committed(client, template["slug"])}


def kit_body(kit: dict[str, Any], filament: str, **fields: Any) -> dict[str, Any]:
    return listing_body(kit, filament, **{"inputs": HOUSE, **fields})


def refusal(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    """The refusal of the publish of a listing of ``body``, which holds nothing."""
    listing = create(client, body)
    refused = client.post(f"{LISTINGS}/{listing['id']}/publish")
    assert refused.status_code == 422, refused.text
    assert listing_holds(client, listing["id"]) == set()
    problem: dict[str, Any] = refused.json()
    return {**problem, "type": problem["type"].removeprefix(STOREFRONT_PROBLEMS)}


def rows(crud: CrudCaller, sql: str, *args: Any) -> list[dict[str, Any]]:
    with _state(crud.api).projection.pool.connection() as conn:
        return list(conn.execute(sql, args).fetchall())


# -- publish ---------------------------------------------------------------------------


def test_publish_keeps_the_inputs_in_the_snapshot(
    client: TestClient, kit: dict[str, Any], pla_red: str
) -> None:
    listing = create(client, kit_body(kit, pla_red))
    # Sent with no version: they are of the commit's own.
    assert (listing["inputs"], listing["inputs_v"]) == (HOUSE, 1)
    published = publish(client, listing["id"])
    snapshot = published["snapshot"]
    assert snapshot["pipeline"] is True
    assert (snapshot["inputs"], snapshot["inputs_version"]) == (HOUSE, 1)
    # The template's parameters stay the listing's to bound and lock.
    assert snapshot["fixed"] == {"height": 5, "colour": "#FF0000", "shape": "round"}


def test_a_template_with_no_pipeline_has_no_inputs(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    listing = publish(client, create(client, listing_body(template, pla_red))["id"])
    assert (listing["inputs"], listing["inputs_v"]) == ({}, None)
    assert (listing["snapshot"]["inputs"], listing["snapshot"]["inputs_version"]) == ({}, 0)
    problem = refusal(client, listing_body(template, pla_red, inputs=HOUSE))
    assert (problem["type"], problem["name"]) == ("listing_inputs_unexpected", "inputs")


def test_a_pipeline_template_that_declares_no_shape_is_not_listed(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    kit = {**template, "commit": committed(client, template["slug"], declared=False)}
    problem = refusal(client, kit_body(kit, pla_red))
    assert (problem["type"], problem["name"]) == ("listing_inputs_undeclared", "inputs")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"inputs": {"house": {"cols": 9, "storeys": 1}}},
            {"type": "listing_inputs_invalid", "name": "inputs.house.cols", "reason": "maximum"},
        ),
        (
            {"inputs": {}},
            {"type": "listing_inputs_invalid", "name": "inputs.house", "reason": "required"},
        ),
        (
            {"inputs_v": 2},
            {"type": "listing_inputs_invalid", "name": "inputs", "reason": "newer"},
        ),
        # The inputs are judged before the parameters, as the spec's table orders them.
        (
            {"inputs": {}, "fixed": {"nope": 1}},
            {"type": "listing_inputs_invalid", "name": "inputs.house", "reason": "required"},
        ),
        ({"fixed": {"nope": 1}}, {"type": "listing_param_unknown", "name": "nope"}),
    ],
)
def test_inputs_outside_the_declared_shape_are_not_published(
    client: TestClient,
    kit: dict[str, Any],
    pla_red: str,
    fields: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    problem = refusal(client, kit_body(kit, pla_red, **fields))
    assert {key: problem[key] for key in expected} == expected


def test_a_parameter_the_pipeline_sets_is_not_offered(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    """Spec §6.1."""
    kit = {**template, "commit": committed(client, template["slug"], sets_params=["height"])}
    offered = {"height": {"kind": "number", "min": 5, "max": 9}}
    for fields in ({"params": offered}, {"fixed": {"height": 9}}):
        problem = refusal(client, kit_body(kit, pla_red, **fields))
        assert problem["type"] == "listing_param_pipeline_set"
        assert problem["name"] == "params.height"
    locked = create(client, kit_body(kit, pla_red, fixed={"height": 5}))
    assert publish(client, locked["id"])["published"] is True


def test_older_inputs_are_migrated_by_the_templates_own_code(
    client: TestClient, kit: dict[str, Any], pla_red: str
) -> None:
    """On the render worker, through `MigrateInputs`. The row keeps what the shop saved:
    the next publish migrates from it again (spec §8)."""
    listing = create(client, kit_body(kit, pla_red, inputs={"cols": 3}, inputs_v=0))
    published = publish(client, listing["id"])
    snapshot = published["snapshot"]
    assert snapshot["inputs"] == {"cols": 3, "house": {"cols": 3, "storeys": 1}}
    assert snapshot["inputs_version"] == 1
    assert (published["inputs"], published["inputs_v"]) == ({"cols": 3}, 0)


def test_inputs_saved_with_no_version_are_migrated_at_a_later_commit(
    client: TestClient, kit: dict[str, Any], pla_red: str
) -> None:
    """The save keeps the version of the commit they were saved at, so the next commit's
    ``INPUTS_VERSION`` is not read as theirs."""
    listing = create(client, kit_body(kit, pla_red, inputs={"cols": 3}))
    assert listing["inputs_v"] == 1
    state = _state(client)
    module = state.paths.model_dir(kit["slug"]) / "pipeline" / "pipeline.py"
    module.write_text(PIPELINE.replace("INPUTS_VERSION = 1", "INPUTS_VERSION = 2"))
    commit = state.history.commit("version 2", kit["slug"])
    body = kit_body(
        {**kit, "commit": commit}, pla_red, inputs=listing["inputs"], inputs_v=listing["inputs_v"]
    )
    saved = client.put(
        f"{LISTINGS}/{listing['id']}",
        json={"listing": body, "updated_at": listing["updated_at"]},
    )
    assert saved.status_code == 200, saved.text
    published = publish(client, listing["id"])
    assert published["snapshot"]["inputs"]["house"] == {"cols": 3, "storeys": 1}
    assert (published["snapshot"]["inputs_version"], published["inputs_v"]) == (2, 1)


@pytest.mark.parametrize("fields", [{"slug": "gone"}, {"commit": "f" * 40}])
def test_a_save_at_a_revision_that_is_not_there_keeps_no_version(
    client: TestClient, kit: dict[str, Any], pla_red: str, fields: dict[str, Any]
) -> None:
    """An export of a commit the history does not know is not trusted either."""
    exported = _state(client).paths.model_revision_dir(kit["slug"], "f" * 40)
    shutil.copytree(_state(client).paths.model_dir(kit["slug"]), exported)
    listing = create(client, kit_body(kit, pla_red, **fields))
    assert (listing["inputs"], listing["inputs_v"]) == (HOUSE, None)


@pytest.mark.parametrize("slug", ["../outside", "absolute", "demo\n", "demo/../../outside", "."])
def test_a_slug_that_is_no_templates_id_is_refused_before_any_path(
    client: TestClient,
    kit: dict[str, Any],
    pla_red: str,
    slug: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model planted outside ``models/`` is not looked at, on a save or at a publish of a
    row that names it."""
    state = _state(client)
    outside = state.paths.models.parent / "outside"
    shutil.copytree(state.paths.model_dir(kit["slug"]), outside)
    if slug == "absolute":
        slug = str(outside)
    made = create(client, kit_body(kit, pla_red))
    looked: list[str] = []
    exists = state.catalogue.exists

    def noted(name: str) -> bool:
        looked.append(name)
        return exists(name)

    monkeypatch.setattr(state.catalogue, "exists", noted)
    body = kit_body({**kit, "slug": slug}, pla_red)
    for refused in (
        client.post(LISTINGS, json=body),
        client.put(
            f"{LISTINGS}/{made['id']}", json={"listing": body, "updated_at": made["updated_at"]}
        ),
    ):
        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == STOREFRONT_PROBLEMS + "listing_source_invalid"
        assert refused.json()["name"] == "slug"
    with state.projection.pool.connection() as conn:
        conn.execute("UPDATE storefront_listings SET slug = %s WHERE id = %s", (slug, made["id"]))
    published = client.post(f"{LISTINGS}/{made['id']}/publish")
    assert published.status_code == 422, published.text
    assert published.json()["type"] == STOREFRONT_PROBLEMS + "listing_reference"
    assert looked == []


def test_a_save_at_a_revision_whose_pipeline_cannot_be_read_keeps_no_version(
    client: TestClient, kit: dict[str, Any], pla_red: str
) -> None:
    """The 0 read there is a placeholder: a later commit must not migrate from it."""
    state = _state(client)
    (state.paths.model_dir(kit["slug"]) / "pipeline" / "pipeline.py").unlink()
    commit = state.history.commit("no module", kit["slug"])
    body = kit_body({**kit, "commit": commit}, pla_red)
    assert create(client, body)["inputs_v"] is None
    assert refusal(client, body)["type"] == "listing_inputs_undeclared"


def test_every_other_write_of_a_listing_keeps_its_inputs(
    client: TestClient, kit: dict[str, Any], pla_red: str
) -> None:
    """Only a save replaces them: not a publish, a picture, a move, an order or an
    unpublish."""
    inputs = {"cols": 3}
    other = create(client, kit_body(kit, pla_red))
    made = create(client, kit_body(kit, pla_red, inputs=inputs, inputs_v=0))
    path = f"{LISTINGS}/{made['id']}"
    published = publish(client, made["id"])
    pictures = {"media_ids": [], "updated_at": published["updated_at"]}
    for answer in (
        client.put(f"{path}/media", json=pictures),
        client.post(f"{path}/move", json={"place": 1}),
        client.put(f"{LISTINGS}/order", json={"ids": [other["id"], made["id"]]}),
        client.post(f"{path}/unpublish"),
        client.post(f"{path}/publish"),
    ):
        assert answer.status_code == 200, answer.text
        row = client.get(path).json()
        assert (row["inputs"], row["inputs_v"]) == (inputs, 0)
    assert row["snapshot"]["inputs"]["house"] == {"cols": 3, "storeys": 1}


def test_a_migration_that_fails_publishes_nothing(
    client: TestClient, kit: dict[str, Any], pla_red: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuses(*_args: Any, **_kwargs: Any) -> MigrateResult:
        raise InputsError("no house was ever built at version 0")

    monkeypatch.setattr(_state(client).render, "migrate_inputs", refuses)
    problem = refusal(client, kit_body(kit, pla_red, inputs_v=0))
    assert problem["type"] == "listing_inputs_migration_failed"
    assert problem["detail"] == "no house was ever built at version 0"


def test_a_save_checks_only_the_shape(
    client: TestClient, kit: dict[str, Any], pla_red: str
) -> None:
    """The commit is not read: inputs that no publish would take are still saved."""
    made = create(client, kit_body(kit, pla_red, inputs={"house": 9}))
    for inputs, slug in (
        ({"params": {}}, "listing_inputs_reserved"),
        ({"v": 1}, "listing_inputs_reserved"),
        ({"note": "x" * 20_000}, "listing_inputs_invalid"),
        ({"note": "a\u0000b"}, "listing_inputs_invalid"),
    ):
        body = kit_body(kit, pla_red, inputs=inputs)
        saved = client.put(
            f"{LISTINGS}/{made['id']}", json={"listing": body, "updated_at": made["updated_at"]}
        )
        for refused in (client.post(LISTINGS, json=body), saved):
            assert refused.status_code == 422, refused.text
            assert refused.json()["type"] == STOREFRONT_PROBLEMS + slug


# -- the public site -------------------------------------------------------------------


def test_a_visitor_neither_sees_nor_sets_the_inputs(
    crud: CrudCaller, kit: dict[str, Any], dense_red: str
) -> None:
    listing = publish(crud.api, create(crud.api, kit_body(kit, dense_red))["id"])
    shown = crud.get(f"/listings/{listing['id']}")
    assert shown.status_code == 200, shown.text
    assert "house" not in shown.text and "inputs" not in shown.json()
    for name in ("house", "v", "params", "inputs"):
        body = render_body(listing, {name: 1})
        for refused in (
            crud.post("/renders", json=body, key=uuid.uuid4().hex),
            crud.post(f"/listings/{listing['id']}/variant", json=body),
        ):
            assert refused.status_code == 422, refused.text
            assert refused.json()["type"] == STOREFRONT_PROBLEMS + "input_outside_listing"
            assert refused.json()["reason"] == "unknown"


# -- the renders, and no price ---------------------------------------------------------


#: The price each cart line last showed.
SHOWN = "SELECT price_key, unit_price_minor FROM storefront_item_prices"


def unpriced(crud: CrudCaller, sliced: type[FakeSlicer]) -> None:
    assert rows(crud, "SELECT * FROM storefront_slice_prices") == []
    assert sliced.started == []


def add(crud: CrudCaller, listing: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    body = render_body(listing, values)
    r = crud.post("/draft/items", json=body, key=uuid.uuid4().hex)
    assert r.status_code == 200, r.text
    draft: dict[str, Any] = r.json()
    return draft


def test_a_render_and_an_order_take_the_inputs_and_no_price(
    crud: CrudCaller, kit: dict[str, Any], dense_red: str, sliced: type[FakeSlicer]
) -> None:
    """The visitor's render, its estimate, the instant read, the cart, the checkout and
    the order's own render: the listing's inputs in each, and no slice in any."""
    api = crud.api
    listing = publish(api, create(api, kit_body(kit, dense_red))["id"])
    job = render_until_done(crud, TOKEN_A, listing, {"width": 40})
    inputs = _state(api).render.store.read(job["id"]).inputs
    assert inputs["house"] == HOUSE["house"] and inputs["v"] == 1
    assert inputs["params"]["width"] == 40

    estimate = crud.get(f"/renders/{job['id']}/estimate")
    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["source"] == "none"
    instant = crud.post(
        f"/listings/{listing['id']}/variant", json=render_body(listing, {"width": 40})
    ).json()
    assert (instant["price"]["source"], instant["preview"]) == ("none", None)
    # The visitor's own render of these choices is found by its whole-inputs key.
    assert instant["render"] == job["id"]

    draft = add(crud, listing, {"width": 40})
    assert draft["items"][0]["price"] == "by_hand"
    assert rows(crud, SHOWN) == [{"price_key": None, "unit_price_minor": None}]
    sent = crud.post(
        "/draft/submit",
        json={"draft_id": draft["id"], "name": "Ada", "email": "ada@example.com"},
        key=uuid.uuid4().hex,
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["pay_now"] is False

    [item] = api.get(f"/api/v1/orders/{draft['id']}").json()["items"]
    reference = item["reference"]
    assert reference["inputs"] == inputs
    assert reference["render_key"] == inputs_key(kit["slug"], inputs, kit["commit"])
    started = api.post(f"/api/v1/orders/{draft['id']}/items/{item['id']}/render", headers=press())
    assert started.status_code == 202, started.text
    op = follow_operation(api, started.json(), timeout=120, poll=0.2)
    assert op["status"] == "succeeded", op
    files = f"/api/v1/outputs/{op['result']['output_id']}/files/inputs.json"
    assert json.loads(api.get(files).content) == inputs
    unpriced(crud, sliced)


def test_a_price_of_the_same_3mf_is_not_a_pipelines(
    crud: CrudCaller,
    template: dict[str, Any],
    dense_red: str,
    sliced: type[FakeSlicer],
) -> None:
    """The template was listed and priced before it had a pipeline, and the pipeline makes
    the same file: the key has no slug and no inputs in it, so the old ``done`` row is the
    key's. The pipeline's line still takes no price from it."""
    api = crud.api
    plain = publish(api, create(api, listing_body(template, dense_red))["id"])
    before = render_until_done(crud, TOKEN_A, plain, {})
    deadline = time.monotonic() + 60
    while crud.get(f"/renders/{before['id']}/estimate").json()["source"] == "pending":
        assert time.monotonic() < deadline, "the price was never worked out"
        time.sleep(0.2)
    assert len(sliced.started) == 1
    [firm] = add(crud, plain, {})["items"]
    assert firm["price"] == "firm"

    # The same listing, moved to the commit that has the pipeline.
    kit = {**template, "commit": committed(api, template["slug"])}
    edited = api.put(
        f"{LISTINGS}/{plain['id']}",
        json={"listing": kit_body(kit, dense_red), "updated_at": plain["updated_at"]},
    )
    assert edited.status_code == 200, edited.text
    listing = publish(api, plain["id"])
    after = render_until_done(crud, TOKEN_A, listing, {})
    assert crud.get(f"/renders/{after['id']}/estimate").json()["source"] == "none"
    instant = crud.post(f"/listings/{listing['id']}/variant", json=render_body(listing, {})).json()
    assert (instant["price"]["source"], instant["preview"]) == ("none", None)
    draft = add(crud, listing, {})
    [line] = [item for item in draft["items"] if item["id"] != firm["id"]]
    assert line["price"] == "by_hand"
    # Taken out and added again (a cart line has no other change), it is priced by hand.
    removed = api.delete(
        f"{CRUD}/draft/items/{line['id']}",
        headers=crud._headers(TOKEN_A, "203.0.113.1", uuid.uuid4().hex),
    )
    assert removed.status_code == 200, removed.text
    draft = add(crud, listing, {})
    [line] = [item for item in draft["items"] if item["id"] != firm["id"]]
    assert line["price"] == "by_hand"

    # The submit finds no price for the pipeline's line: it tells of the old design's
    # line alone, whose listing moved, and then takes the order with no firm price.
    body = {"draft_id": draft["id"], "name": "Ada", "email": "ada@example.com"}
    told = crud.post("/draft/submit", json=body, key=uuid.uuid4().hex)
    assert told.status_code == 409, told.text
    assert told.json()["items"] == [{"id": firm["id"], "price": "by_hand"}]
    sent = crud.post("/draft/submit", json=body, key=uuid.uuid4().hex)
    assert sent.status_code == 200, sent.text
    assert sent.json()["pay_now"] is False
    items = api.get(f"/api/v1/orders/{draft['id']}").json()["items"]
    assert [item["quotes"] for item in items] == [[], []]
    assert rows(crud, SHOWN) == [{"price_key": None, "unit_price_minor": None}] * 2
    assert len(sliced.started) == 1


def test_publish_and_prepare_start_no_slice_of_a_pipeline(
    crud: CrudCaller,
    settings: Settings,
    kit: dict[str, Any],
    dense_red: str,
    pla_blue: str,
    sliced: type[FakeSlicer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6922 warms prices at publish and after Prepare colours, through `estimate_of`
    and not through the visitor's `render_estimate`: a pipeline listing is rendered, and
    warms nothing."""
    api = crud.api
    blue_renders_blue(settings)
    monkeypatch.setattr(_state(api).settings, "storefront_warm_prices", True)
    warmed: list[str] = []
    warm = prepare.warm

    async def noted(*args: Any) -> Any:
        answer = await warm(*args)
        warmed.append(answer.outcome)
        return answer

    monkeypatch.setattr(prepare, "warm", noted)
    body = kit_body(
        kit,
        dense_red,
        colours=[dense_red, pla_blue],
        params={"colour": {"kind": "choice", "choices": ["#FF0000", "#0000FF"]}},
    )
    listing = publish(api, create(api, body)["id"])
    deadline = time.monotonic() + 90
    # The publish's run ends: the default's price is settled with none, then the end.
    while warmed != ["settled", "end"]:
        assert time.monotonic() < deadline, warmed
        time.sleep(0.2)
    # Its renders are a pipeline's, with the listing's inputs, at a visitor's own key.
    [job] = rows(crud, "SELECT inputs FROM render_jobs WHERE state = 'done'")
    assert job["inputs"]["house"] == HOUSE["house"]
    visited = render_until_done(crud, TOKEN_A, listing, {})
    assert rows(crud, "SELECT id FROM render_jobs WHERE state = 'done'") == [{"id": visited["id"]}]

    assert api.post(prepare_path(listing)).status_code in (200, 202)
    assert wait_ready(api, listing, 2)["ready"] == 2
    while len(warmed) < 5:
        assert time.monotonic() < deadline + 90, warmed
        time.sleep(0.2)
    assert warmed[2:] == ["settled", "settled", "end"]
    unpriced(crud, sliced)


def test_a_republish_with_other_inputs_shows_none_of_the_old_design(
    crud: CrudCaller, kit: dict[str, Any], dense_red: str, sliced: type[FakeSlicer]
) -> None:
    """The same commit, other fixed inputs (#6921): another version of the listing, so
    another variant key, and another render key: no price, preview or render of the old
    inputs is the new listing's. Even a ``done`` row of the old variant is not."""
    api = crud.api
    listing = publish(api, create(api, kit_body(kit, dense_red))["id"])
    old = render_until_done(crud, TOKEN_A, listing, {})
    [before] = rows(crud, "SELECT variant_key, render_key FROM storefront_renders")
    prices = _state(api).components.get(ORDERS).slice_prices
    with _state(api).projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO storefront_slice_prices (price_key, model_sha256, status, currency,"
            " unit_price_minor, total_minor, lines, variant_key, listing_id, slot_filaments,"
            " finished_at)"
            " VALUES (%s, %s, 'done', 'USD', 100, 100, '[]', %s, %s, %s::jsonb, now())",
            ("f" * 64, "0" * 64, before["variant_key"], listing["id"], json.dumps([dense_red])),
        )
    assert prices.variant_row(before["variant_key"]) is not None

    taller = {"house": {"cols": 2, "storeys": 3}}
    edited = api.put(
        f"{LISTINGS}/{listing['id']}",
        json={
            "listing": kit_body(kit, dense_red, inputs=taller),
            "updated_at": listing["updated_at"],
        },
    )
    assert edited.status_code == 200, edited.text
    republished = publish(api, listing["id"])
    body = render_body(republished, {})
    instant = crud.post(f"/listings/{listing['id']}/variant", json=body).json()
    assert instant["price"]["source"] == "none"
    assert (instant["preview"], instant["render"]) == (None, None)
    new = render_until_done(crud, TOKEN_A, republished, {})
    assert new["id"] != old["id"]
    assert _state(api).render.store.read(new["id"]).inputs["house"] == taller["house"]
    [after] = rows(
        crud,
        "SELECT variant_key, render_key FROM storefront_renders WHERE job_id = %s",
        new["id"],
    )
    assert after["variant_key"] != before["variant_key"]
    assert after["render_key"] != before["render_key"]


# -- a built-in template (spec §12, the open point) --------------------------------------


@pytest.fixture
def builtin(seed_dir: Path) -> str:
    directory = seed_dir / "kit"
    directory.mkdir()
    (directory / "model.scad").write_text(SOURCE, encoding="utf-8")
    (directory / "model.json").write_text(json.dumps({"name": "Kit"}), encoding="utf-8")
    write_pipeline(directory)
    return "builtin:kit"


@pytest.fixture
def builtin_client(builtin: str, client: TestClient) -> Iterator[TestClient]:
    yield client


def test_a_listing_names_a_builtin_template(
    builtin_client: TestClient, builtin: str, dense_red: str
) -> None:
    """Spec §12: prod's dollhouse is ``builtin:dollhouse-kit``."""
    model = builtin_client.get(f"/api/v1/models/{builtin}").json()
    body = listing_body(
        {"slug": builtin, "commit": model["version"], "media_id": ""},
        dense_red,
        media_ids=[],
        inputs=HOUSE,
    )
    listing = publish(builtin_client, create(builtin_client, body)["id"])
    assert listing["slug"] == builtin and listing["snapshot"]["inputs"] == HOUSE
    job = render_until_done(CrudCaller(builtin_client), TOKEN_A, listing, {})
    assert _state(builtin_client).render.store.read(job["id"]).inputs["house"] == HOUSE["house"]
