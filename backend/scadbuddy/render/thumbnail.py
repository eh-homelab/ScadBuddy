"""The plate cover images Bambu Studio embeds in a 3MF, rendered in pure numpy.

Why a hand-written rasteriser rather than a real renderer: every candidate that
draws through a GL context needs something this image does not have. `pyrender`
wants OSMesa or EGL — an apt tree of its own, and a `libGL` the OpenSCAD base
does not ship; OpenSCAD's own `--render` PNG export needs an offscreen GL
context the headless container has no display for; matplotlib, the path Bambuddy
itself uses server-side, is a 40 MB dependency for one 512x512 image. What is
actually needed is a z-buffer, a dot product and a PNG writer, and numpy plus
stdlib `zlib` already carry all three. That also makes the output a pure
function of the mesh: no driver, no GL implementation, nothing to drift.

Bambu Studio's names and sizes are matched exactly (`bbs_3mf.hpp`'s
`THUMBNAIL_FILE_FORMAT` and friends, `PLATE_THUMBNAIL_SMALL_WIDTH`):
`Metadata/plate_1.png` at 512x512, `Metadata/plate_1_small.png` at 128x128,
`Metadata/top_1.png` and `Metadata/pick_1.png`. `plate_1.png` is the one that
matters downstream — it is the first entry Bambuddy's `ThreeMFParser`
(`services/archive.py::_extract_thumbnail`) looks for on an unsliced upload, and
what becomes a library file's `thumbnail_path`.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from scadbuddy.render.split import ColourPart

PLATE_PNG_SIZE = 512
PLATE_SMALL_PNG_SIZE = 128

# The lit views are rasterised at 2x and box-filtered down, which is the whole
# of the antialiasing. `pick` is NOT supersampled: it is a lookup table from
# pixel to part, so a blended edge pixel would name a part that is not there.
SUPERSAMPLE = 2

# Where the camera SITS for the lit 3/4 view, relative to the model, in
# OpenSCAD's Z-up millimetres: front, right and above, roughly where Studio
# parks its plate camera. `_basis` is given the direction it LOOKS, which is the
# negation of this.
VIEW_POSITION = (0.45, -0.85, 1.1)

# Fraction of the frame left empty around the model's projected bounds.
MARGIN = 0.08

# Flat shading, one normal per triangle. `AMBIENT` is what a face pointing away
# from the light keeps, so a part's colour stays recognisable everywhere on it —
# the point of this image is which colour goes where, not the lighting.
AMBIENT = 0.42
LIGHT_DIRECTION = (0.35, 0.45, 1.0)

# Upper bound on the pixels one batch of triangle bounding boxes may cover, so a
# model made of thousands of large triangles cannot turn into a multi-gigabyte
# temporary.
BATCH_PIXELS = 1 << 22


@dataclass(frozen=True)
class PlateThumbnails:
    """The four PNGs, named for the `Metadata/` entries they become."""

    plate: bytes
    plate_small: bytes
    top: bytes
    pick: bytes


def _unit(vector: Sequence[float] | np.ndarray) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float64)
    return array / np.linalg.norm(array)


def _basis(forward: Sequence[float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Right, up and forward for a camera looking along `forward`.

    World up is Z, except for the top view, which looks straight down it and
    would make the cross product degenerate; that one takes +Y as up, which puts
    the model's +Y at the top of the image.
    """
    ahead = _unit(forward)
    world_up = np.array([0.0, 1.0, 0.0]) if abs(ahead[2]) > 0.999 else np.array([0.0, 0.0, 1.0])
    right = _unit(np.cross(ahead, world_up))
    up = np.cross(right, ahead)
    return right, up, ahead


def _rasterise(
    parts: Sequence[ColourPart],
    forward: Sequence[float],
    size: int,
    *,
    shaded: bool,
    colours: Sequence[tuple[int, int, int]],
) -> np.ndarray:
    """A size x size x 4 uint8 RGBA image of `parts`, transparent behind them.

    Orthographic rather than perspective on purpose: this image is read as "which
    colour is which part", and perspective on a plate-sized object adds
    foreshortening without adding information.
    """
    right, up, ahead = _basis(forward)

    bounds = np.array([part.mesh.bounds for part in parts])
    low = bounds[:, 0, :].min(axis=0)
    high = bounds[:, 1, :].max(axis=0)
    centre = (low + high) / 2

    # Every corner of the model's AABB, projected: what has to fit in the frame
    # is the extent of the PROJECTION, not of the box.
    corners = np.array(np.meshgrid(*zip(low, high, strict=True))).reshape(3, -1).T - centre
    span = float(np.abs(corners @ np.column_stack((right, up))).max()) * 2
    scale = (size * (1 - 2 * MARGIN)) / span if span > 0 else 1.0

    light = _unit(np.asarray(LIGHT_DIRECTION) @ np.array([right, up, -ahead]))

    triangles: list[np.ndarray] = []
    shades: list[np.ndarray] = []
    for part, rgb in zip(parts, colours, strict=True):
        base = np.asarray(rgb, dtype=np.float64) / 255
        vertices = np.asarray(part.mesh.vertices, dtype=np.float64) - centre
        screen = np.column_stack(
            (
                size / 2 + (vertices @ right) * scale,
                size / 2 - (vertices @ up) * scale,
                vertices @ ahead,
            )
        )
        faces = np.asarray(part.mesh.faces)
        triangles.append(screen[faces])
        if shaded:
            normals = np.asarray(part.mesh.face_normals, dtype=np.float64)
            lit = AMBIENT + (1 - AMBIENT) * np.clip(normals @ light, 0.0, 1.0)
        else:
            lit = np.ones(len(faces))
        shades.append(base * lit[:, None])

    return _draw(
        np.concatenate(triangles).astype(np.float64),
        np.concatenate(shades),
        size,
    )


def _draw(triangles: np.ndarray, colours: np.ndarray, size: int) -> np.ndarray:
    """Depth-test every triangle at once and compose the RGBA image.

    Rasterising one triangle per Python iteration is the obvious shape and costs
    ~140 us a triangle almost regardless of the triangle's size — numpy call
    overhead, not pixels — which puts an 80k-face model at half a minute for
    three views. So fragments are generated for every triangle first
    (`_fragments` batches triangles by the size of their pixel bounding box, so
    one set of numpy calls covers thousands of them) and resolved here with a
    painter's pass: sort the fragments far-to-near and scatter-assign, because
    numpy's fancy-index assignment keeps the LAST write to a repeated index.
    That is a depth test with no per-pixel loop.

    There is no backface cull. It would halve the work on a closed part, but this
    also renders the open split mesh (`UNCOLOURED_WARNING` in `render.jobs`),
    where culling punches holes straight through the model.
    """
    image = np.zeros((size * size, 4), dtype=np.uint8)
    pixels, depth, owner = _fragments(triangles, size)
    if len(pixels) == 0:
        return image.reshape(size, size, 4)
    # Stable, so equally deep fragments resolve as a function of the mesh alone
    # and the PNG bytes stay reproducible.
    order = np.argsort(-depth, kind="stable")
    image[pixels[order], :3] = np.rint(np.clip(colours[owner[order]], 0, 1) * 255).astype(np.uint8)
    image[pixels, 3] = 255
    return image.reshape(size, size, 4)


def _fragments(triangles: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Covered pixel index, depth and triangle index, for every triangle.

    Triangles are grouped by their pixel bounding box rounded up to a power of
    two on each axis, so every member of a group rasterises in one broadcast
    against the same block of pixel centres.
    """
    xs, ys = triangles[..., 0], triangles[..., 1]
    x0 = np.clip(np.floor(xs.min(axis=1)), 0, size).astype(np.int64)
    x1 = np.clip(np.ceil(xs.max(axis=1)) + 1, 0, size).astype(np.int64)
    y0 = np.clip(np.floor(ys.min(axis=1)), 0, size).astype(np.int64)
    y1 = np.clip(np.ceil(ys.max(axis=1)) + 1, 0, size).astype(np.int64)
    width = x1 - x0
    height = y1 - y0

    ax, bx, cx = xs.T
    ay, by, cy = ys.T
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)

    live = np.flatnonzero((width > 0) & (height > 0) & (area != 0))
    empty = np.zeros(0, dtype=np.int64)
    if len(live) == 0:
        return empty, np.zeros(0, dtype=np.float64), empty

    block_w = 1 << np.ceil(np.log2(width[live])).astype(np.int64)
    block_h = 1 << np.ceil(np.log2(height[live])).astype(np.int64)

    pixels: list[np.ndarray] = []
    depths: list[np.ndarray] = []
    owners: list[np.ndarray] = []
    for key in np.unique(block_w * (size + 1) + block_h):
        member = (block_w * (size + 1) + block_h) == key
        group = live[member]
        bw, bh = int(block_w[member][0]), int(block_h[member][0])
        batches = max(1, -(-len(group) * bw * bh // BATCH_PIXELS))
        for batch in np.array_split(group, batches):
            found = _block(triangles, batch, (x0, x1, y0, y1), (bw, bh), size, area)
            pixels.append(found[0])
            depths.append(found[1])
            owners.append(found[2])
    return np.concatenate(pixels), np.concatenate(depths), np.concatenate(owners)


def _block(
    triangles: np.ndarray,
    batch: np.ndarray,
    box: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    block: tuple[int, int],
    size: int,
    area: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One broadcast: barycentric coverage and depth for a batch of triangles
    whose bounding boxes all fit the same (height, width) block."""
    x0, x1, y0, y1 = (bound[batch][:, None, None] for bound in box)
    bw, bh = block
    x = x0 + np.arange(bw, dtype=np.int64)[None, None, :]
    y = y0 + np.arange(bh, dtype=np.int64)[None, :, None]
    px = x + 0.5
    py = y + 0.5

    # (vertex, axis, triangle, 1, 1): the leading axes are what the unpacking
    # below reads, the trailing ones broadcast against the pixel block.
    corner = triangles[batch].transpose(1, 2, 0)[..., None, None]
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = corner
    denominator = area[batch][:, None, None]
    # Barycentric weights of C and of B. Their signs flip together with the
    # signed area, so the inside test holds for either winding.
    wc = ((bx - ax) * (py - ay) - (by - ay) * (px - ax)) / denominator
    wb = ((px - ax) * (cy - ay) - (py - ay) * (cx - ax)) / denominator

    inside = (wc >= 0) & (wb >= 0) & (wc + wb <= 1) & (x < x1) & (y < y1)
    depth = az + wb * (bz - az) + wc * (cz - az)
    owner = np.broadcast_to(batch[:, None, None], inside.shape)
    return (np.broadcast_to(y * size + x, inside.shape)[inside], depth[inside], owner[inside])


def _downsample(image: np.ndarray, size: int) -> np.ndarray:
    """Box filter to `size`. Averaging alpha too is what feathers the silhouette."""
    step = image.shape[0] // size
    if step == 1:
        return image
    blocks = image.reshape(size, step, size, step, 4).astype(np.float64)
    return np.rint(blocks.mean(axis=(1, 3))).astype(np.uint8)


def encode_png(image: np.ndarray) -> bytes:
    """An 8-bit RGBA PNG. Filter type 0 on every row: these are flat-shaded
    facets, so a predictor buys little and costs reproducibility."""
    height, width, _ = image.shape
    rows = np.hstack((np.zeros((height, 1), dtype=np.uint8), image.reshape(height, width * 4)))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows.tobytes(), 9))
        + chunk(b"IEND", b"")
    )


def _pick_colours(count: int) -> list[tuple[int, int, int]]:
    """One flat colour per part, its 1-based index in the low channels.

    Studio reads this image as a pixel -> object lookup, so the colours only have
    to be distinct from each other and from the transparent background. Encoding
    the index keeps the mapping readable rather than arbitrary.
    """
    return [(0, (index >> 8) & 0xFF, index & 0xFF) for index in range(1, count + 1)]


def render_plate_thumbnails(parts: Sequence[ColourPart]) -> PlateThumbnails:
    if not parts:
        raise ValueError("a plate thumbnail needs at least one colour part")
    colours = [
        (int(part.colour[1:3], 16), int(part.colour[3:5], 16), int(part.colour[5:7], 16))
        for part in parts
    ]
    supersampled = PLATE_PNG_SIZE * SUPERSAMPLE
    looking = tuple(-axis for axis in VIEW_POSITION)
    lit = _rasterise(parts, looking, supersampled, shaded=True, colours=colours)
    top = _rasterise(parts, (0.0, 0.0, -1.0), supersampled, shaded=True, colours=colours)
    pick = _rasterise(
        parts, (0.0, 0.0, -1.0), PLATE_PNG_SIZE, shaded=False, colours=_pick_colours(len(parts))
    )
    return PlateThumbnails(
        plate=encode_png(_downsample(lit, PLATE_PNG_SIZE)),
        plate_small=encode_png(_downsample(lit, PLATE_SMALL_PNG_SIZE)),
        top=encode_png(_downsample(top, PLATE_PNG_SIZE)),
        pick=encode_png(pick),
    )
