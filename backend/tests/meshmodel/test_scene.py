"""The scene model (spec 2026-10-11 §3.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.meshmodel.scene import (
    MAX_NAME,
    Cut,
    Exposed,
    Joint,
    Part,
    Scene,
    SceneObject,
    SceneSource,
    Transform,
    dump_scene,
    exposed_values,
    load_scene,
    named,
    next_object_id,
)

SOURCE = SceneSource(kind="upload", name="carrot.3mf", sha256="ab" * 32)


def obj(oid: str = "o1", **update: object) -> SceneObject:
    base = SceneObject(
        id=oid, name="Carrot", parts=[Part(id="p1", mesh=f"meshes/{oid}-p1.3mf", colour="#F08A24")]
    )
    return base.model_copy(update=update)


def test_a_scene_round_trips_through_json() -> None:
    scene = Scene(
        source=SOURCE,
        objects=[
            obj(
                ops=[
                    Cut(
                        point=(0, 0, Exposed(expose="cut_height", value=32)),
                        normal=(0, 0, 1),
                        keep="below",
                    )
                ]
            )
        ],
    )
    assert load_scene(dump_scene(scene)) == scene


def test_exposed_values_are_collected_once_in_order() -> None:
    cut = Cut(point=(0, 0, Exposed(expose="cut_height", value=32)), normal=(0, 0, 1), keep="below")
    scene = Scene(
        source=SOURCE,
        objects=[
            obj("o1", ops=[cut]),
            obj(
                "o2",
                ops=[cut.model_copy(update={"keep": "above"})],
                transform=Transform(scale=(Exposed(expose="size", value=1.5), 1, 1)),
            ),
        ],
    )
    assert exposed_values(scene) == {"cut_height": 32.0, "size": 1.5}


def test_one_name_with_two_values_is_refused() -> None:
    with pytest.raises(ValidationError, match="cut_height"):
        Scene(
            source=SOURCE,
            objects=[
                obj(
                    "o1",
                    ops=[
                        Cut(
                            point=(0, 0, Exposed(expose="cut_height", value=32)),
                            normal=(0, 0, 1),
                            keep="below",
                        )
                    ],
                ),
                obj(
                    "o2",
                    ops=[
                        Cut(
                            point=(0, 0, Exposed(expose="cut_height", value=40)),
                            normal=(0, 0, 1),
                            keep="below",
                        )
                    ],
                ),
            ],
        )


@pytest.mark.parametrize(
    "name", ["o1", "o12_parts", "sb_key", "1x", "with space", "$fn", "each", "if", "undef", "PI"]
)
def test_reserved_or_invalid_exposed_names_are_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        Exposed(expose=name, value=1)


def test_repeated_object_ids_are_refused() -> None:
    with pytest.raises(ValidationError, match="repeat"):
        Scene(source=SOURCE, objects=[obj("o1"), obj("o1")])


@pytest.mark.parametrize(
    "mesh", ["../x.3mf", "meshes/../../etc.3mf", "meshes/o1-p1.stl", "/abs/o1-p1.3mf"]
)
def test_a_mesh_path_outside_meshes_is_refused(mesh: str) -> None:
    with pytest.raises(ValidationError):
        Part(id="p1", mesh=mesh, colour="#FFFFFF")


def test_non_finite_numbers_are_refused() -> None:
    with pytest.raises(ValidationError):
        Joint(
            at=(0, 0, 0), axis=(0, 0, -1), diameter=float("nan"), depth=6, clearance=0.2, role="peg"
        )


def test_next_object_id_is_one_past_the_largest() -> None:
    scene = Scene(source=SOURCE, objects=[obj("o1"), obj("o7")])
    assert next_object_id(scene) == "o8"


def test_a_trailing_newline_is_refused_everywhere() -> None:
    # python-re's `$` matches before a trailing "\n"; every pattern ends `\Z` instead.
    with pytest.raises(ValidationError):
        Exposed(expose="x\n", value=1)
    with pytest.raises(ValidationError):
        Part(id="p1", mesh="meshes/o1-p1.3mf", colour="#FFFFFF\n")
    with pytest.raises(ValidationError):
        Part(id="p1\n", mesh="meshes/o1-p1.3mf", colour="#FFFFFF")
    with pytest.raises(ValidationError):
        Part(id="p1", mesh="meshes/o1-p1.3mf\n", colour="#FFFFFF")
    with pytest.raises(ValidationError):
        obj("o1\n")
    with pytest.raises(ValidationError):
        SceneSource(kind="upload", name="x.3mf", sha256="ab" * 32 + "\n")


def test_named_keeps_a_suffixed_name_within_the_cap() -> None:
    assert named("Carrot", " (top)") == "Carrot (top)"
    long = named("x" * 300, " (bottom)")
    assert len(long) == MAX_NAME and long.endswith(" (bottom)")
    assert named("y" * 300) == "y" * MAX_NAME
