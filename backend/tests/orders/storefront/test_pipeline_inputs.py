"""A pipeline template's own inputs on a listing (#5701, spec 2026-11-17 §4.3, §6.1): what
publish checks of them, in the spec's order, with no app. The publish itself is in
``tests/api/test_storefront_pipeline_listings.py``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.storefront.pipeline_inputs import (
    MAX_LISTING_INPUTS_BYTES,
    MAX_SCHEMA_FILE_BYTES,
    PipelineFacts,
    check_set_params,
    check_shape,
    check_unexpected,
    pipeline_facts,
    settled_inputs,
    shows_design,
)
from scadbuddy.orders.storefront.wire import Listing, ListingInput, ListingSnapshot
from scadbuddy.render.inputs import InputsError
from scadbuddy.render.schema import CustomizerSchema, Parameter
from scadbuddy.render.submit import RenderService
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from scadbuddy.workflows.models import MigrateResult

HEAD = "a" * 40
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
    },
    "required": ["house"],
    "additionalProperties": False,
}
HOUSE: dict[str, Any] = {"house": {"cols": 2, "storeys": 1}}
PARAMS = CustomizerSchema(
    parameters=[
        Parameter(name="width", type="number", initial=50, min=10, max=100),
        Parameter(name="piece", type="string", initial="wall"),
        Parameter(name="style", type="string", initial="plain"),
    ]
)
FACTS = PipelineFacts(inputs_version=1, schema=SCHEMA, fault="", sets_params=("piece", "gone"))


def listing(**fields: Any) -> ListingInput:
    return ListingInput.model_validate(
        {"slug": "kit", "commit": HEAD, "title": "Kit", "inputs": HOUSE, **fields}
    )


@contextmanager
def refused_as(slug: str, name: str | None = None, reason: str | None = None) -> Iterator[None]:
    with pytest.raises(ApiError) as refused:
        yield
    assert refused.value.status == 422
    assert refused.value.type == STOREFRONT_PROBLEMS + slug, refused.value.detail
    assert refused.value.extensions.get("name") == name
    assert refused.value.extensions.get("reason") == reason


class FakeRender:
    """`RenderService.migrate_inputs`: answers ``result`` or raises it, every call kept."""

    def __init__(self, result: dict[str, Any] | Exception | None = None) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def migrate_inputs(
        self, slug: str, inputs: Mapping[str, Any], *, version: str | None
    ) -> MigrateResult:
        self.calls.append((slug, dict(inputs), version))
        assert self.result is not None
        if isinstance(self.result, Exception):
            raise self.result
        return MigrateResult(inputs=self.result, from_version=0, to_version=1)


def settle(
    body: ListingInput, facts: PipelineFacts = FACTS, render: FakeRender | None = None
) -> dict[str, Any]:
    fake = render or FakeRender()
    return asyncio.run(settled_inputs(cast(RenderService, fake), body, "kit", HEAD, facts, PARAMS))


# -- the shape a save checks -----------------------------------------------------------


@pytest.mark.parametrize("key", ["params", "v"])
def test_a_key_of_the_hosts_is_reserved(key: str) -> None:
    with refused_as("listing_inputs_reserved", f"inputs.{key}"):
        check_shape({**HOUSE, key: {}})


def test_inputs_are_at_most_16_kib() -> None:
    room = MAX_LISTING_INPUTS_BYTES - len('{"note":""}')
    check_shape({"note": "x" * room})
    with refused_as("listing_inputs_invalid", "inputs", "too_large"):
        check_shape({"note": "x" * (room + 1)})


@pytest.mark.parametrize(
    "inputs",
    [
        {"n": float("nan")},
        {"n": float("inf")},
        {"s": "a\x00b"},
        {"a\x00b": 1},
        {"deep": [{"s": "\x00"}]},
        {"s": "\ud800"},
    ],
)
def test_inputs_no_row_can_hold_are_refused(inputs: dict[str, Any]) -> None:
    with refused_as("listing_inputs_invalid", "inputs", "unstorable"):
        check_shape(inputs)


# -- publish, in the spec's order ------------------------------------------------------


def test_inputs_on_a_template_with_no_pipeline_are_unexpected() -> None:
    check_unexpected(listing(inputs={}))
    with refused_as("listing_inputs_unexpected", "inputs"):
        check_unexpected(listing())


def test_the_inputs_that_fit_are_kept() -> None:
    assert settle(listing()) == HOUSE
    assert settle(listing(inputs_v=1)) == HOUSE


@pytest.mark.parametrize(
    ("facts", "fields", "slug", "name", "reason"),
    [
        # Row 2 before row 3: nothing is judged without a declared shape.
        (
            PipelineFacts(1, None, "no shape"),
            {"inputs": {"params": {}}},
            "listing_inputs_undeclared",
            "inputs",
            None,
        ),
        # Row 3 before row 4.
        (
            FACTS,
            {"inputs": {"v": 1, "note": "x" * 20_000}},
            "listing_inputs_reserved",
            "inputs.v",
            None,
        ),
        # Row 4 before row 5.
        (
            FACTS,
            {"inputs": {"note": "x" * 20_000}, "inputs_v": 2},
            "listing_inputs_invalid",
            "inputs",
            "too_large",
        ),
        # Row 5 before row 7.
        (
            FACTS,
            {"inputs": {"house": 1}, "inputs_v": 2},
            "listing_inputs_invalid",
            "inputs",
            "newer",
        ),
    ],
)
def test_the_first_fault_wins(
    facts: PipelineFacts, fields: dict[str, Any], slug: str, name: str, reason: str | None
) -> None:
    with refused_as(slug, name, reason):
        settle(listing(**fields), facts)


@pytest.mark.parametrize(
    ("inputs", "name", "reason"),
    [
        ({"house": {"cols": 9, "storeys": 1}}, "inputs.house.cols", "maximum"),
        ({"house": {"cols": 0, "storeys": 1}}, "inputs.house.cols", "minimum"),
        ({"house": {"cols": "2", "storeys": 1}}, "inputs.house.cols", "type"),
        ({"house": 1}, "inputs.house", "type"),
        # A missing or an extra key is named itself, not its object.
        ({}, "inputs.house", "required"),
        ({"house": {"cols": 2}}, "inputs.house.storeys", "required"),
        ({**HOUSE, "shed": 1}, "inputs.shed", "additional"),
        ({"house": {"cols": 2, "storeys": 1, "attic": 1}}, "inputs.house.attic", "additional"),
    ],
)
def test_a_value_outside_the_declared_shape_is_refused_by_its_path(
    inputs: dict[str, Any], name: str, reason: str
) -> None:
    with refused_as("listing_inputs_invalid", name, reason):
        settle(listing(inputs=inputs))


def test_older_inputs_are_migrated_once_with_the_settled_params() -> None:
    migrated = {**HOUSE, "params": {"width": 99}, "v": 1}
    render = FakeRender(migrated)
    body = listing(
        inputs={"cols": 2},
        inputs_v=0,
        params={"width": {"kind": "number", "min": 20, "max": 80, "default": 30}},
        fixed={"style": "brick", "unknown": 1},
    )
    # The result less the host's keys: the bounds and the locks stay the shop's.
    assert settle(body, render=render) == HOUSE
    params = {"width": 30, "piece": "wall", "style": "brick"}
    assert render.calls == [("kit", {"cols": 2, "params": params, "v": 0}, HEAD)]


@pytest.mark.parametrize("inputs_v", [None, 1])
def test_inputs_of_the_commits_version_run_no_migration(inputs_v: int | None) -> None:
    render = FakeRender()
    settle(listing(inputs_v=inputs_v), render=render)
    assert render.calls == []


def test_a_refused_migration_says_the_templates_message() -> None:
    render = FakeRender(InputsError("a house of 9 storeys cannot be built"))
    with refused_as("listing_inputs_migration_failed", "inputs"):
        settle(listing(inputs_v=0), render=render)
    with pytest.raises(ApiError) as refused:
        settle(listing(inputs_v=0), render=render)
    assert refused.value.detail == "a house of 9 storeys cannot be built"


@pytest.mark.parametrize("status", [413, 503, 504])
def test_a_migration_that_could_not_run_passes_through(status: int) -> None:
    problem = ApiError(status, "try again")
    with pytest.raises(ApiError) as refused:
        settle(listing(inputs_v=0), render=FakeRender(problem))
    assert refused.value is problem


@pytest.mark.parametrize(
    ("migrated", "name", "reason"),
    [
        ({"house": {"cols": 9, "storeys": 1}}, "inputs.house.cols", "maximum"),
        ({**HOUSE, "note": "x" * 20_000}, "inputs", "too_large"),
    ],
)
def test_the_migrated_inputs_are_the_ones_checked(
    migrated: dict[str, Any], name: str, reason: str
) -> None:
    with refused_as("listing_inputs_invalid", name, reason):
        settle(listing(inputs_v=0), render=FakeRender(migrated))


# -- the parameters the pipeline sets (§6.1) --------------------------------------------


def test_a_parameter_the_pipeline_sets_is_not_the_listings() -> None:
    sets = FACTS.sets_params
    check_set_params(listing(), PARAMS, sets)
    check_set_params(listing(fixed={"piece": "wall", "style": "brick"}), PARAMS, sets)
    # A lock of a parameter the schema lacks is `checked_fixed`'s refusal.
    check_set_params(listing(fixed={"gone": 1}), PARAMS, sets)
    offered = listing(params={"piece": {"kind": "text", "max_length": 10}})
    with refused_as("listing_param_pipeline_set", "params.piece"):
        check_set_params(offered, PARAMS, sets)
    with refused_as("listing_param_pipeline_set", "params.piece"):
        check_set_params(listing(fixed={"piece": "roof"}), PARAMS, sets)


# -- what a commit declares ------------------------------------------------------------


def template(directory: Path, pipeline: Any, schema: bytes | None = None) -> Path:
    (directory / "pipeline").mkdir(exist_ok=True)
    (directory / "pipeline" / "pipeline.py").write_text("INPUTS_VERSION = 3\n")
    if schema is not None:
        (directory / "pipeline" / "inputs.json").write_bytes(schema)
    (directory / "model.json").write_text(json.dumps({"name": "Kit", "pipeline": pipeline}))
    return directory


DECLARED = {
    "module": "pipeline/pipeline.py",
    "api": 1,
    "inputs": "pipeline/inputs.json",
    "sets_params": ["piece"],
}


def test_the_facts_are_read_as_data(tmp_path: Path) -> None:
    facts = pipeline_facts(template(tmp_path, DECLARED, json.dumps(SCHEMA).encode()), "kit")
    assert facts == PipelineFacts(3, SCHEMA, "", ("piece",))


def test_a_template_with_no_pipeline_has_no_facts(tmp_path: Path) -> None:
    assert pipeline_facts(template(tmp_path, None), "kit") is None
    assert pipeline_facts(tmp_path / "gone", "kit") is None


@pytest.mark.parametrize(
    ("pipeline", "schema"),
    [
        ({k: v for k, v in DECLARED.items() if k != "inputs"}, None),
        # A declaration the catalogue cannot read is a pipeline still.
        ({**DECLARED, "module": "../x.py"}, None),
        (DECLARED, None),
        (DECLARED, b"not json"),
        (DECLARED, b"\xff"),
        (DECLARED, b"[" * 10_000),
        (DECLARED, b" " * (MAX_SCHEMA_FILE_BYTES + 1) + json.dumps(SCHEMA).encode()),
        (DECLARED, json.dumps({**SCHEMA, "title": "x" * 17_000}).encode()),
        (DECLARED, json.dumps({"type": "array"}).encode()),
        (
            DECLARED,
            json.dumps({"type": "object", "properties": {"params": {"type": "string"}}}).encode(),
        ),
    ],
)
def test_a_shape_that_cannot_be_used_is_undeclared(
    tmp_path: Path, pipeline: Any, schema: bytes | None
) -> None:
    facts = pipeline_facts(template(tmp_path, pipeline, schema), "kit")
    assert facts is not None and facts.schema is None and facts.fault
    with refused_as("listing_inputs_undeclared", "inputs"):
        settle(listing(), facts)


@pytest.mark.parametrize("module", [None, b"INPUTS_VERSION = 3  # \xff\n"])
def test_a_pipeline_whose_module_cannot_be_read_is_undeclared(
    tmp_path: Path, module: bytes | None
) -> None:
    template(tmp_path, DECLARED, json.dumps(SCHEMA).encode())
    path = tmp_path / "pipeline" / "pipeline.py"
    if module is None:
        path.unlink()
    else:
        path.write_bytes(module)
    facts = pipeline_facts(tmp_path, "kit")
    assert facts is not None and facts.schema is None and facts.inputs_version is None
    assert "pipeline/pipeline.py" in facts.fault


def test_a_pipeline_with_no_shape_has_its_version(tmp_path: Path) -> None:
    declared = {k: v for k, v in DECLARED.items() if k != "inputs"}
    facts = pipeline_facts(template(tmp_path, declared), "kit")
    assert facts is not None and facts.schema is None and facts.inputs_version == 3


def test_a_schema_outside_the_template_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps(SCHEMA))
    directory = tmp_path / "kit"
    directory.mkdir()
    template(directory, DECLARED)
    (directory / "pipeline" / "inputs.json").symlink_to(outside)
    facts = pipeline_facts(directory, "kit")
    assert facts is not None and facts.schema is None and "outside" in facts.fault


# -- the design a listing shows (spec §7.2) -------------------------------------------------


def published(*, pipeline: bool) -> Listing:
    snapshot = ListingSnapshot(
        schema=PARAMS,
        pipeline=pipeline,
        template_name="Kit",
        libraries=[],
        snapshot_key="k",
        inputs=HOUSE if pipeline else {},
        inputs_version=1 if pipeline else 0,
    )
    return Listing.model_construct(slug="kit", commit=HEAD, snapshot=snapshot)


@pytest.mark.parametrize(
    ("inputs", "shown"),
    [
        ({**HOUSE, "params": {"width": 40}, "v": 1}, True),
        # The customer's parameters are theirs, and the order of the keys is no one's.
        ({"v": 1, "house": {"storeys": 1, "cols": 2}, "params": {}}, True),
        ({"house": {"cols": 2, "storeys": 3}, "params": {}, "v": 1}, False),
        ({**HOUSE, "params": {}, "v": 2}, False),
        ({**HOUSE, "params": {}}, False),
        ({**HOUSE, "extra": 1, "params": {}, "v": 1}, False),
        ({"params": {}, "v": 1}, False),
    ],
)
def test_a_pipelines_design_is_its_inputs_too(inputs: dict[str, Any], shown: bool) -> None:
    listing = published(pipeline=True)
    assert shows_design(listing, "kit", HEAD, inputs) is shown
    assert not shows_design(listing, "other", HEAD, inputs)
    assert not shows_design(listing, "kit", "b" * 40, inputs)


def test_a_template_with_no_pipeline_is_its_slug_and_commit() -> None:
    """Its UI's own state renders nothing, and renders share one key whatever it is."""
    listing = published(pipeline=False)
    assert shows_design(listing, "kit", HEAD, {"params": {}, "note": "mine", "v": 0})
    assert not shows_design(listing, "kit", "b" * 40, {})
