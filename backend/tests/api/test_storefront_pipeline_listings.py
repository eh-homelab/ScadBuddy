# ruff: noqa: F811  (pytest fixtures imported by name, then taken as arguments)
"""A pipeline template on the storefront (#5701, spec 2026-11-17): the shop fixes the
pipeline's own inputs on the listing, publish checks them against the shape the template
declares, every render takes them whole, and a render of one output has a firm price from
a slice. A render of several outputs has none yet: its line is priced by hand."""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import shutil
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.library.outputs import storefront_output_id
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.rendering import RenderedOutput, outputs_for_reference
from scadbuddy.orders.storefront import prepare, slice_price
from scadbuddy.orders.storefront import renders as renders_module
from scadbuddy.orders.storefront.renders import fetch_files, reference_of, variant_of
from scadbuddy.render.inputs import InputsError, inputs_key
from scadbuddy.render.job_models import Job
from scadbuddy.store.cache import materialize_result, result_file
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
from tests.api.test_storefront_public import TOKEN_A
from tests.api.test_storefront_renders import (  # noqa: F401  (fixtures)
    CrudCaller,
    FakeSlicer,
    counted_rows,
    crud,
    dense_red,
    priced,
    render_body,
    render_until_done,
    sliced,
)
from tests.api.test_storefront_warm_prices import blue_renders_blue
from tests.support.operations import press
from tests.support.sliced3mf import sliced_3mf

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
#: Two outputs of one render.
TWO_OUTPUTS = PIPELINE + "    await ctx.output(plates=await ctx.pack([part, part]))\n"
#: Two outputs of one render, each of another colour.
TWO_COLOURS = (
    "INPUTS_VERSION = 1\n\n"
    "async def run(ctx, inputs):\n"
    "    params = inputs['params']\n"
    "    red = await ctx.render('model.scad', **params)\n"
    "    blue = await ctx.render('model.scad', **{**params, 'colour': '#0000FF'})\n"
    "    await ctx.output(plates=[ctx.plate_of([red])])\n"
    "    await ctx.output(plates=[ctx.plate_of([blue])])\n"
)
#: One output of two plates, each of another colour.
TWO_PLATES = (
    "INPUTS_VERSION = 1\n\n"
    "async def run(ctx, inputs):\n"
    "    params = inputs['params']\n"
    "    red = await ctx.render('model.scad', **params)\n"
    "    blue = await ctx.render('model.scad', **{**params, 'colour': '#0000FF'})\n"
    "    await ctx.output(plates=[ctx.plate_of([red]), ctx.plate_of([blue])])\n"
)


def write_pipeline(
    directory: Path,
    *,
    sets_params: list[str] | None = None,
    declared: bool = True,
    source: str = PIPELINE,
) -> None:
    (directory / "pipeline").mkdir(exist_ok=True)
    (directory / "pipeline" / "pipeline.py").write_text(source, encoding="utf-8")
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


def test_a_save_at_a_revision_that_declares_no_shape_keeps_its_version(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    kit = {**template, "commit": committed(client, template["slug"], declared=False)}
    assert create(client, kit_body(kit, pla_red))["inputs_v"] == 1


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


# -- the renders and their price -------------------------------------------------------


#: The price each cart line last showed.
SHOWN = "SELECT price_key, unit_price_minor FROM storefront_item_prices"


def add(crud: CrudCaller, listing: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    body = render_body(listing, values)
    r = crud.post("/draft/items", json=body, key=uuid.uuid4().hex)
    assert r.status_code == 200, r.text
    draft: dict[str, Any] = r.json()
    return draft


def submit(crud: CrudCaller, draft: dict[str, Any]) -> Any:
    body = {"draft_id": draft["id"], "name": "Ada", "email": "ada@example.com"}
    return crud.post("/draft/submit", json=body, key=uuid.uuid4().hex)


def instant(crud: CrudCaller, listing: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    r = crud.post(f"/listings/{listing['id']}/variant", json=render_body(listing, values))
    assert r.status_code == 200, r.text
    answer: dict[str, Any] = r.json()
    return answer


def republish(client: TestClient, listing: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    edited = client.put(
        f"{LISTINGS}/{listing['id']}",
        json={"listing": body, "updated_at": listing["updated_at"]},
    )
    assert edited.status_code == 200, edited.text
    return publish(client, listing["id"])


def estimate(crud: CrudCaller, job_id: str) -> dict[str, Any]:
    r = crud.get(f"/renders/{job_id}/estimate")
    assert r.status_code == 200, r.text
    answer: dict[str, Any] = r.json()
    return answer


def slices(crud: CrudCaller) -> list[dict[str, Any]]:
    return rows(crud, "SELECT * FROM storefront_slice_prices ORDER BY started_at")


def plain_price(
    crud: CrudCaller, template: dict[str, Any], filament: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A listing of the template before it has a pipeline, and its price."""
    plain = publish(crud.api, create(crud.api, listing_body(template, filament))["id"])
    est = priced(crud, render_until_done(crud, TOKEN_A, plain, {})["id"])
    assert est["source"] == "slice"
    return plain, est


def test_a_render_is_priced_by_a_slice_with_the_inputs(
    crud: CrudCaller, kit: dict[str, Any], dense_red: str, sliced: type[FakeSlicer]
) -> None:
    """The visitor's render, its price, the instant read, the cart, the checkout and the
    order's line: the listing's inputs in each. It is one render and one slice,
    whatever the pipeline renders (spec §12, decision 3)."""
    api = crud.api
    listing = publish(api, create(api, kit_body(kit, dense_red))["id"])
    job = render_until_done(crud, TOKEN_A, listing, {"width": 40})
    inputs = _state(api).render.store.read(job["id"]).inputs
    assert inputs["house"] == HOUSE["house"] and inputs["v"] == 1
    assert inputs["params"]["width"] == 40

    est = priced(crud, job["id"])
    assert est["source"] == "slice" and est["price_minor"] > 0
    assert counted_rows(crud, TOKEN_A) == 1
    [row] = slices(crud)
    # The key's digest is the one 3MF's own (§7.4: no price of one output moved).
    stored = _state(api).render.store.read(job["id"])
    assert row["model_sha256"] == file_sha256(crud, stored, 0)
    [(output_id, request, _subject, presets)] = sliced.started
    assert output_id == row["output_id"] == storefront_output_id(row_render_key(crud))
    assert request.printer == "p-1" and presets is not None and set(presets) == {1}
    # The slice read the visitor's own render, with its whole inputs (#6371: hidden).
    meta = _state(api).outputs.get(output_id)
    assert (meta.job_id, meta.source) == (job["id"], "storefront")
    files = f"/api/v1/outputs/{output_id}/files/inputs.json"
    assert json.loads(api.get(files).content) == inputs

    found = instant(crud, listing, {"width": 40})
    assert found["price"]["source"] == "slice"
    assert found["price"]["price_minor"] == est["price_minor"]
    # The visitor's own render of these choices is found by its whole-inputs key.
    assert found["render"] == job["id"]
    preview = crud.get(f"/variants/{row['variant_key']}/preview.glb")
    assert preview.status_code == 200 and preview.content[:4] == b"glTF"

    draft = add(crud, listing, {"width": 40})
    assert draft["items"][0]["price"] == "firm"
    sent = submit(crud, draft)
    assert sent.status_code == 200, sent.text
    assert sent.json()["pay_now"] is True

    [item] = api.get(f"/api/v1/orders/{draft['id']}").json()["items"]
    assert [q["total_minor"] for q in item["quotes"]] == [est["price_minor"]]
    assert item["reference"]["inputs"] == inputs
    assert item["reference"]["render_key"] == inputs_key(kit["slug"], inputs, kit["commit"])
    assert len(sliced.started) == 1 and len(slices(crud)) == 1


def test_an_order_priced_by_hand_renders_the_inputs(
    crud: CrudCaller, kit: dict[str, Any], dense_red: str
) -> None:
    """The order's own render, with no shop slice profile and so no price."""
    api = crud.api
    listing = publish(api, create(api, kit_body(kit, dense_red))["id"])
    job = render_until_done(crud, TOKEN_A, listing, {"width": 40})
    assert estimate(crud, job["id"])["source"] == "none"
    draft = add(crud, listing, {"width": 40})
    sent = submit(crud, draft)
    assert sent.status_code == 200 and sent.json()["pay_now"] is False, sent.text
    [item] = draft["items"]
    started = api.post(f"/api/v1/orders/{draft['id']}/items/{item['id']}/render", headers=press())
    assert started.status_code == 202, started.text
    op = follow_operation(api, started.json(), timeout=120, poll=0.2)
    assert op["status"] == "succeeded", op
    output_id = op["result"]["output_id"]
    files = f"/api/v1/outputs/{output_id}/files/inputs.json"
    inputs = _state(api).render.store.read(job["id"]).inputs
    assert json.loads(api.get(files).content) == inputs
    assert _state(api).outputs.get(output_id).source == "back_office"


def test_a_visitor_with_no_render_adds_a_priced_kit(
    crud: CrudCaller, kit: dict[str, Any], dense_red: str, sliced: type[FakeSlicer]
) -> None:
    """#6921: the instant read's preview and price, with no render of the visitor's own."""
    listing = publish(crud.api, create(crud.api, kit_body(kit, dense_red))["id"])
    priced(crud, render_until_done(crud, TOKEN_A, listing, {})["id"])
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute("DELETE FROM storefront_renders")
    found = instant(crud, listing, {})
    assert found["price"]["source"] == "slice" and found["preview"] is not None
    assert found["render"] is None
    assert add(crud, listing, {})["items"][0]["price"] == "firm"


def test_the_slots_of_a_kit_of_two_plates(
    crud: CrudCaller,
    settings: Settings,
    template: dict[str, Any],
    dense_red: str,
    pla_blue: str,
    sliced: type[FakeSlicer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One slot per colour of the whole kit, in the 3MF's slot order, not of the first
    plate (spec §7.2): the key, the filaments and the slice are of one file."""
    api = crud.api
    blue_renders_blue(settings)
    plate = sliced_3mf(plates={1: (3600, [10.0, 4.0])}, gcode={1: b"M620 S0A\n"})
    monkeypatch.setattr(FakeSlicer, "file", plate)
    kit = {**template, "commit": committed(api, template["slug"], source=TWO_PLATES)}
    body = kit_body(kit, dense_red, colours=[pla_blue, dense_red])
    listing = publish(api, create(api, body)["id"])
    shown = render_until_done(crud, TOKEN_A, listing, {})
    est = priced(crud, shown["id"])
    assert est["source"] == "slice" and est["grams"] == {"0": "10.0", "1": "4.0"}

    state = _state(api)
    job = state.render.store.read(shown["id"])
    assert job.result is not None and len(job.outputs) == 1
    assert [colour.upper() for colour in job.result.colors] == ["#FF0000", "#0000FF"]
    assert len(job.result.plates) == 2
    digest, filaments = variant_at(crud, listing, job)
    assert filaments == [dense_red, pla_blue]
    [row] = slices(crud)
    # `GEOMETRY_DIFFERS` would fail the row of a slice of another file.
    assert (row["status"], row["model_sha256"]) == ("done", digest)
    assert row["slot_filaments"] == [dense_red, pla_blue]
    [(output_id, _request, _subject, presets)] = sliced.started
    assert presets is not None and set(presets) == {1, 2}
    meta = state.outputs.get(output_id)
    assert (meta.job_id, meta.colors) == (job.id, job.result.colors)


def variant_at(crud: CrudCaller, listing: dict[str, Any], job: Job) -> tuple[str, list[str]]:
    """The 3MF and the slot filaments `variant_of` keys the price of ``job`` on."""
    state = _state(crud.api)
    record = state.components.get(ORDERS).listings.get(listing["id"])
    assert crud.api.portal is not None
    digest, slots = crud.api.portal.call(variant_of, state, record, job, {}, None)
    return digest, [filament.id for filament in slots]


def file_sha256(crud: CrudCaller, job: Job, index: int) -> str:
    """The sha256 of the 3MF of output ``index`` of ``job``."""
    state = _state(crud.api)
    path = result_file(state.store.blobs, state.paths.root, job.outputs[index].result.model_3mf)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def two_colours(
    crud: CrudCaller,
    settings: Settings,
    template: dict[str, Any],
    dense_red: str,
    pla_blue: str,
) -> tuple[dict[str, Any], Job]:
    """A published listing of a pipeline that makes a red and a blue output, and a
    visitor's done render of it."""
    blue_renders_blue(settings)
    kit = {**template, "commit": committed(crud.api, template["slug"], source=TWO_COLOURS)}
    body = kit_body(kit, dense_red, colours=[pla_blue, dense_red])
    listing = publish(crud.api, create(crud.api, body)["id"])
    shown = render_until_done(crud, TOKEN_A, listing, {})
    # The page shows every slot of the kit.
    assert [colour.upper() for colour in shown["colors"]] == ["#FF0000", "#0000FF"]
    return listing, _state(crud.api).render.store.read(shown["id"])


def kit_price(crud: CrudCaller, listing: dict[str, Any], job: Job) -> dict[str, Any]:
    """The row of the kit's price, through the price path itself: no visitor's read asks
    for one yet (`one_output`)."""
    state = _state(crud.api)
    record = state.components.get(ORDERS).listings.get(listing["id"])
    portal = crud.api.portal
    assert portal is not None and record is not None
    digest, slots = portal.call(variant_of, state, record, job, {}, None)
    read = functools.partial(
        slice_price.slice_estimate,
        state,
        reference=reference_of(record, job),
        model_sha256=digest,
        filaments=slots,
    )
    deadline = time.monotonic() + 60
    while portal.call(read).source == "pending":
        assert time.monotonic() < deadline, "the price was never worked out"
        time.sleep(0.2)
    [row] = rows(crud, "SELECT * FROM storefront_slice_prices WHERE model_sha256 = %s", digest)
    return row


def row_render_key(crud: CrudCaller) -> str:
    [row] = rows(crud, "SELECT render_key FROM storefront_renders")
    key: str = row["render_key"]
    return key


def test_every_output_of_a_kit_is_sliced_and_priced_once(
    crud: CrudCaller,
    two_colours: tuple[dict[str, Any], Job],
    dense_red: str,
    pla_blue: str,
    sliced: type[FakeSlicer],
) -> None:
    """Spec §7.4, over a real render of two outputs: one price of both, each saved from the
    one job and sliced with the presets of its own slots. A visitor is still shown none of
    it, though the price stands (until PR 5)."""
    api = crud.api
    listing, job = two_colours
    state = _state(api)
    row = kit_price(crud, listing, job)
    assert row["status"] == "done", row["failure"]
    digests = [file_sha256(crud, job, index) for index in (0, 1)]
    text = json.dumps(["kit", *digests], separators=(",", ":"))
    assert row["model_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert row["slot_filaments"] == [dense_red, pla_blue]
    assert [colour.upper() for colour in row["slot_colours"]] == ["#FF0000", "#0000FF"]
    # Each output's one plate reads 10 g of its own first slot: the kit's 0, then its 1.
    assert row["grams"] == {"0": "10.0", "1": "10.0"}
    assert [plate["index"] for plate in row["plates"]] == [1, 2]
    assert row["print_seconds"] == 7200
    started = [(output_id, set(presets or {})) for output_id, _, _, presets in sliced.started]
    assert [slots for _, slots in started] == [{1}, {1}]
    metas = [state.outputs.get(output_id) for output_id, _ in started]
    assert [meta.job_id for meta in metas] == [job.id, job.id]
    assert [meta.colors for meta in metas] == [out.result.colors for out in job.outputs]
    assert {meta.source for meta in metas} == {"storefront"}
    assert row["output_id"] == metas[0].id
    assert counted_rows(crud, TOKEN_A) == 1

    assert estimate(crud, job.id)["source"] == "none"
    assert instant(crud, listing, {})["price"]["source"] == "none"
    draft = add(crud, listing, {})
    assert draft["items"][0]["price"] == "by_hand"
    assert rows(crud, SHOWN) == []
    # The line has every slot of the kit, each with its filament.
    [item] = api.get(f"/api/v1/orders/{draft['id']}").json()["items"]
    assert [slot["filament_id"] for slot in item["colours"]] == [dense_red, pla_blue]
    assert len(sliced.started) == 2


@pytest.mark.parametrize("fault", ["left out", "two jobs"])
def test_outputs_that_are_not_the_whole_of_one_job_have_no_price(
    crud: CrudCaller,
    two_colours: tuple[dict[str, Any], Job],
    sliced: type[FakeSlicer],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    real = outputs_for_reference

    async def faulty(*args: Any, **kwargs: Any) -> list[RenderedOutput]:
        first, second = await real(*args, **kwargs)
        if fault == "left out":
            return [first]
        return [first, dataclasses.replace(second, job_id="f" * 32)]

    monkeypatch.setattr(slice_price, "outputs_for_reference", faulty)
    row = kit_price(crud, *two_colours)
    assert (row["status"], row["failure"]) == ("failed", slice_price.GEOMETRY_DIFFERS)
    assert sliced.started == []


def test_a_kit_whose_later_files_are_gone_is_lost(
    crud: CrudCaller, two_colours: tuple[dict[str, Any], Job], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its first output's files are there, and it has no digest without the rest."""
    _listing, job = two_colours
    state = _state(crud.api)
    portal = crud.api.portal
    assert portal is not None
    assert portal.call(fetch_files, state, job)
    monkeypatch.setattr(renders_module, "_failed", OrderedDict())
    real = materialize_result
    later = job.outputs[1].result

    async def gone(blobs: Any, result: Any) -> None:
        # As a store with no blob of the key: nothing raises, and nothing is there.
        if result == later:
            result_file(blobs, state.paths.root, result.model_3mf).unlink(missing_ok=True)
        else:
            await real(blobs, result)

    monkeypatch.setattr(renders_module, "materialize_result", gone)
    for _ in range(renders_module.FETCH_TRIES):
        assert not renders_module.files_lost(job.id)
        assert not portal.call(fetch_files, state, job)
    assert renders_module.files_lost(job.id)


def test_the_same_3mf_has_one_price_wherever_it_was_made(
    crud: CrudCaller, template: dict[str, Any], dense_red: str, sliced: type[FakeSlicer]
) -> None:
    """The key has no slug and no inputs in it (spec §7.3): the 3MF, its filaments and the
    setup are what is sliced. The template was listed and priced before it had a pipeline,
    and the pipeline makes the same file, so that price is the kit's, with no new slice."""
    api = crud.api
    plain, est = plain_price(crud, template, dense_red)
    [firm] = add(crud, plain, {})["items"]
    assert firm["price"] == "firm"

    # The same listing, moved to the commit that has the pipeline.
    kit = {**template, "commit": committed(api, template["slug"])}
    listing = republish(api, plain, kit_body(kit, dense_red))
    after = render_until_done(crud, TOKEN_A, listing, {})
    found = estimate(crud, after["id"])
    assert (found["source"], found["price_key"]) == ("slice", est["price_key"])
    assert found["price_minor"] == est["price_minor"]
    # The row is the old variant's: it is never moved to this one's key, and its output is
    # of the old commit. The page renders, and reads the price of its own render.
    [row] = slices(crud)
    assert crud.get(f"/variants/{row['variant_key']}/preview.glb").status_code == 404
    assert instant(crud, listing, {})["preview"] is None
    draft = add(crud, listing, {})
    [line] = [item for item in draft["items"] if item["id"] != firm["id"]]
    assert line["price"] == "firm"

    # The old design's line alone lost its price: its listing moved.
    told = submit(crud, draft)
    assert told.status_code == 409, told.text
    assert told.json()["items"] == [{"id": firm["id"], "price": "by_hand"}]
    sent = submit(crud, draft)
    assert sent.status_code == 200, sent.text
    assert len(sliced.started) == 1


@pytest.mark.parametrize("every_output", [False, True])
def test_a_line_added_with_no_price(
    crud: CrudCaller,
    kit: dict[str, Any],
    dense_red: str,
    sliced: type[FakeSlicer],
    every_output: bool,
) -> None:
    """A line added while its slice ran is told of the price that came. One that PR 2 added
    stays by hand: its row does not say whether the render made other outputs."""
    listing = publish(crud.api, create(crud.api, kit_body(kit, dense_red))["id"])
    priced(crud, render_until_done(crud, TOKEN_A, listing, {})["id"])
    draft = add(crud, listing, {})
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_item_prices SET price_key = NULL, unit_price_minor = NULL,"
            " currency = NULL, every_output = %s",
            (every_output,),
        )
    sent = submit(crud, draft)
    if every_output:
        assert sent.status_code == 409, sent.text
        assert sent.json()["items"] == [{"id": draft["items"][0]["id"], "price": "firm"}]
        return
    assert sent.status_code == 200, sent.text
    assert sent.json()["pay_now"] is False
    [item] = crud.api.get(f"/api/v1/orders/{draft['id']}").json()["items"]
    assert item["quotes"] == []


def noted_warms(crud: CrudCaller, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Prices warmed at publish and after Prepare colours (#6922), and each answer."""
    monkeypatch.setattr(_state(crud.api).settings, "storefront_warm_prices", True)
    warmed: list[str] = []
    warm = prepare.warm

    async def noted(*args: Any) -> Any:
        answer = await warm(*args)
        warmed.append(answer.outcome)
        return answer

    monkeypatch.setattr(prepare, "warm", noted)
    return warmed


def wait_warmed(warmed: list[str]) -> None:
    deadline = time.monotonic() + 90
    while warmed[-1:] != ["end"]:
        assert time.monotonic() < deadline, warmed
        time.sleep(0.2)


def test_publish_and_prepare_warm_the_prices_of_a_pipeline(
    crud: CrudCaller,
    settings: Settings,
    kit: dict[str, Any],
    dense_red: str,
    pla_blue: str,
    sliced: type[FakeSlicer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = crud.api
    blue_renders_blue(settings)
    warmed = noted_warms(crud, monkeypatch)
    body = kit_body(
        kit,
        dense_red,
        colours=[dense_red, pla_blue],
        params={"colour": {"kind": "choice", "choices": ["#FF0000", "#0000FF"]}},
    )
    listing = publish(api, create(api, body)["id"])
    wait_warmed(warmed)
    # Its render is a pipeline's, with the listing's inputs, at a visitor's own key.
    [job] = rows(crud, "SELECT inputs FROM render_jobs WHERE state = 'done'")
    assert job["inputs"]["house"] == HOUSE["house"]
    found = instant(crud, listing, {})
    assert found["price"]["source"] == "slice" and found["preview"] is not None
    visited = render_until_done(crud, TOKEN_A, listing, {})
    assert rows(crud, "SELECT id FROM render_jobs WHERE state = 'done'") == [{"id": visited["id"]}]
    assert estimate(crud, visited["id"])["price_key"] == found["price"]["price_key"]

    warmed.clear()
    assert api.post(prepare_path(listing)).status_code in (200, 202)
    assert wait_ready(api, listing, 2)["ready"] == 2
    wait_warmed(warmed)
    assert instant(crud, listing, {"colour": "#0000FF"})["price"]["source"] == "slice"
    assert len(sliced.started) == 2
    assert counted_rows(crud, TOKEN_A) == 0


def test_a_render_of_several_outputs_has_no_price_on_any_path(
    crud: CrudCaller,
    template: dict[str, Any],
    dense_red: str,
    sliced: type[FakeSlicer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An order makes the first output only (spec §7.4), so no customer pays for such a
    kit: not at publish's warming, the estimate, the instant read, the add or the submit,
    though a ``done`` price of its first output's very 3MF stands."""
    api = crud.api
    plain, _est = plain_price(crud, template, dense_red)
    warmed = noted_warms(crud, monkeypatch)
    kit = {**template, "commit": committed(api, template["slug"], source=TWO_OUTPUTS)}
    listing = republish(api, plain, kit_body(kit, dense_red))
    wait_warmed(warmed)
    assert warmed == ["settled", "end"]

    shown = render_until_done(crud, TOKEN_A, listing, {})
    job = _state(api).render.store.read(shown["id"])
    assert len(job.outputs) == 2
    [row] = slices(crud)
    digest, filaments = variant_at(crud, listing, job)
    # The kit's digest is not its first output's, whose price is the plain listing's.
    assert (file_sha256(crud, job, 0), filaments) == (row["model_sha256"], row["slot_filaments"])
    assert digest != row["model_sha256"]

    assert estimate(crud, shown["id"])["source"] == "none"
    found = instant(crud, listing, {})
    assert (found["price"]["source"], found["preview"]) == ("none", None)
    draft = add(crud, listing, {})
    [line] = draft["items"]
    assert line["price"] == "by_hand"
    # The line keeps no variant, so no later submit finds a price for it.
    assert rows(crud, SHOWN) == []
    # A line PR 2 added kept one, of the first output.
    with _state(api).projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO storefront_item_prices (order_item_id, listing_id, model_sha256,"
            " slot_filaments) VALUES (%s, %s, %s, %s)",
            (line["id"], listing["id"], row["model_sha256"], filaments),
        )
    sent = submit(crud, draft)
    assert sent.status_code == 200, sent.text
    assert sent.json()["pay_now"] is False
    [item] = api.get(f"/api/v1/orders/{draft['id']}").json()["items"]
    assert item["quotes"] == []
    assert len(sliced.started) == 1 and len(slices(crud)) == 1


def test_a_republish_with_other_inputs_shows_none_of_the_old_design(
    crud: CrudCaller, kit: dict[str, Any], dense_red: str, sliced: type[FakeSlicer]
) -> None:
    """The same commit, other fixed inputs: the old render has no price, the old variant's
    URL no preview, the old cart line no firm price, and the instant read finds none of
    them. A republish of the same inputs, their keys in another order, changes none of it."""
    api = crud.api
    listing = publish(api, create(api, kit_body(kit, dense_red))["id"])
    old = render_until_done(crud, TOKEN_A, listing, {})
    est = priced(crud, old["id"])
    [before] = rows(crud, "SELECT variant_key, render_key FROM storefront_renders")
    old_preview = f"/variants/{before['variant_key']}/preview.glb"
    assert crud.get(old_preview).status_code == 200
    draft = add(crud, listing, {})
    [line] = draft["items"]
    assert line["price"] == "firm"

    reordered = {"house": {"storeys": 1, "cols": 2}}
    same = republish(api, listing, kit_body(kit, dense_red, inputs=reordered))
    assert estimate(crud, old["id"])["price_key"] == est["price_key"]
    assert crud.get(old_preview).status_code == 200

    taller = {"house": {"cols": 2, "storeys": 3}}
    republished = republish(api, same, kit_body(kit, dense_red, inputs=taller))
    assert estimate(crud, old["id"])["source"] == "none"
    gone = crud.get(old_preview)
    assert gone.status_code == 404, gone.text
    assert gone.json()["type"] == STOREFRONT_PROBLEMS + "variant_not_found"
    found = instant(crud, republished, {})
    assert found["price"]["source"] == "none"
    assert (found["preview"], found["render"]) == (None, None)

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

    told = submit(crud, draft)
    assert told.status_code == 409, told.text
    assert told.json()["items"] == [{"id": line["id"], "price": "by_hand"}]


def test_an_output_of_other_inputs_is_not_the_listings(
    crud: CrudCaller, kit: dict[str, Any], dense_red: str, sliced: type[FakeSlicer]
) -> None:
    """Even a ``done`` row of the current variant gives no preview and no firm line when
    the output it names is of other inputs (#6921)."""
    api = crud.api
    listing = publish(api, create(api, kit_body(kit, dense_red))["id"])
    priced(crud, render_until_done(crud, TOKEN_A, listing, {})["id"])
    taller = {"house": {"cols": 2, "storeys": 3}}
    republished = republish(api, listing, kit_body(kit, dense_red, inputs=taller))
    render_until_done(crud, TOKEN_A, republished, {})
    with _state(api).projection.pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_slice_prices SET variant_key ="
            " (SELECT variant_key FROM storefront_renders ORDER BY id DESC LIMIT 1)"
        )
        conn.execute("DELETE FROM storefront_renders")
    assert instant(crud, republished, {})["preview"] is None
    [row] = slices(crud)
    assert crud.get(f"/variants/{row['variant_key']}/preview.glb").status_code == 404
    assert add(crud, republished, {})["items"][0]["price"] == "by_hand"


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
