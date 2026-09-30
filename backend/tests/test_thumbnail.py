"""The plate cover images.

What these pin is the thing issue #104 is about: a two-colour model has to READ
as two colours. Nothing here compares pixels to a recorded image — the renderer
is a pure function of the mesh, but a golden PNG would turn every lighting or
framing tweak into a binary diff nobody can review. Colour presence, coverage,
size and transparency are the properties that carry the meaning.
"""

from __future__ import annotations

from typing import get_args

import numpy as np
import pytest
import trimesh

from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import (
    MAX_BREAKDOWN_COLOURS,
    MAX_VIEW_SIZE,
    MIN_VIEW_SIZE,
    PLATE_PNG_SIZE,
    PLATE_SMALL_PNG_SIZE,
    VIEW_DIRECTIONS,
    ViewName,
    _downsample,
    breakdown_colours,
    encode_png,
    render_colour_breakdown,
    render_plate_thumbnails,
    render_view,
)
from tests.conftest import read_png

BLUE = "#0047BB"
PINK = "#FF1493"


def _translate(x: float, y: float, z: float) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, 3] = (x, y, z)
    return matrix


def _two_colour_parts() -> list[ColourPart]:
    """A pink slab sitting on a blue one — the keychain's shape, at two boxes."""
    return [
        ColourPart(1, "Color 1", BLUE, trimesh.creation.box(extents=(40, 20, 6))),
        ColourPart(
            2,
            "Color 2",
            PINK,
            trimesh.creation.box(extents=(20, 10, 3), transform=_translate(0, 0, 4)),
        ),
    ]


@pytest.fixture
def rendered() -> dict[str, np.ndarray]:
    thumbnails = render_plate_thumbnails(_two_colour_parts())
    return {
        "plate": read_png(thumbnails.plate),
        "plate_small": read_png(thumbnails.plate_small),
        "top": read_png(thumbnails.top),
        "pick": read_png(thumbnails.pick),
    }


def _opaque_colours(image: np.ndarray) -> np.ndarray:
    return np.unique(image[image[..., 3] == 255][:, :3], axis=0)


def test_the_images_are_the_sizes_bambu_studio_writes(rendered: dict[str, np.ndarray]) -> None:
    assert rendered["plate"].shape == (PLATE_PNG_SIZE, PLATE_PNG_SIZE, 4)
    assert rendered["plate_small"].shape == (PLATE_SMALL_PNG_SIZE, PLATE_SMALL_PNG_SIZE, 4)
    assert rendered["top"].shape == (PLATE_PNG_SIZE, PLATE_PNG_SIZE, 4)
    assert rendered["pick"].shape == (PLATE_PNG_SIZE, PLATE_PNG_SIZE, 4)


def test_a_two_colour_model_shows_both_filament_colours(rendered: dict[str, np.ndarray]) -> None:
    """The acceptance test of #104, stated in pixels.

    Every lit face of a part is its filament colour scaled by one Lambert term,
    so the hue survives shading: the brightest opaque pixel of each part's
    channel mix is that part's colour at full light.
    """
    colours = _opaque_colours(rendered["plate"]).astype(np.int64)
    # Hue, not exact RGB: shading scales all three channels by the same factor.
    hues = {tuple(np.rint(row / max(row.max(), 1) * 255).astype(int)) for row in colours}

    def nearest(target: str) -> int:
        rgb = np.array([int(target[i : i + 2], 16) for i in (1, 3, 5)])
        normalised = np.rint(rgb / rgb.max() * 255).astype(int)
        return min(int(np.abs(np.array(hue) - normalised).sum()) for hue in hues)

    assert nearest(BLUE) <= 8
    assert nearest(PINK) <= 8


def test_each_part_covers_a_real_share_of_the_image(rendered: dict[str, np.ndarray]) -> None:
    """A colour that is present on four stray edge pixels would pass the test
    above and still look single-colour. The smaller part is 1/8 the footprint of
    the larger one, so both have to be clearly there."""
    image = rendered["plate"]
    opaque = image[..., 3] == 255
    # Pink is the only colour here whose red channel dominates its blue.
    pink = opaque & (image[..., 0] > image[..., 2])
    blue = opaque & (image[..., 2] > image[..., 0])
    assert pink.sum() > 0.02 * opaque.sum()
    assert blue.sum() > 0.5 * opaque.sum()


def test_the_background_is_transparent(rendered: dict[str, np.ndarray]) -> None:
    for name, image in rendered.items():
        assert image[0, 0, 3] == 0, name
        assert image[-1, -1, 3] == 0, name
        assert (image[..., 3] == 255).any(), name


def test_the_small_cover_is_the_large_one_reduced(rendered: dict[str, np.ndarray]) -> None:
    """Studio's small cover is a box filter of the big one, so the two have to
    agree on where the model is rather than being two independent renders."""
    step = PLATE_PNG_SIZE // PLATE_SMALL_PNG_SIZE
    large = rendered["plate"][..., 3].reshape(
        PLATE_SMALL_PNG_SIZE, step, PLATE_SMALL_PNG_SIZE, step
    )
    assert (
        np.corrcoef(
            large.mean(axis=(1, 3)).ravel(), rendered["plate_small"][..., 3].ravel().astype(float)
        )[0, 1]
        > 0.99
    )


def test_the_pick_image_is_one_flat_colour_per_part(rendered: dict[str, np.ndarray]) -> None:
    """Studio reads this one as a pixel -> object lookup, so it must not be
    shaded or antialiased: exactly as many opaque colours as there are parts."""
    assert _opaque_colours(rendered["pick"]).tolist() == [[0, 0, 1], [0, 0, 2]]


def test_the_top_view_looks_straight_down(rendered: dict[str, np.ndarray]) -> None:
    """The smaller part sits centred on the larger one, so from above its
    silhouette is a centred rectangle entirely inside the other's."""
    image = rendered["pick"]
    smaller = np.argwhere((image[..., 2] == 2) & (image[..., 3] == 255))
    larger = np.argwhere((image[..., 2] == 1) & (image[..., 3] == 255))
    assert smaller.min(axis=0).min() > larger.min(axis=0).min()
    assert smaller.max(axis=0).max() < larger.max(axis=0).max()
    centre = (smaller.min(axis=0) + smaller.max(axis=0)) / 2
    assert centre == pytest.approx([PLATE_PNG_SIZE / 2] * 2, abs=1.5)


def test_a_single_colour_model_still_renders() -> None:
    parts = [ColourPart(1, "Color 1", BLUE, trimesh.creation.box(extents=(10, 10, 10)))]
    image = read_png(render_plate_thumbnails(parts).plate)
    assert (image[..., 3] == 255).sum() > 0.3 * PLATE_PNG_SIZE**2


def test_an_open_mesh_is_not_culled_away() -> None:
    """`render.jobs` hands the writer an open split mesh whenever OpenSCAD could
    not close the solids, and a backface cull would render it full of holes."""
    closed = trimesh.creation.box(extents=(20, 20, 20))
    open_mesh = trimesh.Trimesh(
        vertices=closed.vertices.copy(), faces=closed.faces[:6].copy(), process=False
    )
    image = read_png(render_plate_thumbnails([ColourPart(1, "Open", BLUE, open_mesh)]).plate)
    assert (image[..., 3] == 255).any()


def test_rendering_is_reproducible() -> None:
    first = render_plate_thumbnails(_two_colour_parts())
    second = render_plate_thumbnails(_two_colour_parts())
    assert first == second


def test_empty_part_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one colour part"):
        render_plate_thumbnails([])


def test_the_encoder_round_trips_through_the_reader() -> None:
    image = np.arange(4 * 3 * 4, dtype=np.uint8).reshape(4, 3, 4)
    assert np.array_equal(read_png(encode_png(image)), image)


def test_downsampling_an_edge_keeps_its_colour_and_scales_only_coverage() -> None:
    """#117: a transparent sample carries no colour, so it must not darken the edge.

    One opaque red pixel in a 2x2 block of otherwise transparent ones is a quarter
    coverage of red: (255, 0, 0, 64). A straight box filter gives (64, 0, 0, 64),
    a dark red that compositing darkens a second time.
    """
    image = np.zeros((2, 2, 4), dtype=np.uint8)
    image[0, 0] = (255, 0, 0, 255)

    assert _downsample(image, 1)[0, 0].tolist() == [255, 0, 0, 64]


def test_downsampling_a_fully_transparent_block_stays_transparent_black() -> None:
    image = np.zeros((2, 2, 4), dtype=np.uint8)

    assert _downsample(image, 1)[0, 0].tolist() == [0, 0, 0, 0]


def test_downsampling_two_opaque_colours_still_averages_them() -> None:
    image = np.zeros((2, 2, 4), dtype=np.uint8)
    image[:, 0] = (255, 0, 0, 255)
    image[:, 1] = (0, 0, 255, 255)

    assert _downsample(image, 1)[0, 0].tolist() == [128, 0, 128, 255]


def test_downsampling_keeps_each_larger_block_to_itself() -> None:
    """Production reduces 4x4 and larger blocks, not only 2x2: each output pixel must
    come from its own block, premultiplied, whatever the step."""
    image = np.zeros((8, 8, 4), dtype=np.uint8)
    image[0, 0] = (255, 0, 0, 255)  # one sample of sixteen in the top-left block
    image[0:4, 4:8] = (0, 0, 255, 255)  # top-right block fully blue
    image[4:6, 4:8] = (0, 255, 0, 255)  # bottom-right block half green

    reduced = _downsample(image, 2)

    assert reduced[0, 0].tolist() == [255, 0, 0, 16]
    assert reduced[0, 1].tolist() == [0, 0, 255, 255]
    assert reduced[1, 0].tolist() == [0, 0, 0, 0]
    assert reduced[1, 1].tolist() == [0, 255, 0, 128]


# ── named views (#252) ───────────────────────────────────────────────────────


def _opaque_extent(image: np.ndarray) -> tuple[int, int]:
    """(width, height) of the opaque pixels' bounding box."""
    rows, cols = np.nonzero(image[..., 3] == 255)
    return int(cols.max() - cols.min() + 1), int(rows.max() - rows.min() + 1)


def _long_in_x() -> list[ColourPart]:
    return [ColourPart(1, "Color 1", BLUE, trimesh.creation.box(extents=(40, 10, 10)))]


def test_every_view_name_has_a_direction() -> None:
    assert set(get_args(ViewName)) == set(VIEW_DIRECTIONS)


@pytest.mark.parametrize(
    ("view", "wide"),
    [
        ("front", True),
        ("back", True),
        ("top", True),
        ("bottom", True),
        ("left", False),
        ("right", False),
    ],
)
def test_each_view_looks_along_its_own_axis(view: ViewName, wide: bool) -> None:
    """A bar along X is wide from the front, back, top and bottom, and square end-on."""
    width, height = _opaque_extent(read_png(render_view(_long_in_x(), view, 256)))

    if wide:
        assert width > 3 * height
    else:
        assert abs(width - height) <= 2


def test_a_view_is_the_size_asked_for_and_shows_every_colour() -> None:
    image = read_png(render_view(_two_colour_parts(), "iso", 200))

    assert image.shape == (200, 200, 4)
    opaque = _opaque_colours(image).astype(int)
    red, blue = opaque[:, 0], opaque[:, 2]
    assert np.any(blue > red + 60)  # the blue slab
    assert np.any(red > blue + 60)  # the pink one


@pytest.mark.parametrize(
    ("view", "size"),
    [("sideways", 256), ("front", MIN_VIEW_SIZE - 1), ("front", MAX_VIEW_SIZE + 1)],
)
def test_a_view_refuses_what_it_cannot_draw(view: str, size: int) -> None:
    with pytest.raises(ValueError):
        render_view(_long_in_x(), view, size)  # type: ignore[arg-type]


def test_a_view_of_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        render_view([], "iso")


# ── per-colour breakdown (#252) ──────────────────────────────────────────────


def _cubes(*colours: str) -> list[ColourPart]:
    return [
        ColourPart(
            index + 1,
            f"Color {index + 1}",
            colour,
            trimesh.creation.box(
                extents=(10, 10, 10),
                transform=trimesh.transformations.translation_matrix((index * 20, 0, 0)),
            ),
        )
        for index, colour in enumerate(colours)
    ]


def test_breakdown_colours_follow_the_extruder_order_then_the_parts() -> None:
    parts = _cubes("#FF0000", "#00FF00", "#0000FF", "#00FF00")
    assert breakdown_colours(parts) == ["#FF0000", "#00FF00", "#0000FF"]
    # Lower case in `order` still matches; a colour no part has is left out.
    assert breakdown_colours(parts, ["#0000ff", "#123456"]) == ["#0000FF", "#FF0000", "#00FF00"]


def test_a_breakdown_is_a_near_square_grid_of_tiles() -> None:
    drawn = render_colour_breakdown(_cubes("#FF0000", "#00FF00"), "top", 64)
    assert drawn.columns == 2
    assert read_png(drawn.png).shape == (64, 128, 4)


def test_a_breakdown_past_its_deadline_stops_before_the_next_tile() -> None:
    # Review of #750: the route's timeout cannot stop the worker thread.
    with pytest.raises(TimeoutError, match="after 0 of 2 tiles"):
        render_colour_breakdown(_cubes("#FF0000", "#00FF00"), "top", 64, deadline=0.0)


def test_a_breakdown_refuses_too_many_colours() -> None:
    colours = [f"#0000{index:02X}" for index in range(MAX_BREAKDOWN_COLOURS + 1)]
    with pytest.raises(ValueError, match="more than a breakdown draws"):
        render_colour_breakdown(_cubes(*colours), "top", 64)
