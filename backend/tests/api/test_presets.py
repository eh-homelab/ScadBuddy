"""Per-template presets: named parameter sets saved on a template, or shipped with it."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import scadbuddy.api.params as params_api
from scadbuddy.core.paths import LEGACY_PRESETS_NAME, MODEL_META_NAME, DataPaths
from scadbuddy.library.catalogue import ModelMeta
from scadbuddy.library.presets import (
    MAX_PRESET_DESCRIPTION,
    MAX_PRESET_NAME,
    MAX_PRESET_TAG,
    MAX_PRESET_TAGS,
    MAX_PRESETS,
    PRESET_LOCK_PREFIX,
    ParamPresetCreate,
    PresetStore,
    SavedPresetsUnavailableError,
)
from scadbuddy.main import sweep_assets
from scadbuddy.render.schema import CustomizerSchema, Option, Parameter

# Saved presets are rows in Postgres (#332): every test here runs on a throwaway schema.
pytestmark = pytest.mark.requires_postgres

BUILTIN = "builtin:keychain"
SOURCE = 'width = 10;\nlabel = "hi";\n'
SHIPPED = [
    {
        "id": "wide",
        "name": "Wide",
        "params": {"width": 40},
        "description": "For a wide label.",
        "tags": ["wide", "bags"],
    }
]


@pytest.fixture
def bundled(seed_dir: Path) -> Path:
    directory = seed_dir / "keychain"
    directory.mkdir()
    (directory / "model.scad").write_text(SOURCE, encoding="utf-8")
    meta = {"name": "Keychain", "presets": SHIPPED}
    (directory / MODEL_META_NAME).write_text(json.dumps(meta), encoding="utf-8")
    return directory


@pytest.fixture
def store(paths: DataPaths, pg_conninfo: str) -> Iterator[PresetStore]:
    presets = PresetStore(paths, pg_conninfo)
    presets.open()
    yield presets
    presets.close()


def _rows(conninfo: str, model_id: str) -> list[str]:
    with psycopg.connect(conninfo) as conn:
        rows = conn.execute(
            "SELECT name FROM saved_presets WHERE model_id = %s ORDER BY position", (model_id,)
        ).fetchall()
    return [row[0] for row in rows]


@pytest.fixture
def client(app: FastAPI, bundled: Path) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _define(paths: DataPaths, model_id: str, presets: Any) -> None:
    """Write ``presets`` into the template's ``model.json``, beside what is there."""
    meta_path = paths.model_meta(model_id)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["presets"] = presets
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _url(model_id: str, preset_id: str | None = None) -> str:
    base = f"/api/v1/models/{model_id}/presets"
    return base if preset_id is None else f"{base}/{preset_id}"


def _save(client: TestClient, model_id: str, name: str, params: dict[str, Any]) -> Any:
    response = client.post(_url(model_id), json={"name": name, "params": params})
    assert response.status_code == 201, response.text
    return response.json()


def test_a_template_without_presets_lists_none(client: TestClient, model: str) -> None:
    response = client.get(_url(model))
    assert response.status_code == 200
    assert response.json() == []


def test_a_saved_preset_is_listed_with_its_values(client: TestClient, model: str) -> None:
    saved = _save(client, model, "  Big   label ", {"width": 25, "label": "Ada"})
    assert saved["name"] == "Big label"
    assert saved["origin"] == "mine"
    assert saved["params"] == {"width": 25, "label": "Ada"}
    listed = client.get(_url(model)).json()
    assert [(p["id"], p["name"], p["params"]) for p in listed] == [
        (saved["id"], "Big label", {"width": 25, "label": "Ada"})
    ]


def test_presets_are_kept_outside_the_template(
    client: TestClient, model: str, paths: DataPaths, pg_conninfo: str
) -> None:
    _save(client, model, "Big", {"width": 25})
    assert _rows(pg_conninfo, model) == ["Big"]
    assert not (paths.root / "presets").exists()
    assert "presets" not in json.loads(paths.model_meta(model).read_text(encoding="utf-8"))


def test_a_preset_is_checked_as_a_render_is(client: TestClient, model: str) -> None:
    unknown = client.post(_url(model), json={"name": "X", "params": {"depth": 3}})
    assert unknown.status_code == 422
    assert unknown.json()["parameters"] == ["depth"]
    wrong_type = client.post(_url(model), json={"name": "X", "params": {"width": "wide"}})
    assert wrong_type.status_code == 422
    assert client.get(_url(model)).json() == []


def test_a_dropdown_value_has_to_be_one_of_its_options(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake openscad exports no dropdown, so the schema is given one here.
    schema = CustomizerSchema(
        parameters=[
            Parameter(
                name="style",
                type="select",
                initial="flat",
                options=[Option(name="Flat", value="flat"), Option(name="Wavy", value="wavy")],
            )
        ]
    )

    async def with_a_dropdown(*args: Any, **kwargs: Any) -> tuple[None, CustomizerSchema]:
        return None, schema

    monkeypatch.setattr(params_api, "schema_of", with_a_dropdown)
    refused = client.post(_url(model), json={"name": "X", "params": {"style": "zigzag"}})
    assert refused.status_code == 422
    assert refused.json()["parameters"] == ["style"]
    saved = _save(client, model, "Wavy", {"style": "wavy"})
    update = client.patch(_url(model, saved["id"]), json={"params": {"style": "zigzag"}})
    assert update.status_code == 422


def test_names_are_unique_per_template_ignoring_case(client: TestClient, model: str) -> None:
    _save(client, model, "Big", {"width": 25})
    clash = client.post(_url(model), json={"name": "big", "params": {}})
    assert clash.status_code == 409
    assert clash.json()["name"] == "big"


def test_a_blank_or_long_name_is_refused(client: TestClient, model: str) -> None:
    assert client.post(_url(model), json={"name": "   ", "params": {}}).status_code == 422
    too_long = "x" * (MAX_PRESET_NAME + 1)
    assert client.post(_url(model), json={"name": too_long, "params": {}}).status_code == 422


def test_a_preset_can_be_renamed_and_its_values_replaced(client: TestClient, model: str) -> None:
    saved = _save(client, model, "Big", {"width": 25, "label": "Ada"})
    response = client.patch(_url(model, saved["id"]), json={"params": {"width": 30}})
    assert response.status_code == 200, response.text
    assert response.json()["params"] == {"width": 30}
    assert response.json()["name"] == "Big"
    renamed = client.patch(_url(model, saved["id"]), json={"name": "Bigger"})
    assert renamed.json()["name"] == "Bigger"
    assert renamed.json()["params"] == {"width": 30}


def test_a_rename_onto_another_preset_is_refused(client: TestClient, model: str) -> None:
    _save(client, model, "Big", {"width": 25})
    small = _save(client, model, "Small", {"width": 5})
    response = client.patch(_url(model, small["id"]), json={"name": "BIG"})
    assert response.status_code == 409


def test_an_update_is_checked_too(client: TestClient, model: str) -> None:
    saved = _save(client, model, "Big", {"width": 25})
    response = client.patch(_url(model, saved["id"]), json={"params": {"nope": 1}})
    assert response.status_code == 422
    assert client.get(_url(model)).json()[0]["params"] == {"width": 25}


def test_a_preset_can_be_deleted(client: TestClient, model: str, pg_conninfo: str) -> None:
    saved = _save(client, model, "Big", {"width": 25})
    assert client.delete(_url(model, saved["id"])).status_code == 204
    assert client.get(_url(model)).json() == []
    assert _rows(pg_conninfo, model) == []
    assert client.delete(_url(model, saved["id"])).status_code == 404


def test_an_unknown_preset_is_a_404(client: TestClient, model: str) -> None:
    missing = "0" * 32
    assert client.patch(_url(model, missing), json={"name": "X"}).status_code == 404


def test_an_unknown_model_is_a_404(client: TestClient) -> None:
    assert client.get(_url("nope")).status_code == 404
    assert client.post(_url("nope"), json={"name": "X", "params": {}}).status_code == 404


@pytest.mark.requires_git
def test_a_built_in_lists_its_shipped_presets_first(client: TestClient) -> None:
    saved = _save(client, BUILTIN, "Mine", {"label": "Bo"})
    listed = client.get(_url(BUILTIN)).json()
    assert [(p["name"], p["origin"], p["params"]) for p in listed] == [
        ("Wide", "template", {"width": 40}),
        ("Mine", "mine", {"label": "Bo"}),
    ]
    assert listed[1]["id"] == saved["id"]


@pytest.mark.requires_git
def test_shipped_presets_are_read_only(client: TestClient) -> None:
    shipped = client.get(_url(BUILTIN)).json()[0]
    patch = client.patch(_url(BUILTIN, shipped["id"]), json={"name": "Other"})
    assert patch.status_code == 403
    assert client.delete(_url(BUILTIN, shipped["id"])).status_code == 403
    # Nor can a saved one take its name.
    assert client.post(_url(BUILTIN), json={"name": "wide", "params": {}}).status_code == 409


@pytest.mark.requires_git
def test_saving_a_preset_does_not_move_the_template_revision(client: TestClient) -> None:
    before = client.get(f"/api/v1/models/{BUILTIN}").json()["version"]
    _save(client, BUILTIN, "Mine", {"label": "Bo"})
    assert client.get(f"/api/v1/models/{BUILTIN}").json()["version"] == before


@pytest.mark.parametrize(
    "broken",
    [
        {"not": "a list"},
        [{"params": {"width": 1}}],
        [{"name": "Twice"}, {"name": "twice"}],
        [{"id": "same", "name": "One"}, {"id": "same", "name": "Two"}],
        [{"id": "Not A Slug", "name": "One"}],
    ],
)
def test_a_broken_preset_list_costs_only_the_template_presets(
    client: TestClient, model: str, paths: DataPaths, broken: Any
) -> None:
    _define(paths, model, broken)
    saved = _save(client, model, "Big", {"width": 25})
    assert [p["id"] for p in client.get(_url(model)).json()] == [saved["id"]]
    # Never the model: model.json's own fields are read as they always were.
    assert client.get(f"/api/v1/models/{model}").json()["name"] == "Demo"


@pytest.mark.requires_git
def test_a_duplicate_takes_the_saved_presets_along(client: TestClient) -> None:
    saved = _save(client, BUILTIN, "Mine", {"label": "Bo"})
    created = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "My keychain"})
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    listed = client.get(_url(slug)).json()
    assert [(p["name"], p["origin"]) for p in listed] == [
        ("Wide", "template"),
        ("Mine", "mine"),
    ]
    # Copies, not the same presets: editing one leaves the other alone.
    assert listed[1]["id"] != saved["id"]
    client.patch(_url(slug, listed[1]["id"]), json={"name": "Renamed"})
    assert client.get(_url(BUILTIN)).json()[1]["name"] == "Mine"


def test_deleting_a_model_takes_its_presets(
    client: TestClient, model: str, pg_conninfo: str
) -> None:
    _save(client, model, "Big", {"width": 25})
    assert client.delete(f"/api/v1/models/{model}").status_code == 204
    assert _rows(pg_conninfo, model) == []


def test_the_orphan_sweep_forgets_a_gone_template_s_presets(
    store: PresetStore, pg_conninfo: str
) -> None:
    # A delete whose own cleanup failed: rows left for a template that is gone.
    store.create("gone", ParamPresetCreate(name="Big", params={}))
    store.create("kept", ParamPresetCreate(name="Big", params={}))
    assert store.sweep_orphans(lambda slug: slug == "kept") == ["gone"]
    assert _rows(pg_conninfo, "gone") == []
    assert _rows(pg_conninfo, "kept") == ["Big"]


def test_an_upload_a_saved_preset_names_is_kept_by_the_sweep(
    client: TestClient, app: FastAPI, model: str, pg_conninfo: str
) -> None:
    state = app.state.scadbuddy
    kept = state.assets.put(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "kept.svg")
    swept = state.assets.put(b"<svg xmlns='http://www.w3.org/2000/svg' width='2'/>", "gone.svg")
    _save(client, model, "Logo", {"label": kept.id})
    # Last used long ago: the upload store's rows (#591).
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE assets SET last_used_at = now() - make_interval(secs => %s)",
            (10 * state.config.asset_sweep_grace,),
        )
    assert sweep_assets(state) == [swept.id]


def _duplicate(client: TestClient, model_id: str, preset_id: str, name: str) -> Any:
    return client.post(f"{_url(model_id, preset_id)}/duplicate", json={"name": name})


@pytest.mark.requires_git
def test_a_shipped_preset_is_duplicated_to_an_editable_one(client: TestClient) -> None:
    shipped = client.get(_url(BUILTIN)).json()[0]
    response = _duplicate(client, BUILTIN, shipped["id"], "  Wide   copy ")
    assert response.status_code == 201, response.text
    copy = response.json()
    assert copy["name"] == "Wide copy"
    assert copy["origin"] == "mine"
    assert copy["params"] == shipped["params"]
    assert copy["id"] != shipped["id"]
    # The copy is the one to change; the shipped one stays as it ships.
    renamed = client.patch(_url(BUILTIN, copy["id"]), json={"params": {"width": 12}})
    assert renamed.status_code == 200
    assert client.get(_url(BUILTIN)).json()[0]["params"] == {"width": 40}


def test_a_saved_preset_is_duplicated_with_its_values(client: TestClient, model: str) -> None:
    saved = _save(client, model, "Big", {"width": 25, "label": "Ada"})
    copy = _duplicate(client, model, saved["id"], "Big for Bo").json()
    assert copy["params"] == {"width": 25, "label": "Ada"}
    assert [p["name"] for p in client.get(_url(model)).json()] == ["Big", "Big for Bo"]


def test_a_duplicate_needs_a_free_name(client: TestClient, model: str) -> None:
    saved = _save(client, model, "Big", {"width": 25})
    assert _duplicate(client, model, saved["id"], "big").status_code == 409
    assert _duplicate(client, model, saved["id"], "   ").status_code == 422


def test_duplicating_an_unknown_preset_is_a_404(client: TestClient, model: str) -> None:
    assert _duplicate(client, model, "0" * 32, "Copy").status_code == 404
    assert _duplicate(client, model, "template-9", "Copy").status_code == 404


def test_a_shipped_preset_the_template_outgrew_is_refused(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    _define(paths, model, [{"id": "old", "name": "Old", "params": {"depth": 3}}])
    response = _duplicate(client, model, "template-old", "Old copy")
    assert response.status_code == 422
    assert response.json()["parameters"] == ["depth"]


def _ids(client: TestClient, model_id: str) -> list[str]:
    return [p["id"] for p in client.get(_url(model_id)).json() if p["origin"] == "template"]


def test_a_template_preset_keeps_its_id_when_the_list_is_reordered(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    first = {"id": "narrow", "name": "Narrow", "params": {"width": 5}}
    second = {"id": "wide", "name": "Wide", "params": {"width": 40}}
    _define(paths, model, [first, second])
    assert _ids(client, model) == ["template-narrow", "template-wide"]
    _define(paths, model, [second, {**first, "name": "Thin"}])
    assert _ids(client, model) == ["template-wide", "template-narrow"]


def test_a_template_preset_without_an_id_is_keyed_by_its_name(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    _define(
        paths,
        model,
        [
            {"name": "Bag tag", "params": {}},
            {"id": "bag-tag-2", "name": "Other", "params": {}},
            {"name": "Bag  Tag!", "params": {}},
            {"name": "🎄", "params": {}},
        ],
    )
    assert _ids(client, model) == [
        "template-bag-tag",
        "template-bag-tag-2",
        "template-bag-tag-3",
        "template-preset-4",
    ]


def test_a_legacy_presets_file_is_still_read_below_model_json(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    _define(paths, model, [{"id": "wide", "name": "Wide", "params": {"width": 40}}])
    legacy = {
        "presets": [
            {"name": "Wide", "params": {"width": 99}},
            {"name": "Narrow", "params": {"width": 5}},
        ]
    }
    (paths.model_dir(model) / LEGACY_PRESETS_NAME).write_text(json.dumps(legacy), "utf-8")
    listed = [(p["id"], p["params"]) for p in client.get(_url(model)).json()]
    assert listed == [("template-wide", {"width": 40}), ("template-narrow", {"width": 5})]


def _patch_presets(client: TestClient, model_id: str, presets: Any) -> Any:
    return client.patch(f"/api/v1/models/{model_id}", json={"presets": presets})


def test_a_template_of_mine_edits_its_presets_through_its_metadata(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    response = _patch_presets(
        client,
        model,
        [
            {"name": "Big label", "params": {"width": 25, "label": "Ada"}, "tags": ["big"]},
            {"id": "tiny", "name": "Tiny", "params": {"width": 2}},
        ],
    )
    assert response.status_code == 200, response.text
    assert _ids(client, model) == ["template-big-label", "template-tiny"]
    # Every key is written down, so a rename later keeps the preset's id.
    written = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))["presets"]
    assert [p["id"] for p in written] == ["big-label", "tiny"]
    assert written[0]["tags"] == ["big"]
    renamed = [{**written[0], "name": "Huge label"}, written[1]]
    assert _patch_presets(client, model, renamed).status_code == 200
    assert _ids(client, model) == ["template-big-label", "template-tiny"]
    # They are the template's own now: read-only through the preset routes.
    assert client.delete(_url(model, "template-tiny")).status_code == 403


def test_a_template_preset_edit_is_checked(client: TestClient, model: str) -> None:
    unknown = _patch_presets(client, model, [{"name": "X", "params": {"depth": 1}}])
    assert unknown.status_code == 422
    assert unknown.json()["parameters"] == ["depth"]
    twice = _patch_presets(client, model, [{"name": "X"}, {"name": "x"}])
    assert twice.status_code == 422
    same_id = [{"id": "same", "name": "One"}, {"id": "same", "name": "Two"}]
    assert _patch_presets(client, model, same_id).status_code == 422
    too_many = [{"name": f"Preset {n}"} for n in range(MAX_PRESETS + 1)]
    assert _patch_presets(client, model, too_many).status_code == 422
    assert _ids(client, model) == []


def test_a_template_preset_edit_replaces_a_legacy_file_too(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    legacy = {"presets": [{"name": "Wide", "params": {"width": 40}}, {"name": "Narrow"}]}
    (paths.model_dir(model) / LEGACY_PRESETS_NAME).write_text(json.dumps(legacy), "utf-8")
    assert _ids(client, model) == ["template-wide", "template-narrow"]
    # Keeping one of what was read keeps it, under the same id; the other goes.
    kept = [{"id": "wide", "name": "Wide", "params": {"width": 40}}]
    assert _patch_presets(client, model, kept).status_code == 200
    assert _ids(client, model) == ["template-wide"]
    assert not (paths.model_dir(model) / LEGACY_PRESETS_NAME).exists()
    # And an empty list leaves none at all.
    assert _patch_presets(client, model, []).status_code == 200
    assert _ids(client, model) == []


def test_a_template_preset_cannot_take_a_saved_one_s_name(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    assert client.post(_url(model), json={"name": "Mum", "params": {}}).status_code == 201
    response = _patch_presets(client, model, [{"name": "mum", "params": {}}])
    assert response.status_code == 409, response.text
    assert response.json()["name"] == "mum"
    assert "presets" not in json.loads(paths.model_meta(model).read_text(encoding="utf-8"))


def test_a_template_s_list_is_checked_and_written_under_the_lock_a_save_takes(
    store: PresetStore, pg_conninfo: str
) -> None:
    held: list[tuple[bool, bool]] = []

    def write() -> None:
        # From another session: this template's lock is taken, another template's free.
        with psycopg.connect(pg_conninfo, autocommit=True) as conn:
            mine, other = (
                conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                    (f"{PRESET_LOCK_PREFIX}{model_id}",),
                ).fetchone()
                for model_id in ("m", "n")
            )
            assert mine is not None and other is not None
            held.append((mine[0], other[0]))

    # Held from the check to the write, so a save cannot land between them.
    assert store.with_names_free("m", ["A"], write) is None
    assert held == [(False, True)]


def test_a_store_without_a_database_reads_only_the_template_s_own(
    paths: DataPaths,
) -> None:
    store = PresetStore(paths)
    assert store.saved_presets("m") == []
    assert store.with_names_free("m", ["A"], lambda: "written") == "written"
    with pytest.raises(SavedPresetsUnavailableError):
        store.create("m", ParamPresetCreate(name="A", params={}))


@pytest.mark.requires_git
def test_a_built_in_s_presets_cannot_be_edited(client: TestClient) -> None:
    assert _patch_presets(client, BUILTIN, []).status_code == 403


def test_a_legacy_preset_never_takes_a_key_model_json_had_to_disambiguate(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    # Two model.json names slug alike, so the second is keyed `foo-2`. The legacy file
    # claims `foo-2` explicitly, and has "Foo 2", which within the legacy file alone is
    # keyed `foo-2-2` (its own `foo-2` being taken there).
    _define(paths, model, [{"name": "Foo"}, {"name": "Foo!"}])
    legacy = {"presets": [{"name": "Foo 2"}, {"id": "foo-2", "name": "Other"}, {"name": "Bar"}]}
    (paths.model_dir(model) / LEGACY_PRESETS_NAME).write_text(json.dumps(legacy), "utf-8")
    listed = [(p["id"], p["name"]) for p in client.get(_url(model)).json()]
    # model.json's keys hold; the legacy entry claiming `foo-2` is dropped; the rest keep
    # keys of their own; no two ids are the same.
    assert listed == [
        ("template-foo", "Foo"),
        ("template-foo-2", "Foo!"),
        ("template-foo-2-2", "Foo 2"),
        ("template-bar", "Bar"),
    ]


def test_model_metadata_ignores_the_presets_key() -> None:
    """Presets live in model.json but not in ModelMeta: it must keep ignoring the key,
    or every model that has presets would stop reading as metadata."""
    meta = ModelMeta.model_validate({"name": "X", "presets": [{"name": "Y"}]})
    assert "presets" not in meta.model_dump()


def test_a_model_json_that_is_not_json_costs_only_the_template_presets(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    saved = _save(client, model, "Big", {"width": 25})
    paths.model_meta(model).write_text("{not json", encoding="utf-8")
    assert [p["id"] for p in client.get(_url(model)).json()] == [saved["id"]]


def test_a_template_preset_description_and_tags_are_bounded(client: TestClient, model: str) -> None:
    long_description = [{"name": "X", "description": "d" * (MAX_PRESET_DESCRIPTION + 1)}]
    assert _patch_presets(client, model, long_description).status_code == 422
    many_tags = [{"name": "X", "tags": [f"t{n}" for n in range(MAX_PRESET_TAGS + 1)]}]
    assert _patch_presets(client, model, many_tags).status_code == 422
    long_tag = [{"name": "X", "tags": ["t" * (MAX_PRESET_TAG + 1)]}]
    assert _patch_presets(client, model, long_tag).status_code == 422
    comma_tag = [{"name": "X", "tags": ["M3, M4"]}]
    assert _patch_presets(client, model, comma_tag).status_code == 422
    fine = [{"name": "X", "description": "d" * MAX_PRESET_DESCRIPTION, "tags": ["a", "b"]}]
    assert _patch_presets(client, model, fine).status_code == 200


def test_a_template_tag_written_with_a_comma_still_loads(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Until #327 a model.json tag could hold a comma. Such a file keeps loading, the
    tag read as the tags it lists (cleaned, and the list cut at the bound), rather than
    costing the template every preset it defines. A save still refuses one (422)."""
    full = [f"t{n}" for n in range(MAX_PRESET_TAGS - 1)]
    _define(
        paths,
        model,
        [
            {"name": "Bits", "tags": ["M3, M4", "m3", "x"], "description": "Hex bits"},
            {"name": "Full", "tags": [*full, "a , b"]},
            {"name": "Plain", "tags": ["a"]},
        ],
    )
    listed = client.get(_url(model)).json()
    assert [(p["name"], p["tags"]) for p in listed] == [
        ("Bits", ["M3", "M4", "x"]),
        ("Full", [*full, "a"]),
        ("Plain", ["a"]),
    ]
    assert listed[0]["description"] == "Hex bits"
    assert _patch_presets(client, model, [{"name": "X", "tags": ["M3, M4"]}]).status_code == 422


def test_a_template_preset_with_null_details_still_loads(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """A model.json written by hand, or by a tool that writes every key, says ``null``
    for a description or tags a preset has none of. That reads as none, rather than
    failing the whole list and costing the template every preset it defines. A save's
    body still refuses a null description (422)."""
    _define(
        paths,
        model,
        [
            {"name": "Bare", "description": None, "tags": None},
            {"name": "Plain", "tags": ["a"], "description": "Plain."},
        ],
    )
    listed = client.get(_url(model)).json()
    assert [(p["name"], p["description"], p["tags"]) for p in listed] == [
        ("Bare", "", []),
        ("Plain", "Plain.", ["a"]),
    ]
    null_description = client.post(_url(model), json={"name": "X", "description": None})
    assert null_description.status_code == 422


def test_a_saved_preset_carries_a_description_and_tags(client: TestClient, model: str) -> None:
    response = client.post(
        _url(model),
        json={
            "name": "Big",
            "params": {"width": 25},
            "description": "  For **bags**.  ",
            "tags": [" big ", "Big", "", "kids  size", "big"],
        },
    )
    assert response.status_code == 201, response.text
    saved = response.json()
    # Trimmed, blanks dropped, each tag kept once ignoring case, in the order given.
    assert saved["description"] == "For **bags**."
    assert saved["tags"] == ["big", "kids size"]
    listed = client.get(_url(model)).json()
    assert [(p["description"], p["tags"]) for p in listed] == [
        ("For **bags**.", ["big", "kids size"])
    ]
    # Without them, a preset has none.
    plain = _save(client, model, "Plain", {})
    assert (plain["description"], plain["tags"]) == ("", [])


def test_a_saved_preset_s_details_are_edited_and_cleared(client: TestClient, model: str) -> None:
    saved = _save(client, model, "Big", {"width": 25})
    url = _url(model, saved["id"])
    edited = client.patch(url, json={"description": "Wide", "tags": ["a", "b"]})
    assert edited.status_code == 200, edited.text
    assert (edited.json()["description"], edited.json()["tags"]) == ("Wide", ["a", "b"])
    # A field left out stays as it was.
    renamed = client.patch(url, json={"name": "Bigger"}).json()
    assert (renamed["name"], renamed["description"], renamed["tags"]) == (
        "Bigger",
        "Wide",
        ["a", "b"],
    )
    assert renamed["params"] == {"width": 25}
    # An empty one clears it.
    cleared = client.patch(url, json={"description": "", "tags": []}).json()
    assert (cleared["description"], cleared["tags"]) == ("", [])


def test_a_saved_preset_s_details_are_bounded(client: TestClient, model: str) -> None:
    def refused(body: dict[str, Any]) -> bool:
        status: int = client.post(_url(model), json={"name": "X", **body}).status_code
        return status == 422

    assert refused({"description": "d" * (MAX_PRESET_DESCRIPTION + 1)})
    assert refused({"tags": [f"t{n}" for n in range(MAX_PRESET_TAGS + 1)]})
    assert refused({"tags": ["t" * (MAX_PRESET_TAG + 1)]})
    # A comma would split the tag in two when the UI edits tags as one line.
    assert refused({"tags": ["M3, M4"]})
    # The length is counted in code points, not UTF-16 units.
    assert not refused({"tags": ["\U0001f600" * MAX_PRESET_TAG]})
    # Repeats count once: they are dropped before the bound is checked.
    repeated = [f"t{n % MAX_PRESET_TAGS}" for n in range(MAX_PRESET_TAGS * 2)]
    assert not refused({"tags": repeated, "description": "d" * MAX_PRESET_DESCRIPTION})
    saved = client.get(_url(model)).json()[0]
    too_long = client.patch(_url(model, saved["id"]), json={"tags": ["t" * (MAX_PRESET_TAG + 1)]})
    assert too_long.status_code == 422
    comma = client.patch(_url(model, saved["id"]), json={"tags": ["a,b"]})
    assert comma.status_code == 422


@pytest.mark.requires_git
def test_a_shipped_preset_s_details_come_from_model_json_and_survive_a_duplicate(
    client: TestClient,
) -> None:
    shipped = client.get(_url(BUILTIN)).json()[0]
    assert (shipped["description"], shipped["tags"]) == ("For a wide label.", ["wide", "bags"])
    copy = _duplicate(client, BUILTIN, shipped["id"], "Wide copy")
    assert copy.status_code == 201, copy.text
    assert (copy.json()["description"], copy.json()["tags"]) == (
        "For a wide label.",
        ["wide", "bags"],
    )


@pytest.mark.requires_git
def test_a_template_duplicate_copies_its_saved_presets_details(client: TestClient) -> None:
    client.post(
        _url(BUILTIN),
        json={"name": "Mine", "params": {}, "description": "Mine", "tags": ["x"]},
    )
    created = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "My keychain"})
    assert created.status_code == 201, created.text
    copied = client.get(_url(created.json()["slug"])).json()[1]
    assert (copied["name"], copied["description"], copied["tags"]) == ("Mine", "Mine", ["x"])


@pytest.mark.requires_git
def test_a_malformed_preset_list_is_refused_before_the_route_runs(
    client: TestClient, model: str
) -> None:
    """The list's shape is checked parsing the body, so it is a 422 even for a model
    that is not there or a built-in; its values are checked in the route, after them."""
    twice = [{"name": "X"}, {"name": "x"}]
    assert _patch_presets(client, "no-such-model", twice).status_code == 422
    assert _patch_presets(client, BUILTIN, twice).status_code == 422
    unknown = [{"name": "X", "params": {"nope": 1}}]
    assert _patch_presets(client, "no-such-model", unknown).status_code == 404
    assert _patch_presets(client, BUILTIN, unknown).status_code == 403


def test_a_template_preset_file_value_is_checked_as_a_saved_one(
    client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `file` value in a template's own preset has to name an upload or a sample,
    as a saved preset's does (#204): the same check, not a looser one."""
    schema = CustomizerSchema(
        parameters=[Parameter(name="overlay", type="file", initial="", accept=["svg"])]
    )
    source = SimpleNamespace(scad=paths.model_source(model))

    async def with_a_file(*args: Any, **kwargs: Any) -> tuple[Any, CustomizerSchema]:
        return source, schema

    monkeypatch.setattr(params_api, "schema_of", with_a_file)
    bogus = [{"name": "X", "params": {"overlay": "f" * 32}}]
    assert _patch_presets(client, model, bogus).status_code == 422
    assert (
        client.post(_url(model), json={"name": "X", "params": {"overlay": "f" * 32}}).status_code
        == 422
    )
    empty = [{"name": "X", "params": {"overlay": ""}}]
    assert _patch_presets(client, model, empty).status_code == 200


def test_a_template_write_does_not_hold_up_a_save_on_another(
    store: PresetStore, paths: DataPaths
) -> None:
    """``with_names_free`` holds its lock across a git commit; that lock is the
    template's, so a save on some other template goes ahead meanwhile."""
    for slug in ("a", "b"):
        paths.model_dir(slug).mkdir(parents=True)
        (paths.model_dir(slug) / MODEL_META_NAME).write_text("{}")
    committing, saved = threading.Event(), threading.Event()

    def slow_write() -> None:
        committing.set()
        assert saved.wait(timeout=5), "the save on 'b' waited for 'a'"

    writer = threading.Thread(target=store.with_names_free, args=("a", ["X"], slow_write))
    writer.start()
    assert committing.wait(timeout=5)
    store.create("b", ParamPresetCreate(name="X", params={}))
    saved.set()
    writer.join(timeout=5)
    assert [preset.name for preset in store.saved_presets("b")] == ["X"]


def test_a_preset_saves_inputs_and_reads_them_back(client: TestClient, model: str) -> None:
    body = {"name": "Lid", "inputs": {"params": {"width": 12}, "ui": {"tab": "lid"}}}
    created = client.post(f"/api/v1/models/{model}/presets", json=body)
    assert created.status_code == 201
    preset = created.json()
    assert preset["params"] == {"width": 12}
    assert preset["inputs"] == {"params": {"width": 12}, "ui": {"tab": "lid"}, "v": 0}
    listed = client.get(f"/api/v1/models/{model}/presets").json()
    assert [p["inputs"] for p in listed if p["origin"] == "mine"] == [preset["inputs"]]


def test_a_params_only_save_reads_as_version_zero_inputs(client: TestClient, model: str) -> None:
    created = client.post(
        f"/api/v1/models/{model}/presets", json={"name": "Wide", "params": {"width": 20}}
    ).json()
    assert created["inputs"] == {"params": {"width": 20}, "v": 0}


def test_a_params_only_update_keeps_the_ui_state(client: TestClient, model: str) -> None:
    body = {"name": "Lid", "inputs": {"params": {"width": 12}, "ui": {"tab": "lid"}}}
    preset = client.post(f"/api/v1/models/{model}/presets", json=body).json()
    updated = client.patch(
        f"/api/v1/models/{model}/presets/{preset['id']}", json={"params": {"width": 14}}
    ).json()
    assert updated["inputs"] == {"params": {"width": 14}, "ui": {"tab": "lid"}, "v": 0}


def test_preset_inputs_are_checked_as_a_render_is(client: TestClient, model: str) -> None:
    bad = {"name": "Bad", "inputs": {"params": {"nope": 1}}}
    assert client.post(f"/api/v1/models/{model}/presets", json=bad).status_code == 422
    clash = {"name": "Clash", "params": {"width": 1}, "inputs": {"params": {"width": 2}}}
    assert client.post(f"/api/v1/models/{model}/presets", json=clash).status_code == 422


def test_template_presets_carry_inputs_or_read_as_v0(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    meta["presets"] = [
        {"name": "Plain", "params": {"width": 5}},
        {"name": "Designed", "inputs": {"params": {"width": 6}, "ui": {"tab": "b"}, "v": 2}},
    ]
    paths.model_meta(model).write_text(json.dumps(meta), encoding="utf-8")
    listed = client.get(f"/api/v1/models/{model}/presets").json()
    by_name = {p["name"]: p for p in listed}
    assert by_name["Plain"]["inputs"] == {"params": {"width": 5}, "v": 0}
    assert by_name["Designed"]["inputs"] == {"params": {"width": 6}, "ui": {"tab": "b"}, "v": 2}
    assert by_name["Designed"]["params"] == {"width": 6}


# ── fix round 1: `params` is authoritative for `inputs["params"]` ────────────


def _sql(conninfo: str, statement: str, args: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(statement.encode(), args)


def _stored(conninfo: str, preset_id: str) -> tuple[Any, Any]:
    with psycopg.connect(conninfo) as conn:
        row = conn.execute(
            "SELECT params, inputs FROM saved_presets WHERE id = %s", (preset_id,)
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def _lid(client: TestClient, model_id: str) -> Any:
    body = {"name": "Lid", "inputs": {"params": {"width": 12}, "ui": {"tab": "lid"}}}
    created = client.post(_url(model_id), json=body)
    assert created.status_code == 201, created.text
    return created.json()


def test_a_preset_whose_params_an_older_release_changed_reads_them_in_its_inputs(
    client: TestClient, model: str, pg_conninfo: str
) -> None:
    """A rollback's `update` writes `params` only; back on this release they win."""
    lid = _lid(client, model)
    _sql(
        pg_conninfo,
        "UPDATE saved_presets SET params = %s WHERE id = %s",
        (json.dumps({"width": 30}), lid["id"]),
    )
    listed = {p["id"]: p for p in client.get(_url(model)).json()}
    assert listed[lid["id"]]["inputs"] == {"params": {"width": 30}, "ui": {"tab": "lid"}, "v": 0}
    renamed = client.patch(_url(model, lid["id"]), json={"name": "Lid 2"}).json()
    assert renamed["params"] == {"width": 30}
    assert renamed["inputs"] == {"params": {"width": 30}, "ui": {"tab": "lid"}, "v": 0}
    assert _stored(pg_conninfo, lid["id"])[0] == {"width": 30}
    copy = _duplicate(client, model, lid["id"], "Lid copy").json()
    assert copy["params"] == {"width": 30}
    assert copy["inputs"] == {"params": {"width": 30}, "ui": {"tab": "lid"}, "v": 0}


def test_an_update_with_inputs_replaces_them(client: TestClient, model: str) -> None:
    lid = _lid(client, model)
    body = {"inputs": {"params": {"width": 9}, "ui": {"tab": "base"}}}
    updated = client.patch(_url(model, lid["id"]), json=body)
    assert updated.status_code == 200, updated.text
    assert updated.json()["params"] == {"width": 9}
    assert updated.json()["inputs"] == {"params": {"width": 9}, "ui": {"tab": "base"}, "v": 0}


def test_a_params_only_update_is_checked_as_inputs(client: TestClient, model: str) -> None:
    """Merged into the stored inputs, the values go through the same checks and cap."""
    body = {"name": "Big", "inputs": {"params": {"width": 1}, "ui": {"blob": "x" * 40000}}}
    big = client.post(_url(model), json=body).json()
    refused = client.patch(_url(model, big["id"]), json={"params": {"label": "y" * 30000}})
    assert refused.status_code == 422, refused.text
    assert "at most 65536" in refused.json()["detail"]


def test_a_row_saved_before_inputs_reads_as_version_zero(
    client: TestClient, model: str, pg_conninfo: str
) -> None:
    saved = _save(client, model, "Old", {"width": 7})
    _sql(pg_conninfo, "UPDATE saved_presets SET inputs = '{}'::jsonb WHERE id = %s", (saved["id"],))
    listed = {p["id"]: p for p in client.get(_url(model)).json()}
    assert listed[saved["id"]]["inputs"] == {"params": {"width": 7}, "v": 0}


def test_the_migration_backfills_inputs_from_params(
    client: TestClient, model: str, pg_conninfo: str
) -> None:
    """The migration's own backfill, run on a row as the column default leaves it."""
    saved = _save(client, model, "Old", {"width": 7})
    _sql(pg_conninfo, "UPDATE saved_presets SET inputs = '{}'::jsonb WHERE id = %s", (saved["id"],))
    migration = (
        Path(__file__).parents[2] / "scadbuddy/migrations/20260929T0311Z_saved_presets_inputs.sql"
    )
    [backfill] = [
        line
        for line in migration.read_text(encoding="utf-8").splitlines()
        if line.startswith("UPDATE saved_presets")
    ]
    _sql(pg_conninfo, backfill)
    assert _stored(pg_conninfo, saved["id"])[1] == {"params": {"width": 7}, "v": 0}


def test_a_duplicated_template_takes_its_presets_inputs_along(client: TestClient) -> None:
    body = {"name": "Mine", "inputs": {"params": {"label": "Bo"}, "ui": {"tab": "text"}}}
    assert client.post(_url(BUILTIN), json=body).status_code == 201
    created = client.post(f"/api/v1/models/{BUILTIN}/duplicate", json={"name": "My keychain"})
    assert created.status_code == 201, created.text
    [mine] = [p for p in client.get(_url(created.json()["slug"])).json() if p["origin"] == "mine"]
    assert mine["inputs"] == {"params": {"label": "Bo"}, "ui": {"tab": "text"}, "v": 0}


def test_model_json_keeps_inputs_only_beside_ui_state(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    presets = [
        {"name": "Plain", "params": {"width": 5}},
        {"name": "Designed", "inputs": {"params": {"width": 6}, "ui": {"tab": "b"}}},
    ]
    assert _patch_presets(client, model, presets).status_code == 200
    written = {
        p["name"]: p
        for p in json.loads(paths.model_meta(model).read_text(encoding="utf-8"))["presets"]
    }
    assert "inputs" not in written["Plain"]
    assert written["Plain"]["params"] == {"width": 5}
    assert written["Designed"]["params"] == {"width": 6}
    assert written["Designed"]["inputs"]["ui"] == {"tab": "b"}


def test_a_hand_edited_template_preset_keeps_the_list(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Editing `params` in the committed file wins over the `inputs` beside it, and
    never costs the template its other presets."""
    presets = [
        {"name": "Plain", "params": {"width": 5}},
        {"name": "Designed", "inputs": {"params": {"width": 6}, "ui": {"tab": "b"}}},
    ]
    assert _patch_presets(client, model, presets).status_code == 200
    meta_path = paths.model_meta(model)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for preset in meta["presets"]:
        if preset["name"] == "Designed":
            preset["params"] = {"width": 99}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    listed = {p["name"]: p for p in client.get(_url(model)).json()}
    assert set(listed) == {"Plain", "Designed"}
    assert listed["Designed"]["params"] == {"width": 99}
    assert listed["Designed"]["inputs"] == {"params": {"width": 99}, "ui": {"tab": "b"}, "v": 0}


# ── fix round 2: request bodies stay strict; model.json keeps `v` ────────────


def test_a_metadata_patch_whose_preset_disagrees_is_refused(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Only a stored file is read leniently: a client that sends both and changed one
    is told so, rather than having its `inputs.params` edit dropped."""
    clash = {"name": "A", "params": {"width": 1}, "inputs": {"params": {"width": 2}, "ui": {}}}
    refused = _patch_presets(client, model, [clash])
    assert refused.status_code == 422, refused.text
    assert "disagree" in refused.text
    # The same disagreement in the committed file still loads, with `params`'s values.
    _define(paths, model, [clash])
    [listed] = [p for p in client.get(_url(model)).json() if p["origin"] == "template"]
    assert listed["params"] == {"width": 1}
    assert listed["inputs"] == {"params": {"width": 1}, "ui": {}, "v": 0}


def test_an_inputs_version_survives_model_json(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    versioned = {"name": "V", "inputs": {"params": {"width": 3}, "v": 2}}
    assert _patch_presets(client, model, [versioned]).status_code == 200
    [written] = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))["presets"]
    assert written["inputs"] == {"params": {"width": 3}, "v": 2}
    [listed] = [p for p in client.get(_url(model)).json() if p["origin"] == "template"]
    assert listed["inputs"] == {"params": {"width": 3}, "v": 2}
