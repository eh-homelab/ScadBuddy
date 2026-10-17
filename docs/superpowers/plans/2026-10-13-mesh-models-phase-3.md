# Mesh models, phase 3: paint transfer, weak joints and the carrot eval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A mesh model's Bambu Studio painting prints (the Carrot Garden's green leaves print green), and `analyze_geometry` finds a weak neck and hands the agent a `split_at_plane` edit with a joint that fits, which the eval "the leaves of this carrot snap off; fix it" scores.

**Architecture:** Paint transfer is a new `paint` stage of the render, run in the piece's `render_solids` activity right after the per-colour solids: `meshmodel/paint.py` matches each rendered triangle back to the painted source triangle it came from (numpy, `render/meshmatch.py`), and the piece's `layout.json` names a file of the carried codes, so the piece's own `model.3mf`, every output and Arrange (which all write from that layout) carry the paint. `render/geometry.py` gains `weak_joints`, from a vectorised per-layer section profile (`meshmodel/section.py` `layer_areas`) of each connected piece; `meshmodel/locate.py` names the scene object for an output of a mesh model. The agent gets no new tool: tool descriptions, the authoring skill and a new eval scenario on the recorded backend.

**Tech Stack:** Python 3.12, numpy, trimesh 5.1.1 (no scipy, shapely or rtree: `trimesh.proximity.closest_point` raises `ModuleNotFoundError: rtree`, so nearest-point and nearest-triangle queries are written here in numpy), pydantic v2, FastAPI, Temporal (unchanged workflows); TypeScript/zod and vitest for the agent and its evals.

**Spec:** `docs/superpowers/specs/2026-10-11-mesh-models-design.md`, phase 3 of §11: §7 (paint) and §9 (`weak_joints`), plus §10's agent eval scenario. Phase 4 (§8, the UI) is out of scope. Phases 1–2 are `docs/superpowers/plans/2026-10-11-mesh-models-phase-1-2.md` (merged in #2585).

## Global Constraints

- Every backend command from `CLAUDE.md`: `uv run --frozen ruff check .`, `uv run --frozen ruff format --check .`, `uv run --frozen mypy` (strict), `uv run --frozen pytest -n auto`; agent `pnpm lint && pnpm typecheck && pnpm test && pnpm build`.
- `tests/api` tests need `SCADBUDDY_TEST_DATABASE_URL` and a Temporal; they skip without them. Run them before calling a task done. Put `tests/api/` paths together in one pytest command.
- `requires_openscad` tests run only in the image (`docker build --target test -t scadbuddy:test . && docker run --rm scadbuddy:test`); `requires_fixtures` tests need `git lfs pull --include "test-fixtures/**"`. Run both before calling Tasks 6 and 9 done.
- No new Python or Node dependency (none is needed: everything here is numpy).
- OpenSCAD drops paint: its output triangles carry only material indices (spec §7). Part mesh files carry no colour (spec §3.3).
- Paint rules, verbatim from spec §7: an output triangle whose three vertices are within 1e-4 mm of a source triangle's "takes the source code verbatim"; otherwise "if the nearest source face is within 0.01 mm and its code is one colour, it takes that colour; else the part's own colour (code empty)".
- Weak joints, verbatim from spec §9: "sections at every layer height (0.2 mm default) along print Z; a section whose area is below 25 % of the larger of the sections 2 mm below and above, with at least 1 cm³ of the part's volume above it, is reported with its Z, area, and a suggested `split_at_plane`".
- A peg needs `room >= diameter / 2 + clearance + 0.8` mm; the default diameter is `1.2 * room` (`meshmodel/edits.py`, spec §5).
- Nothing is written to Bambuddy; no tool here is `outward`.
- `piece_key` and `render_key` are unchanged (CLAUDE.md: never swap them; this plan does not touch either).
- Agent-authored docs cite sources (#259); a measured fact says where and how. A plugin edit bumps the plugin's `version` (the `PostToolUse` hook does it on the branch's first plugin edit).
- Commit after every task; conventional-commit messages ending with the session's attribution lines.

## Measured for this plan (2026-10-13)

All in this worktree's `backend/.venv` (trimesh 5.1.1) and `openscad/openscad:dev.2026-10-12@sha256:4098a7c2…` (the pinned base image, `--backend=Manifold`), on Bambuddy library file 688 (`test-fixtures/makerworld/688-carrot-garden.3mf`) opened with `scene_from_objects` and compiled with `compile_scene`:

- **Op-free, all four objects:** every one of the 69,150 output triangles matched a source triangle with all three vertices within 1e-4 mm, so all 13,873 painted triangles were carried. Only 87 % fell in the same 1e-4 mm grid cell and 0.6 % matched to 1 µm: OpenSCAD's 3MF import holds coordinates as 32-bit floats and its export writes 6 decimals, so the match must be by distance, with neighbouring cells searched.
- **Vertex order:** about two thirds of the matched output triangles start at a different vertex than their source (15,614 / 14,542 / 16,612 at turns 0 / 1 / 2 on the orange part), none reversed. A Bambu `paint_color` code splits its triangle by vertex position (`render/paint.py`), so a copied code is only right once the output triangle's vertices are turned back to the source's order.
- **With the green anchor** of `tests/test_mesh_models_fixtures.py` on the small carrot (a 6 mm cylinder through the neck, `colour` `#0ACC38`): 20,934 of 21,631 orange triangles verbatim; of the 6,319 painted source triangles, exactly the 5,639 lying wholly clear of the anchor were carried verbatim.
- **Section profile** of each object's mesh at 0.2 mm layers with the §9 rule: the small carrot's neck is flagged at z 30.9–31.7 (area 81.5 mm², 499 mm² 2 mm below, 4,191 mm³ above, room 4.22 mm, centroid inside), the big carrot's at z 33.3–34.5 (85.7 mm², room 4.26 mm). The rule also flagged the planter's walls above its floor (z 2.1–3.9: 1,264 mm² against 6,494 mm²) and the big carrot's hollow body (z 28.3–28.5); in both the section's centroid is outside the section (`Section.inside` false), and in neither does a peg fit.
- A vectorised layer profile (Task 7) equals `section.measure`'s area to 5e-13 mm² on the test carrot and 3e-12 on the big carrot; the whole Carrot Garden's weak-joint pass took about 1 s.
- `read_objects` reads a `paint_color` part back from a 3MF written by `write_plates_3mf` (24 of 48 faces painted, `used()` = `['#0ACC38']`).

## Where this plan departs from the spec, and why

1. **Where the paint step runs (§7, §6).** The spec puts it in an activity "between the pieces and `pack`". `pack` sees only bounding boxes (`workflows/arrange.py`); the 3MF is written from each piece's `layout.json` by `finish_piece` (the piece's own `model.3mf`, what Customize downloads) and by `write_output` / Arrange (`workflows/outputs.py` `_write_plates`). So the paint is carried inside the piece, as a `paint` stage at the end of the `render_solids` activity (`render/jobs.py` `_render_solids`), and the layout names each painted part's codes file. Every consumer then gets it with no workflow change.
2. **Vertex order (§7 step 2).** Not in the spec: a verbatim code also turns its output triangle's vertices to the source's order (measured above), stored as one digit per face in the codes file.
3. **A mirrored part (§7 last paragraph).** The spec has it "render in its own colour". A mirror reverses the winding, so no triangle matches verbatim, but rule 2 still applies: whole-triangle colours are kept and finer painting falls back to the part's colour. `get_scene`'s notes and the render's warnings say so. The edit answer is the `ModelRecord` the routes return (`api/mesh_models.py`), so the warning is not added there.
4. **Rule 2's nearest face** is the nearest source triangle of that colour's parts (in world coordinates), found by box prefilter and exact point–triangle distance; past a pair budget the remaining faces take the part's own colour (never reached on file 688).
5. **Weak joints per piece, not per part (§9).** A rendered part is one colour across the whole plate, so its sections at one height add up every object of that colour. The plan sections each *piece*: a part's connected islands, joined across parts where they share a vertex (two colours printed as one). It also reports only sections whose centroid lies inside them (measured above: this drops a planter's walls and a hollow body, where no peg could go anyway). The volume above is the sum of the layer areas above times the layer height.
6. **The suggestion's object (§9).** `analyze_geometry` reads an output's 3MF, which knows no scene. `GET /outputs/{id}/geometry` fills `suggestion.object` for an output of a mesh model that was not arranged, reading the scene at the output's revision; otherwise it is null.
7. **The eval (§10).** The bundled `models/mesh-joint-demo` is already split, so the scenario seeds the unsplit carrot `make.py` starts from in the recorded backend, which answers `analyze_geometry` with the neck as a weak joint until a render made after a split through it. Its "render" is the recorded backend's; the real geometry is the backend tests' (Tasks 6, 8, 9) and the demo's `verify.sh`.
8. **Pieces cached before this release** keep no paint: `piece_key` has no code version, so a revision and parameters rendered before carry none until the next scene edit or parameter change. Not fixed here (changing `piece_key` re-renders everything); the skill says so.

## Review Focus

1. **Copies of one painted mesh** (the Carrot Garden places several objects; `scene_from_objects` shares one mesh file across an object's copies): each copy, at its own place, gets the whole painting, and no triangle of one copy takes another's code. Pinned in Task 3 (`test_copies_of_one_painted_mesh_are_each_painted`).
2. **An exposed transform parameter moves the painted object at render time** (`leaves_x` in the demo): the paint follows the render's value, not the scene's default. Pinned in Task 3 (`test_an_exposed_translate_carries_the_paint_to_where_it_renders`).
3. **A painted model whose `model.scad` was edited by hand:** the scene no longer describes the geometry, so no paint is guessed onto it, and the render's warnings say why. Pinned in Task 3 (`test_a_hand_edited_model_carries_no_paint_and_says_so`).
4. **A painted part recoloured with `set_colour`:** the painted faces keep their painted colours, the rest prints in the new colour, and every colour the painting uses gets a filament slot in the 3MF. Pinned in Task 3 (`test_a_recoloured_part_keeps_its_painted_colours`) and Task 4 (`test_a_mesh_models_painting_reaches_its_3mf`'s filament list).
5. **`analyze_geometry` on an output that is not a plain mesh-model render** (a template's render, an arranged output): weak joints are still reported, with `suggestion.object` null rather than a wrong object or an error. Pinned in Task 9 (`test_a_weak_joint_names_no_object_outside_a_mesh_models_render`).

---

## File map

| File | Responsibility |
|---|---|
| `backend/scadbuddy/render/paint.py` | `uniform_state`, `single_code` (Task 1) |
| `backend/scadbuddy/render/meshmatch.py` | `match_points`, `point_triangle_distance`, `nearest_triangles` (Task 2) |
| `backend/scadbuddy/meshmodel/meshes.py` | `part_loader`: a model directory's part meshes (Task 3) |
| `backend/scadbuddy/meshmodel/paint.py` | Paint transfer: `transfer`, `carry_paint`, the carried-paint file, `with_params`, `paint_warnings`, `scene_notes` (Task 3) |
| `backend/scadbuddy/render/jobs.py` | `PartSource.paint`; `paint_layout`; the `paint` stage (Task 4) |
| `backend/scadbuddy/core/metrics.py` | `RenderStage` gains `"paint"` (Task 4) |
| `backend/scadbuddy/meshmodel/ingest.py`, `backend/scadbuddy/api/mesh_models.py` | Scene notes (Task 5) |
| `backend/tests/test_mesh_models_paint.py` | `requires_openscad` + `requires_fixtures` acceptance on file 688 (Task 6) |
| `backend/scadbuddy/meshmodel/section.py` | `layer_areas` (Task 7) |
| `backend/scadbuddy/render/geometry.py` | `weak_joints`, its models, `ANALYSIS_VERSION = 3` (Task 8) |
| `backend/scadbuddy/meshmodel/locate.py`, `backend/scadbuddy/api/outputs.py` | A weak joint's scene object (Task 9) |
| `agent/src/tools/outputs.ts`, `agent/src/tools/meshModels.ts`, `plugins/scadbuddy/skills/authoring/SKILL.md` | Tool descriptions, the skill (Task 10) |
| `agent/evals/backend.ts`, `agent/evals/scenarios.ts`, `agent/test/evals.test.ts`, `docs/ai/evals.md` | The carrot eval (Task 11) |
| `docs/superpowers/specs/2026-10-11-mesh-models-design.md`, `CLAUDE.md` | Spec and layout notes (Task 12) |

---

### Task 1: Whole-triangle paint codes

**Files:**
- Modify: `backend/scadbuddy/render/paint.py` (`_walk`, `states`; add `uniform_state`, `single_code`)
- Test: `backend/tests/test_paint.py`

**Interfaces:**
- Consumes: `_walk`, `MAX_STATE`, `PaintCodeError` (same module).
- Produces: `uniform_state(code: str) -> int | None` (`""` is 0; None when the triangle's leaves name two extruders, 0 counted); `single_code(state: int) -> str` (the unsplit code; `""` for 0). `states(code)` is unchanged in behaviour.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_paint.py` (it already defines `WHOLE = {1: "4", 2: "8", 3: "0C", 4: "1C"}`; add the imports to its import line):

```python
from scadbuddy.render.paint import PaintCodeError, remap, single_code, states, uniform_state

# A real split code from Bambuddy library file 688: leaves of extruder 0 and 1.
MIXED = "00400644002044A3003003"
# Split in two (sides 01, special side 0), both halves extruder 2.
SPLIT_GREEN = "881"


def test_a_whole_triangle_has_one_state() -> None:
    for extruder, code in WHOLE.items():
        assert uniform_state(code) == extruder
    assert uniform_state("") == 0


def test_a_split_triangle_of_one_extruder_has_one_state() -> None:
    assert uniform_state(SPLIT_GREEN) == 2


def test_a_triangle_split_between_two_extruders_has_none() -> None:
    assert uniform_state("481") is None
    # The part's own colour (leaf 0) is a colour too: painted in part only.
    assert uniform_state(MIXED) is None
    assert states(MIXED) == {1}


def test_single_code_is_bambu_studios_whole_triangle() -> None:
    assert {s: single_code(s) for s in WHOLE} == WHOLE
    assert single_code(0) == ""


def test_single_code_round_trips_every_state() -> None:
    for state in range(0, 60):
        assert uniform_state(single_code(state)) == state
    assert remap(single_code(2), {2: 33}) == single_code(33)


def test_single_code_refuses_a_state_no_code_names() -> None:
    with pytest.raises(PaintCodeError):
        single_code(256)
    with pytest.raises(PaintCodeError):
        single_code(-1)
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_paint.py -v`
Expected: FAIL with `ImportError: cannot import name 'single_code'`.

- [ ] **Step 3: Implement**

In `backend/scadbuddy/render/paint.py`, change `_walk`'s docstring and its leaf handling so the found set holds every leaf state, 0 included:

```python
def _walk(code: str, mapping: Mapping[int, int] | None) -> tuple[set[int], list[int]]:
    """Every leaf state ``code`` holds, 0 (the part's own) included, and its bits with
    each painted leaf's state renumbered by ``mapping`` (unrenumbered when None)."""
```

and, in its loop, replace

```python
        if state:
            found.add(state)
            if mapping is not None:
                state = mapping.get(state, state)
```

with

```python
        found.add(state)
        if state and mapping is not None:
            state = mapping.get(state, state)
```

Then change `states` and add the two functions after it:

```python
def states(code: str) -> set[int]:
    """The extruders ``code`` paints with, 0 (the part's own) left out."""
    return _walk(code, None)[0] - {0}


def uniform_state(code: str) -> int | None:
    """The one extruder every piece of ``code``'s triangle prints with (0: the part's
    own, which ``""`` is), or None when its painting splits it between two (#2146,
    spec 2026-10-11 §7: a face that is not its source's verbatim takes only a whole
    colour)."""
    if not code:
        return 0
    found = _walk(code, None)[0]
    return next(iter(found)) if len(found) == 1 else None


def single_code(state: int) -> str:
    """The code of a triangle painted whole with extruder ``state``, unsplit: ``""`` for
    0, as Bambu Studio leaves an unpainted triangle. The bits are `_walk`'s for one
    leaf: two of no split, then the state's."""
    if not 0 <= state <= MAX_STATE:
        raise PaintCodeError(f"extruder {state} is past what a paint code can name")
    if state == 0:
        return ""
    bits = [0, 0]
    if state >= 3:
        bits += [1, 1]
        rest = state - 3
        while rest >= 15:
            bits += [1, 1, 1, 1]
            rest -= 15
        bits += [(rest >> bit) & 1 for bit in range(4)]
    else:
        bits += [(state >> bit) & 1 for bit in range(2)]
    bits += [0] * (-len(bits) % 4)
    digits = [
        "0123456789ABCDEF"[sum(bits[at + bit] << bit for bit in range(4))]
        for at in range(0, len(bits), 4)
    ]
    return "".join(reversed(digits))
```

- [ ] **Step 4: Run them to see them pass, and the modules that use `states`**

Run: `cd backend && uv run --frozen pytest tests/test_paint.py tests/test_bambu3mf.py tests/test_split.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/render/paint.py backend/tests/test_paint.py
git commit -m "feat(render): a paint code's whole-triangle colour, and the code of one (#2146)"
```

---

### Task 2: Matching points and triangles without rtree

**Files:**
- Create: `backend/scadbuddy/render/meshmatch.py`
- Test: `backend/tests/test_meshmatch.py`

**Interfaces:**
- Produces: `FloatArray`, `IntArray` (aliases); `match_points(source: FloatArray, query: FloatArray, tol: float) -> IntArray` (per query point, the nearest source index within `tol`, else -1); `point_triangle_distance(points: FloatArray, triangles: FloatArray) -> FloatArray` (row by row; `triangles` shape (n, 3, 3)); `nearest_triangles(points: FloatArray, triangles: FloatArray, tol: float) -> tuple[IntArray, FloatArray]` (per point, the nearest triangle within `tol` and its distance, else -1 and inf).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_meshmatch.py
"""Point and triangle matching in numpy (#2146, spec 2026-10-11 §7, §9)."""

from __future__ import annotations

import math

import numpy as np
import trimesh

from scadbuddy.render.meshmatch import match_points, nearest_triangles, point_triangle_distance


def test_every_point_finds_its_source_within_the_tolerance() -> None:
    rng = np.random.default_rng(1)
    source = rng.uniform(-100, 100, (5000, 3))
    picked = rng.permutation(len(source))[:2000]
    # As OpenSCAD's export moves them: up to a few 1e-5 mm, across cell edges.
    query = source[picked] + rng.uniform(-3e-5, 3e-5, (2000, 3))
    found = match_points(source, query, 1e-4)
    assert (found == picked).all()


def test_a_point_further_than_the_tolerance_finds_nothing() -> None:
    source = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
    query = np.array([[0.0, 0.0, 2e-4], [10.0, 5e-5, 0.0]])
    assert match_points(source, query, 1e-4).tolist() == [-1, 1]


def test_repeated_source_points_still_match() -> None:
    source = np.array([[1.0, 2.0, 3.0], [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    found = match_points(source, np.array([[4.0, 5.0, 6.0], [1.0, 2.0, 3.0]]), 1e-4)
    assert found[0] == 2 and found[1] in (0, 1)


def test_empty_inputs_match_nothing() -> None:
    assert match_points(np.zeros((0, 3)), np.zeros((2, 3)), 1e-4).tolist() == [-1, -1]
    assert match_points(np.zeros((2, 3)), np.zeros((0, 3)), 1e-4).tolist() == []


def test_point_triangle_distance_by_region() -> None:
    triangle = np.array([[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]])
    points = np.array(
        [
            [0.2, 0.2, 1.0],  # above the face
            [-1.0, -1.0, 0.0],  # past corner a
            [2.0, 0.0, 0.0],  # past corner b
            [0.5, 0.5, 0.0],  # on edge bc
            [0.0, -1.0, 0.0],  # off a's corner along -y
            [0.3, 0.3, 0.0],  # in the face
            [1.0, 1.0, 0.0],  # off edge bc
        ]
    )
    measured = point_triangle_distance(points, np.repeat(triangle, len(points), axis=0))
    expected = [1.0, math.sqrt(2), 1.0, 0.0, 1.0, 0.0, math.sqrt(2) / 2]
    assert np.allclose(measured, expected)


def test_a_degenerate_triangle_is_measured_to_its_corners() -> None:
    line = np.array([[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]])
    assert np.allclose(point_triangle_distance(np.array([[1.0, 1.0, 0.0]]), line), [1.0])


def test_nearest_triangles_within_the_tolerance() -> None:
    box = trimesh.creation.box((10, 10, 10))
    points = np.array([[0.0, 0.0, 5.005], [0.0, 0.0, 0.0], [5.2, 0.0, 0.0]])
    index, distance = nearest_triangles(points, box.triangles, 0.01)
    assert index[0] >= 0 and box.face_normals[index[0]][2] == 1.0
    assert math.isclose(distance[0], 0.005, abs_tol=1e-9)
    assert index[1:].tolist() == [-1, -1]
    assert np.isinf(distance[1:]).all()
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_meshmatch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scadbuddy.render.meshmatch'`.

- [ ] **Step 3: Implement**

```python
# backend/scadbuddy/render/meshmatch.py
"""Matching a rendered mesh back to the meshes it came from (#2146, spec 2026-10-11
§7, §9): points to points within a tolerance, and points to the nearest triangle.

numpy only: trimesh's proximity queries need rtree, which is not installed (measured
2026-10-13 in backend/.venv, trimesh 5.1.1: `trimesh.proximity.closest_point` raises
``ModuleNotFoundError: rtree``)."""

from __future__ import annotations

import itertools

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

#: Bits of a cell's key per axis: three fit one int64.
_BITS = 21
_CELLS = 1 << _BITS
#: The smallest cell, mm.
MIN_CELL = 1e-3
#: Point and triangle pairs `nearest_triangles` compares at once (memory, not time).
_CHUNK_PAIRS = 1 << 22


def match_points(source: FloatArray, query: FloatArray, tol: float) -> IntArray:
    """For each ``query`` point, the index of the nearest ``source`` point at most
    ``tol`` away, or -1. Points are binned into cells no smaller than ``tol``, so a
    match is in the point's own cell or one of its 26 neighbours."""
    found = np.full(len(query), -1, dtype=np.int64)
    if len(source) == 0 or len(query) == 0:
        return found
    low = np.minimum(source.min(axis=0), query.min(axis=0)) - 2 * tol
    span = float((np.maximum(source.max(axis=0), query.max(axis=0)) - low).max()) + 2 * tol
    cell = max(MIN_CELL, tol, span / (_CELLS - 2))

    def cells(points: FloatArray) -> IntArray:
        return np.floor((points - low) / cell).astype(np.int64)

    def key(c: IntArray) -> IntArray:
        return (c[:, 0] << (2 * _BITS)) | (c[:, 1] << _BITS) | c[:, 2]

    keys = key(cells(source))
    order = np.argsort(keys, kind="stable")
    ordered = keys[order]
    most = int(np.unique(ordered, return_counts=True)[1].max())
    best = np.full(len(query), np.inf)
    home = cells(query)
    for offset in itertools.product((-1, 0, 1), repeat=3):
        shifted = home + np.asarray(offset, dtype=np.int64)
        valid = ((shifted >= 0) & (shifted < _CELLS)).all(axis=1)
        wanted = key(np.clip(shifted, 0, _CELLS - 1))
        start = np.searchsorted(ordered, wanted)
        for step in range(most):
            at = start + step
            ok = valid & (at < len(ordered))
            ok[ok] = ordered[at[ok]] == wanted[ok]
            rows = np.flatnonzero(ok)
            if len(rows) == 0:
                break
            candidates = order[at[rows]]
            distance = np.linalg.norm(source[candidates] - query[rows], axis=1)
            better = (distance <= tol) & (distance < best[rows])
            best[rows[better]] = distance[better]
            found[rows[better]] = candidates[better]
    return found


def point_triangle_distance(points: FloatArray, triangles: FloatArray) -> FloatArray:
    """Row by row, the distance from ``points[i]`` to the triangle ``triangles[i]``
    (shape (n, 3, 3)): Ericson, *Real-Time Collision Detection* §5.1.5
    (ClosestPtPointTriangle), by Voronoi region, vectorised. A degenerate triangle is
    measured to its nearest corner."""
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    ab, ac = b - a, c - a

    def dot(x: FloatArray, y: FloatArray) -> FloatArray:
        return np.einsum("ij,ij->i", x, y)

    d1, d2 = dot(ab, points - a), dot(ac, points - a)
    d3, d4 = dot(ab, points - b), dot(ac, points - b)
    d5, d6 = dot(ab, points - c), dot(ac, points - c)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    closest = np.empty_like(points)
    done = np.zeros(len(points), dtype=bool)

    def take(mask: npt.NDArray[np.bool_], value: FloatArray) -> None:
        pick = mask & ~done
        closest[pick] = value[pick]
        done[pick] = True

    with np.errstate(divide="ignore", invalid="ignore"):
        take((d1 <= 0) & (d2 <= 0), a)
        take((d3 >= 0) & (d4 <= d3), b)
        take((d6 >= 0) & (d5 <= d6), c)
        take((vc <= 0) & (d1 >= 0) & (d3 <= 0), a + (d1 / (d1 - d3))[:, None] * ab)
        take((vb <= 0) & (d2 >= 0) & (d6 <= 0), a + (d2 / (d2 - d6))[:, None] * ac)
        edge = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        take((va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0), b + edge[:, None] * (c - b))
        total = va + vb + vc
        inside = a + (vb / total)[:, None] * ab + (vc / total)[:, None] * ac
        take(np.ones(len(points), dtype=bool), inside)
        distance = np.linalg.norm(closest - points, axis=1)
    bad = ~np.isfinite(distance)
    if bad.any():
        corners = np.linalg.norm(triangles[bad] - points[bad][:, None, :], axis=2)
        distance[bad] = corners.min(axis=1)
    return np.asarray(distance, dtype=np.float64)


def nearest_triangles(
    points: FloatArray, triangles: FloatArray, tol: float
) -> tuple[IntArray, FloatArray]:
    """For each point, the index of the nearest of ``triangles`` (shape (m, 3, 3)) at
    most ``tol`` away, or -1, and that distance (inf for -1). Only triangles whose box,
    grown by ``tol``, holds the point are measured."""
    index = np.full(len(points), -1, dtype=np.int64)
    distance = np.full(len(points), np.inf)
    if len(points) == 0 or len(triangles) == 0:
        return index, distance
    low = triangles.min(axis=1) - tol
    high = triangles.max(axis=1) + tol
    chunk = max(1, _CHUNK_PAIRS // len(triangles))
    for start in range(0, len(points), chunk):
        block = points[start : start + chunk]
        near = ((block[:, None, :] >= low[None]) & (block[:, None, :] <= high[None])).all(axis=2)
        rows, cols = np.nonzero(near)
        if len(rows) == 0:
            continue
        measured = point_triangle_distance(block[rows], triangles[cols])
        keep = measured <= tol
        rows, cols, measured = rows[keep], cols[keep], measured[keep]
        if len(rows) == 0:
            continue
        order = np.lexsort((measured, rows))
        rows, cols, measured = rows[order], cols[order], measured[order]
        first = np.r_[True, rows[1:] != rows[:-1]]
        index[start + rows[first]] = cols[first]
        distance[start + rows[first]] = measured[first]
    return index, distance
```

- [ ] **Step 4: Run them to see them pass**

Run: `cd backend && uv run --frozen pytest tests/test_meshmatch.py -v && uv run --frozen mypy`
Expected: PASS, and mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/render/meshmatch.py backend/tests/test_meshmatch.py
git commit -m "feat(render): match points and triangles to a source mesh in numpy (#2146)"
```

---

### Task 3: Paint transfer

**Files:**
- Create: `backend/scadbuddy/meshmodel/meshes.py`, `backend/scadbuddy/meshmodel/paint.py`
- Test: `backend/tests/meshmodel/test_paint.py`

**Interfaces:**
- Consumes: `match_points`, `nearest_triangles` (Task 2); `uniform_state`, `single_code` (Task 1); `Paint`, `ColourPart` (`render/split.py`); `Scene`, `SceneObject`, `Part`, `Num`, `value`, `load_scene`, `text_sha256`, `SCENE_NAME` (`meshmodel/scene.py`); `linear` (`meshmodel/frames.py`); `ParamValue` (`render/schema.py`).
- Produces:
  - `meshmodel/meshes.py`: `MeshOf = Callable[[str], trimesh.Trimesh]`; `part_loader(directory: Path) -> MeshOf`.
  - `meshmodel/paint.py`: `VERBATIM_MM = 1e-4`, `NEAR_MM = 0.01`, `SOURCE_NAME = "model.scad"`, `HAND_EDITED: str`, `OLD_PAINT_NOTE: str`; `paint_file(part: Part) -> str`; `placement(obj: SceneObject) -> tuple[FloatArray, FloatArray]`; `mirrored(obj) -> bool`; `with_params(scene: Scene, params: Mapping[str, ParamValue]) -> Scene`; `paint_warnings(scene: Scene) -> list[str]`; `scene_notes(scene: Scene) -> list[str]`; `@dataclass Source(vertices, faces, codes: tuple[str, ...], palette: tuple[str, ...])`; `@dataclass CarriedPaint(paint: Paint, turns: IntArray)`; `transfer(output: trimesh.Trimesh, source: Source) -> tuple[list[str], IntArray, int]`; `carry_paint(directory: Path, params: Mapping[str, ParamValue], parts: Sequence[ColourPart]) -> tuple[list[CarriedPaint | None], list[str]]`; `turned(mesh, turns) -> trimesh.Trimesh`; `write_carried(path: Path, carried: CarriedPaint) -> None`; `read_carried(path: Path, mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, Paint]`.
  - Test helpers later tasks import from `tests/meshmodel/test_paint.py`: `ORANGE`, `GREEN`, `MIXED`, `SPLIT_GREEN`, `painted_box() -> tuple[trimesh.Trimesh, tuple[str, ...]]`, `written(directory, read) -> Scene`, `rendered(directory, scene, colour, *, mirror=False) -> trimesh.Trimesh`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/meshmodel/test_paint.py
"""Paint transfer (spec 2026-10-11 §7): a painted part's codes carried onto its
rendered solid. The "renders" here are what OpenSCAD's op-free render of a part was
measured to be (meshmodel/paint.py's docstring): the source triangles in world
coordinates, to 6 decimals, each starting at another vertex."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh

from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.ingest import scene_from_objects
from scadbuddy.meshmodel.meshes import part_loader
from scadbuddy.meshmodel.paint import (
    HAND_EDITED,
    OLD_PAINT_NOTE,
    CarriedPaint,
    carry_paint,
    paint_warnings,
    placement,
    read_carried,
    scene_notes,
    turned,
    with_params,
    write_carried,
)
from scadbuddy.meshmodel.scene import (
    SCENE_NAME,
    Exposed,
    Scene,
    SceneSource,
    Transform,
    dump_scene,
    text_sha256,
)
from scadbuddy.render.objects3mf import ReadObject
from scadbuddy.render.split import ColourPart, Paint

SOURCE = SceneSource(kind="upload", name="box.3mf", sha256="ab" * 32)
ORANGE, GREEN, PURPLE = "#F08A24", "#3A7D2C", "#7B2FBE"
# A real split code from Bambuddy library file 688: leaves of extruder 0 and 1.
MIXED = "00400644002044A3003003"
# Split in two, both halves extruder 2 (green).
SPLIT_GREEN = "881"


def painted_box() -> tuple[trimesh.Trimesh, tuple[str, ...]]:
    """A 10 mm cube in 48 triangles (vertices at the mid-planes too), painted in turn
    with nothing, green, part-orange and split-green."""
    mesh = trimesh.creation.box((10, 10, 10)).subdivide()
    cycle = ("", "8", MIXED, SPLIT_GREEN)
    return mesh, tuple(cycle[i % 4] for i in range(len(mesh.faces)))


def body(count: int = 1) -> list[ReadObject]:
    mesh, codes = painted_box()
    paint = Paint(codes, (ORANGE, GREEN))
    return [ReadObject(name="Box", count=count, parts=[ColourPart(0, "Body", ORANGE, mesh, paint)])]


def save(directory: Path, scene: Scene) -> Scene:
    """Compile ``scene`` into ``directory``: model.scad and scene.json, its hash recorded."""
    text = compile_scene(scene)
    scene = scene.model_copy(update={"generated_sha256": text_sha256(text)})
    (directory / "model.scad").write_text(text)
    (directory / SCENE_NAME).write_text(dump_scene(scene))
    return scene


def written(directory: Path, read: list[ReadObject]) -> Scene:
    scene, files = scene_from_objects(read, SOURCE)
    for name, data in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
    return save(directory, scene)


def rendered(
    directory: Path, scene: Scene, colour: str, *, mirror: bool = False
) -> trimesh.Trimesh:
    """What OpenSCAD's op-free render of the scene's ``colour`` parts looks like: world
    coordinates to 6 decimals, each face starting at another vertex; wound the other
    way where the object is mirrored (``mirror``), as the render re-orients it."""
    load = part_loader(directory)
    meshes = []
    for obj in scene.objects:
        m, t = placement(obj)
        for part in obj.parts:
            if part.colour != colour:
                continue
            mesh = load(part.mesh)
            faces = np.asarray(mesh.faces)
            if mirror:
                faces = faces[:, ::-1]
            rolled = np.array([np.roll(face, -(i % 3)) for i, face in enumerate(faces)])
            world = np.round(np.asarray(mesh.vertices) @ m.T + t, 6)
            meshes.append(trimesh.Trimesh(vertices=world, faces=rolled, process=False))
    joined = trimesh.util.concatenate(meshes)
    assert isinstance(joined, trimesh.Trimesh)
    return joined


def world_triangles(directory: Path, scene: Scene) -> np.ndarray:
    load = part_loader(directory)
    out = []
    for obj in scene.objects:
        m, t = placement(obj)
        mesh = load(obj.parts[0].mesh)
        out.append((np.asarray(mesh.vertices) @ m.T + t)[np.asarray(mesh.faces)])
    return np.concatenate(out)


def carried_one(directory: Path, output: trimesh.Trimesh, colour: str = ORANGE, **params: float) -> CarriedPaint:
    found, _ = carry_paint(directory, params, [ColourPart(1, "Body", colour, output)])
    assert found[0] is not None
    return found[0]


def test_an_op_free_part_keeps_every_code_verbatim(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    _, codes = painted_box()
    output = rendered(tmp_path, scene, ORANGE)
    carried = carried_one(tmp_path, output)
    assert carried.paint.codes == codes
    assert carried.paint.colours == (ORANGE, GREEN)
    # Turned back, every face starts where its source triangle does.
    assert np.allclose(turned(output, carried.turns).triangles, world_triangles(tmp_path, scene), atol=1e-5)
    assert set(carried.turns.tolist()) == {0, 1, 2}


def test_a_cut_part_keeps_its_uncut_faces_and_gives_cut_faces_its_own_colour(
    tmp_path: Path,
) -> None:
    """Spec §10: the box cut level at its middle (z = 5): the faces below keep their
    codes, the cap the cut made takes the part's own colour."""
    scene = written(tmp_path, body())
    _, codes = painted_box()
    whole = rendered(tmp_path, scene, ORANGE)
    below = (whole.triangles[:, :, 2] <= 5 + 1e-9).all(axis=1)
    kept = trimesh.Trimesh(vertices=whole.vertices, faces=whole.faces[below], process=False)
    cap = trimesh.Trimesh(
        vertices=[[-5, -5, 5], [5, -5, 5], [5, 5, 5], [-5, 5, 5]],
        faces=[[0, 1, 2], [0, 2, 3]],
        process=False,
    )
    output = trimesh.util.concatenate([kept, cap])
    carried = carried_one(tmp_path, output)
    expected = [code for code, keep in zip(codes, below, strict=True) if keep]
    assert list(carried.paint.codes) == [*expected, "", ""]


def test_a_retriangulated_face_takes_only_a_whole_colour(tmp_path: Path) -> None:
    """A face cut into three about its centre (a new triangulation in the same plane):
    from a green face, green; from a split-green face, green; from a face painted only
    in part, the part's own colour."""
    scene = written(tmp_path, body())
    _, codes = painted_box()
    whole = rendered(tmp_path, scene, ORANGE)
    picked = {codes.index("8"): "8", codes.index(SPLIT_GREEN): "8", codes.index(MIXED): ""}
    keep = [i for i in range(len(whole.faces)) if i not in picked]
    vertices = [np.asarray(whole.vertices)]
    faces = [np.asarray(whole.faces)[keep]]
    expected = [codes[i] for i in keep]
    for face, colour in picked.items():
        a, b, c = whole.triangles[face]
        centre = (a + b + c) / 3
        base = sum(len(v) for v in vertices)
        vertices.append(np.array([a, b, c, centre]))
        faces.append(np.array([[0, 1, 3], [1, 2, 3], [2, 0, 3]]) + base)
        expected += [colour] * 3
    output = trimesh.Trimesh(vertices=np.concatenate(vertices), faces=np.concatenate(faces), process=False)
    assert list(carried_one(tmp_path, output).paint.codes) == expected


def test_copies_of_one_painted_mesh_are_each_painted(tmp_path: Path) -> None:
    scene = written(tmp_path, body(count=2))
    assert scene.objects[0].parts[0].mesh == scene.objects[1].parts[0].mesh
    _, codes = painted_box()
    carried = carried_one(tmp_path, rendered(tmp_path, scene, ORANGE))
    assert carried.paint.codes == codes + codes


def test_an_exposed_translate_carries_the_paint_to_where_it_renders(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    moved = scene.objects[0].transform.model_copy(
        update={"translate": (Exposed(expose="box_x", value=0.0), 0.0, 0.0)}
    )
    scene = save(tmp_path, scene.model_copy(update={"objects": [scene.objects[0].model_copy(update={"transform": moved})]}))
    _, codes = painted_box()
    output = rendered(tmp_path, with_params(scene, {"box_x": 30}), ORANGE)
    assert carried_one(tmp_path, output, box_x=30).paint.codes == codes
    # At the scene's own value the render is 30 mm away from every source triangle.
    found, _ = carry_paint(tmp_path, {}, [ColourPart(1, "Body", ORANGE, output)])
    assert found == [None]


def test_a_mirrored_part_keeps_only_whole_colours_and_says_so(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    flipped = scene.objects[0].model_copy(update={"transform": Transform(scale=(-1.0, 1.0, 1.0))})
    scene = save(tmp_path, scene.model_copy(update={"objects": [flipped]}))
    _, codes = painted_box()
    found, warnings = carry_paint(
        tmp_path, {}, [ColourPart(1, "Body", ORANGE, rendered(tmp_path, scene, ORANGE, mirror=True))]
    )
    assert found[0] is not None
    whole = {"": "", "8": "8", SPLIT_GREEN: "8", MIXED: ""}
    assert list(found[0].paint.codes) == [whole[code] for code in codes]
    assert warnings == paint_warnings(scene)
    assert len(warnings) == 1 and warnings[0].startswith("o1 (Box) is mirrored")


def test_a_recoloured_part_keeps_its_painted_colours(tmp_path: Path) -> None:
    """set_colour: the part renders purple; its painted faces keep their codes, which
    name orange and green, so both still get a filament (Task 4)."""
    scene = written(tmp_path, body())
    part = scene.objects[0].parts[0].model_copy(update={"colour": PURPLE})
    scene = save(tmp_path, scene.model_copy(update={"objects": [scene.objects[0].model_copy(update={"parts": [part]})]}))
    _, codes = painted_box()
    carried = carried_one(tmp_path, rendered(tmp_path, scene, PURPLE), colour=PURPLE)
    assert carried.paint.codes == codes
    assert carried.paint.used() == [ORANGE, GREEN]


def test_a_hand_edited_model_carries_no_paint_and_says_so(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    output = rendered(tmp_path, scene, ORANGE)
    (tmp_path / "model.scad").write_text("cube(10);\n")
    assert carry_paint(tmp_path, {}, [ColourPart(1, "Body", ORANGE, output)]) == ([None], [HAND_EDITED])


def test_a_model_without_a_scene_or_paint_is_left_alone(tmp_path: Path) -> None:
    part = ColourPart(1, "Body", ORANGE, trimesh.creation.box((10, 10, 10)))
    assert carry_paint(tmp_path, {}, [part]) == ([None], [])
    plain = [ReadObject(name="Box", count=1, parts=[ColourPart(0, "Body", ORANGE, part.mesh)])]
    written(tmp_path, plain)
    assert carry_paint(tmp_path, {}, [part]) == ([None], [])


def test_another_colours_part_is_left_alone(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    found, _ = carry_paint(tmp_path, {}, [ColourPart(1, "Leaves", GREEN, rendered(tmp_path, scene, ORANGE))])
    assert found == [None]


def test_carried_paint_round_trips_through_its_file(tmp_path: Path) -> None:
    scene = written(tmp_path, body())
    output = rendered(tmp_path, scene, ORANGE)
    carried = carried_one(tmp_path, output)
    write_carried(tmp_path / "paint-1-1.json", carried)
    mesh, paint = read_carried(tmp_path / "paint-1-1.json", output)
    assert paint == carried.paint
    assert np.array_equal(mesh.faces, turned(output, carried.turns).faces)
    with pytest.raises(ValueError, match="another mesh"):
        read_carried(tmp_path / "paint-1-1.json", trimesh.creation.box((1, 1, 1)))


def test_scene_notes_drop_the_phase_one_paint_note() -> None:
    mesh, codes = painted_box()
    scene, _ = scene_from_objects(body(), SOURCE)
    old = scene.model_copy(update={"notes": [f"Box: painted in Bambu Studio; {OLD_PAINT_NOTE}, so each part prints in its own colour", "Box: kept"]})
    assert scene_notes(old) == ["Box: kept"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_paint.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scadbuddy.meshmodel.meshes'`.

- [ ] **Step 3: Implement the mesh loader**

```python
# backend/scadbuddy/meshmodel/meshes.py
"""A mesh model's part meshes, read from its directory (spec 2026-10-11 §3): each
``meshes/<object>-<part>.3mf`` as one mesh, its faces in the file's order, which is the
order its ``.paint.json`` codes follow. trimesh's default load merges vertices but keeps
every face in order; `tests/test_mesh_models_fixtures.py` pins that (#2516)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import trimesh

MeshOf = Callable[[str], trimesh.Trimesh]


def part_loader(directory: Path) -> MeshOf:
    """Each part mesh of ``directory`` read once. A path is the scene's own (`Part.mesh`
    is held to ``meshes/o<n>-p<n>.3mf``)."""
    cache: dict[str, trimesh.Trimesh] = {}

    def load(path: str) -> trimesh.Trimesh:
        if path not in cache:
            mesh = trimesh.load(directory / path, file_type="3mf", force="mesh")
            if not isinstance(mesh, trimesh.Trimesh):
                raise ValueError(f"{path} holds no mesh")
            cache[path] = mesh
        return cache[path]

    return load
```

- [ ] **Step 4: Implement paint transfer**

```python
# backend/scadbuddy/meshmodel/paint.py
"""Paint transfer (#2146, spec 2026-10-11 §7). OpenSCAD drops painting: its output
triangles carry only material indices. A painted part's codes (``<mesh stem>.paint.json``,
`ingest.py`) are carried here onto the rendered part of its colour, after the render
(`render/jobs.py` `paint_layout`, the render's ``paint`` stage):

1. An output triangle whose three vertices are a source triangle's, each within
   :data:`VERBATIM_MM`, takes that triangle's code verbatim, so painting finer than the
   triangle survives. Its vertices are turned to start where the source's do, since a
   code splits its triangle by vertex position; a match wound the other way (a mirrored
   object) is not verbatim.
2. Any other triangle (a cut face, a retriangulated face, an added shape's) takes the
   colour of the nearest source triangle within :data:`NEAR_MM` when that triangle is
   one colour (`render/paint.py` `uniform_state`), and otherwise the part's own (``""``).

Measured 2026-10-13 with openscad/openscad:dev.2026-10-12 (``--backend=Manifold``) on
Bambuddy library file 688 (``test-fixtures/makerworld/688-carrot-garden.3mf``), its four
objects op-free: all 69,150 output triangles matched a source triangle within 1e-4 mm, so
all 13,873 painted ones were carried; only 87 % fell in the same 1e-4 mm grid cell
(OpenSCAD's 3MF import holds coordinates as 32-bit floats and its export writes 6
decimals), and about two thirds started at another vertex. With the green anchor of
`tests/test_mesh_models_fixtures.py` on the small carrot, every painted triangle clear of
the anchor (5,639 of 6,319) was carried verbatim. `tests/test_mesh_models_paint.py`
re-measures both in the image."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import trimesh

from scadbuddy.meshmodel.frames import linear
from scadbuddy.meshmodel.meshes import MeshOf, part_loader
from scadbuddy.meshmodel.scene import (
    SCENE_NAME,
    Num,
    Part,
    Scene,
    SceneObject,
    load_scene,
    text_sha256,
    value,
)
from scadbuddy.render.meshmatch import match_points, nearest_triangles
from scadbuddy.render.paint import single_code, uniform_state
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.split import ColourPart, Paint

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

#: An output triangle is its source triangle when every vertex is within this, mm (§7).
VERBATIM_MM = 1e-4
#: How near a source triangle another triangle must be to take its colour, mm (§7).
NEAR_MM = 0.01
PAINT_SUFFIX = ".paint.json"
SOURCE_NAME = "model.scad"
HAND_EDITED = (
    "model.scad was edited by hand since the scene compiled it, so no part's painting is "
    "carried: each part prints in its own colour"
)
#: What phases 1-2 noted on a painted object (`ingest.py`); dropped now paint is carried.
OLD_PAINT_NOTE = "the paint is not carried into renders or prints until phase 3"


def paint_file(part: Part) -> str:
    """Where a painted part's codes are kept beside its mesh (`ingest.py`)."""
    return part.mesh.removesuffix(".3mf") + PAINT_SUFFIX


def _floats(v: Sequence[Num]) -> list[float]:
    return [value(x) for x in v]


def placement(obj: SceneObject) -> tuple[FloatArray, FloatArray]:
    """The object's linear part and translation, mesh frame to world (`frames.py`)."""
    t = obj.transform
    return linear(_floats(t.rotate), _floats(t.scale)), np.asarray(_floats(t.translate), dtype=float)


def mirrored(obj: SceneObject) -> bool:
    return float(np.linalg.det(placement(obj)[0])) < 0


def with_params(scene: Scene, params: Mapping[str, ParamValue]) -> Scene:
    """The scene with each exposed number at the render's value for it (a number only;
    a parameter the render does not set keeps the scene's), so a part is looked for
    where it rendered."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if set(node) == {"expose", "value"}:
                given = params.get(node["expose"])
                if isinstance(given, int | float) and not isinstance(given, bool):
                    return {"expose": node["expose"], "value": float(given)}
                return node
            return {key: walk(item) for key, item in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return Scene.model_validate(walk(scene.model_dump(mode="json")))


def paint_warnings(scene: Scene) -> list[str]:
    """Where an object's painting cannot be carried whole: a mirrored object's triangles
    render wound the other way, so none is its source's verbatim (rule 1)."""
    return [
        f"{obj.id} ({obj.name}) is mirrored, so its painting is carried only where a whole "
        "triangle is one colour; finer painting prints in the part's own colour"
        for obj in scene.objects
        if mirrored(obj) and any(p.painted and not p.negative for p in obj.parts)
    ]


def scene_notes(scene: Scene) -> list[str]:
    """The notes `get_scene` shows: the scene's own, less phases 1-2's "not carried until
    phase 3", and the paint warnings."""
    return [note for note in scene.notes if OLD_PAINT_NOTE not in note] + paint_warnings(scene)


@dataclass(frozen=True)
class Source:
    """The triangles a rendered part may come from, in world coordinates, each with its
    code (``""`` unpainted), the codes numbered against ``palette``."""

    vertices: FloatArray
    faces: IntArray
    codes: tuple[str, ...]
    palette: tuple[str, ...]


@dataclass(frozen=True)
class CarriedPaint:
    """A rendered part's painting: its faces' codes and the colours they number, and
    each face's turn: its vertices ``np.roll(face, -turn)`` start where its source's do."""

    paint: Paint
    turns: IntArray


def transfer(output: trimesh.Trimesh, source: Source) -> tuple[list[str], IntArray, int]:
    """Each of ``output``'s faces' code and turn, by the module docstring's rules, and
    how many faces were carried verbatim."""
    vertices = np.asarray(output.vertices, dtype=np.float64)
    faces = np.asarray(output.faces, dtype=np.int64)
    codes = [""] * len(faces)
    turns = np.zeros(len(faces), dtype=np.int64)
    if len(faces) == 0 or len(source.faces) == 0:
        return codes, turns, 0
    ids = match_points(source.vertices, vertices, VERBATIM_MM)[faces]
    whole = np.flatnonzero((ids >= 0).all(axis=1))
    found = np.full(len(faces), -1, dtype=np.int64)
    if len(whole):
        # The source triangle with the same three vertices, whatever their order.
        known = np.sort(source.faces, axis=1)
        _, group = np.unique(
            np.concatenate([known, np.sort(ids[whole], axis=1)]), axis=0, return_inverse=True
        )
        group = group.reshape(-1)
        first = np.full(int(group.max()) + 1, -1, dtype=np.int64)
        first[group[: len(known)][::-1]] = np.arange(len(known))[::-1]
        found[whole] = first[group[len(known) :]]
    matched = np.flatnonzero(found >= 0)
    own = source.faces[found[matched]]
    seen = ids[matched]
    turn = (seen == own[:, :1]).argmax(axis=1)
    same = seen[np.arange(len(matched)), (turn + 1) % 3] == own[:, 1]
    verbatim = matched[same]
    turns[verbatim] = turn[same]
    for row, face in zip(verbatim.tolist(), found[verbatim].tolist(), strict=True):
        codes[row] = source.codes[face]
    rest = np.setdiff1d(np.arange(len(faces)), verbatim)
    if len(rest):
        one_colour: dict[str, str] = {}

        def whole_colour(code: str) -> str:
            if code not in one_colour:
                state = uniform_state(code)
                one_colour[code] = "" if state is None else single_code(state)
            return one_colour[code]

        centres = vertices[faces[rest]].mean(axis=1)
        nearest, _ = nearest_triangles(centres, source.vertices[source.faces], NEAR_MM)
        for row, face in zip(rest.tolist(), nearest.tolist(), strict=True):
            if face >= 0:
                codes[row] = whole_colour(source.codes[face])
    return codes, turns, len(verbatim)


def _source(
    directory: Path, members: Sequence[tuple[SceneObject, Part]], mesh_of: MeshOf
) -> tuple[Source, list[str]]:
    """The members' triangles placed in the world, their codes renumbered into one
    palette (the paintings' colours, first seen first), and what could not be read."""
    paints: dict[str, Paint] = {}
    for _, part in members:
        if part.painted and part.mesh not in paints:
            raw = json.loads((directory / paint_file(part)).read_text(encoding="utf-8"))
            paints[part.mesh] = Paint(tuple(raw["codes"]), tuple(raw["colours"]))
    palette = tuple(dict.fromkeys(c.upper() for p in paints.values() for c in p.colours))
    numbered = {mesh: paint.renumbered(palette) for mesh, paint in paints.items()}
    vertices: list[FloatArray] = []
    faces: list[IntArray] = []
    codes: list[str] = []
    problems: list[str] = []
    offset = 0
    for obj, part in members:
        mesh = mesh_of(part.mesh)
        m, t = placement(obj)
        vertices.append(np.asarray(mesh.vertices, dtype=np.float64) @ m.T + t)
        faces.append(np.asarray(mesh.faces, dtype=np.int64) + offset)
        offset += len(mesh.vertices)
        own = numbered.get(part.mesh)
        if own is not None and len(own.codes) != len(mesh.faces):
            problems.append(
                f"{obj.id} {part.id}: its painting is for another mesh, so it prints in its "
                "own colour"
            )
            own = None
        codes += list(own.codes) if own is not None else [""] * len(mesh.faces)
    source = Source(np.concatenate(vertices), np.concatenate(faces), tuple(codes), palette)
    return source, problems


def carry_paint(
    directory: Path, params: Mapping[str, ParamValue], parts: Sequence[ColourPart]
) -> tuple[list[CarriedPaint | None], list[str]]:
    """For each of ``parts`` (one render of the model in ``directory``, a part per
    colour), the painting its colour's painted scene parts carry onto it, or None; and
    the warnings the render gives. A directory with no scene, or a scene with no painted
    part, gives nothing; one whose model.scad was edited by hand gives the warning."""
    carried: list[CarriedPaint | None] = [None] * len(parts)
    path = directory / SCENE_NAME
    if not path.is_file():
        return carried, []
    scene = load_scene(path.read_text(encoding="utf-8"))
    if not any(p.painted and not p.negative for o in scene.objects for p in o.parts):
        return carried, []
    if text_sha256((directory / SOURCE_NAME).read_text(encoding="utf-8")) != scene.generated_sha256:
        return carried, [HAND_EDITED]
    scene = with_params(scene, params)
    mesh_of = part_loader(directory)
    warnings = paint_warnings(scene)
    for index, part in enumerate(parts):
        if part.negative:
            continue
        colour = part.colour.upper()
        members = [
            (obj, p)
            for obj in scene.objects
            for p in obj.parts
            if not p.negative and p.colour == colour
        ]
        if not any(p.painted for _, p in members):
            continue
        source, problems = _source(directory, members, mesh_of)
        warnings += problems
        codes, turns, _ = transfer(part.mesh, source)
        if any(codes):
            carried[index] = CarriedPaint(Paint(tuple(codes), source.palette), turns)
    return carried, warnings


def turned(mesh: trimesh.Trimesh, turns: IntArray) -> trimesh.Trimesh:
    """``mesh`` with each face's vertices turned by its turn: the same triangles, the
    same winding, each starting where its source triangle does."""
    faces = np.asarray(mesh.faces, dtype=np.int64)
    order = (np.arange(3)[None, :] + turns[:, None]) % 3
    return trimesh.Trimesh(
        vertices=mesh.vertices, faces=np.take_along_axis(faces, order, axis=1), process=False
    )


def write_carried(path: Path, carried: CarriedPaint) -> None:
    """The carried painting beside a render's layout (`render/jobs.py` `PartSource.paint`)."""
    document = {
        "codes": list(carried.paint.codes),
        "colours": list(carried.paint.colours),
        "turns": "".join(str(turn) for turn in carried.turns.tolist()),
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def read_carried(path: Path, mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, Paint]:
    """What :func:`write_carried` wrote, applied to the part's ``mesh`` as it was re-read
    from the render: its faces turned, and its painting."""
    document = json.loads(path.read_text(encoding="utf-8"))
    codes = tuple(str(code) for code in document["codes"])
    turns = np.array([int(turn) for turn in document["turns"]], dtype=np.int64)
    if len(codes) != len(mesh.faces) or len(turns) != len(mesh.faces):
        raise ValueError(f"{path.name} is the painting of another mesh")
    return turned(mesh, turns), Paint(codes, tuple(document["colours"]))
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_paint.py -v && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .`
Expected: PASS and clean. If ruff format reflows a test's long lines, run `uv run --frozen ruff format tests/meshmodel/test_paint.py` and re-run.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/meshmodel/meshes.py backend/scadbuddy/meshmodel/paint.py backend/tests/meshmodel/test_paint.py
git commit -m "feat(meshmodel): carry a part's painting onto its rendered solid (#2146)"
```

---

### Task 4: The render's `paint` stage

**Files:**
- Modify: `backend/scadbuddy/render/jobs.py` (`PartSource`, `PlateLayout.save`/`load`, new `PAINT_FILE` and `paint_layout`, `_render_solids`)
- Modify: `backend/scadbuddy/core/metrics.py:38` (`RenderStage`)
- Test: `backend/tests/test_jobs.py`, `backend/tests/test_worker.py:193`

**Interfaces:**
- Consumes: `carry_paint`, `read_carried`, `turned`, `write_carried`, `CarriedPaint` (Task 3).
- Produces: `PartSource(file: str, solid: bool = False, paint: str | None = None)`; `PAINT_FILE = "paint-{plate}-{part}.json"`; `paint_layout(layout: PlateLayout, scad: Path, params: Mapping[str, ParamValue], work: Path) -> PlateLayout`; `RenderStage` includes `"paint"`. A layout entry may carry `"paint": "<file>"`; `PlateLayout.load` reads the part's painting and turned faces from it.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_jobs.py` (imports at the top: `from scadbuddy.render.objects3mf import read_objects`, `from scadbuddy.render.read_budget import ReadBudget`, `from scadbuddy.render.solids import solid_file, solid_mesh`, `from scadbuddy.render.split import Paint`, `from scadbuddy.render.jobs import PartSource`, and `from tests.meshmodel.test_paint import GREEN, ORANGE, painted_box, rendered, written`):

```python
async def test_a_mesh_models_painting_reaches_its_3mf(paths: DataPaths) -> None:
    """Spec 2026-10-11 §7: the solid of a painted part's colour carries the part's codes
    into model.3mf, through the layout that `finish_piece` and an output re-read; the
    colours the painting uses get filaments."""
    model_dir = paths.model_dir("demo")
    model_dir.mkdir(parents=True)
    mesh, codes = painted_box()
    read = [ReadObject("Box", 1, [ColourPart(0, "Body", ORANGE, mesh, Paint(codes, (ORANGE, GREEN)))])]
    scene = written(model_dir, read)
    world = rendered(model_dir, scene, ORANGE)

    async def render(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Body", f"{ORANGE}00", world)])
        return mock.Mock(
            log_tail=[], missing_files=(), diagnostics=(), diagnostics_dropped=0, notes=(), plates=None
        )

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return CustomizerSchema()

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        colours, work_dir = args[3], args[4]
        assert isinstance(colours, list) and isinstance(work_dir, Path)
        meshes = {}
        for index, colour in enumerate(colours, start=1):
            path = solid_file(work_dir, index)
            write_openscad_3mf(path, [("Body", f"{colour}00", world)])
            found = solid_mesh(path)
            assert found is not None
            meshes[colour] = found
        return SolidRender(meshes=meshes)

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        result, _ = await jobs.render_job(_job("p"), config=CONFIG, paths=paths, assets=AssetStore(paths.assets))

    assert result.colors == [ORANGE, GREEN]
    assert [(p.colour, p.extruder) for p in result.parts] == [(ORANGE, 1), (GREEN, 2)]
    [obj] = read_objects((paths.root / result.model_3mf).read_bytes(), ReadBudget())
    [part] = obj.parts
    assert part.paint is not None and part.paint.codes == codes
    layout = PlateLayout.load(paths.cache / "render-job" / "p" / LAYOUT_NAME)
    [loaded] = layout.plates[0].parts
    assert loaded.paint == Paint(codes, (ORANGE, GREEN))
    assert layout.sources[0][0].paint == "paint-1-1.json"


def test_a_layout_part_without_paint_is_saved_as_before(tmp_path: Path) -> None:
    """No "paint" key unless a part carries paint: a layout written before still loads."""
    mesh = trimesh.creation.box((1, 1, 1))
    raw = tmp_path / RAW_RENDER_NAME
    write_openscad_3mf(raw, [("Color 1", "#FF000000", mesh)])
    part = ColourPart(1, "Color 1", "#FF0000", mesh)
    layout = PlateLayout(
        [jobs.single_plate([part])],
        ["#FF0000"],
        [],
        BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        [(PartSource(RAW_RENDER_NAME),)],
    )
    layout.save(tmp_path / LAYOUT_NAME)
    entry = json.loads((tmp_path / LAYOUT_NAME).read_text())["plates"][0]["parts"][0]
    assert "paint" not in entry
    assert PlateLayout.load(tmp_path / LAYOUT_NAME).plates[0].parts[0].paint is None
```

`jobs.single_plate` is the `single_plate` `jobs.py` imports from `bambu3mf`; `ReadObject` comes from `scadbuddy.render.objects3mf` (add it to that import).

In `backend/tests/test_worker.py:193`, add the stage:

```python
    for stage in ("source", "render", "split", "solids", "paint", "thumbnail", "write"):
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_jobs.py -k "painting_reaches or without_paint" -v`
Expected: FAIL: `test_a_mesh_models_painting_reaches_its_3mf` with `part.paint is None` (nothing carries it yet); the second with `TypeError` from `PartSource` once it is constructed with no `paint` field read back (or PASS; it pins the old shape either way).

- [ ] **Step 3: Implement**

In `backend/scadbuddy/core/metrics.py`:

```python
RenderStage = Literal["source", "render", "split", "solids", "paint", "thumbnail", "write"]
```

In `backend/scadbuddy/render/jobs.py`, import `from scadbuddy.meshmodel.paint import carry_paint, read_carried, turned, write_carried`. Change `PartSource`:

```python
@dataclass(frozen=True)
class PartSource:
    """Where a layout part's mesh is on disk, relative to the render's directory:
    a closed solid's own file, or the material of a split render; and, for a mesh
    model's painted part, the file of the painting carried onto it (#2146)."""

    file: str
    solid: bool = False
    paint: str | None = None
```

In `PlateLayout.save`, the part entry gains the key only when set:

```python
                    {
                        "material_index": part.material_index,
                        "name": part.name,
                        "colour": part.colour,
                        "file": source.file,
                        "solid": source.solid,
                        **({"negative": True} if part.negative else {}),
                        **({"paint": source.paint} if source.paint is not None else {}),
                    }
```

In `PlateLayout.load`'s `part`, read it, after the mesh and split paint are found:

```python
            source = PartSource(entry["file"], entry["solid"], entry.get("paint"))
            if source.solid:
                ...  # unchanged
            else:
                ...  # unchanged
            if source.paint is not None:
                mesh, paint = read_carried(work / source.paint, mesh)
```

Add after `result_plates`:

```python
#: A painted part's carried painting beside the layout (spec 2026-10-11 §7): plate, part.
PAINT_FILE = "paint-{plate}-{part}.json"


def paint_layout(
    layout: PlateLayout, scad: Path, params: Mapping[str, ParamValue], work: Path
) -> PlateLayout:
    """A mesh model's painting carried onto its render (#2146, spec 2026-10-11 §7): each
    painted part's codes written beside the layout and named by its `PartSource`, its
    faces turned to start where their source triangles do, the colours its painting
    prints with added to the filament list, and the transfer's warnings. Anything but a
    one-plate render of a model's own model.scad comes back as it was; a model with no
    scene, or no painted part, too (`carry_paint`)."""
    if scad.name != SOURCE_NAME or len(layout.plates) != 1:
        return layout
    plate, sources = layout.plates[0], layout.sources[0]
    carried, warnings = carry_paint(scad.parent, params, plate.parts)
    if not any(found is not None for found in carried) and not warnings:
        return layout
    parts, placed, colours = list(plate.parts), list(sources), list(layout.colours)
    for index, found in enumerate(carried):
        if found is None:
            continue
        name = PAINT_FILE.format(plate=1, part=index + 1)
        write_carried(work / name, found)
        parts[index] = replace(parts[index], mesh=turned(parts[index].mesh, found.turns), paint=found.paint)
        placed[index] = replace(placed[index], paint=name)
        for colour in found.paint.used():
            if colour.upper() not in {c.upper() for c in colours}:
                colours.append(colour.upper())
    return replace(
        layout,
        plates=[replace(plate, parts=tuple(parts))],
        colours=colours,
        warnings=[*layout.warnings, *warnings],
        sources=[tuple(placed)],
    )
```

In `_render_solids`, between the `solids` stage and `layout.save`:

```python
    with stage("paint"):
        # A mesh model's painting, onto the parts the layout names (spec 2026-10-11 §7).
        layout = await asyncio.to_thread(paint_layout, layout, prepared.scad, params, work)
    layout.save(work / LAYOUT_NAME)
    return layout
```

(`params` there is the job's own parameters, `_render_solids`' fourth argument; `SOURCE_NAME` is already imported from `core.paths`.)

- [ ] **Step 4: Run them to see them pass, with the render and worker tests**

Run: `cd backend && uv run --frozen pytest tests/test_jobs.py tests/test_worker.py tests/meshmodel -v && uv run --frozen mypy`
Expected: PASS (`test_worker.py` needs Postgres and Temporal; it skips without them, so run it where they are), mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/render/jobs.py backend/scadbuddy/core/metrics.py backend/tests/test_jobs.py backend/tests/test_worker.py
git commit -m "feat(render): a paint stage carries a mesh model's painting into its 3MF (#2146)"
```

---

### Task 5: Scene notes say what paint does now

**Files:**
- Modify: `backend/scadbuddy/meshmodel/ingest.py` (docstring; drop the "until phase 3" note)
- Modify: `backend/scadbuddy/api/mesh_models.py` (`get_scene` notes)
- Test: `backend/tests/meshmodel/test_ingest.py:83-96`, `backend/tests/api/test_mesh_models.py`

**Interfaces:**
- Consumes: `scene_notes` (Task 3).
- Produces: `GET /models/{slug}/scene`'s `notes` = `scene_notes(scene)`.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/meshmodel/test_ingest.py`, replace the end of `test_a_painted_part_keeps_its_codes_beside_its_mesh`:

```python
    assert scene.objects[0].parts[0].painted
    assert json.loads(files["meshes/o1-p1.paint.json"])["codes"][-1] == "4"
    # The render carries the paint (meshmodel/paint.py), so no note says it does not.
    assert scene.notes == []
```

In `backend/tests/api/test_mesh_models.py` (imports: `import tempfile`, `from pathlib import Path`, `from scadbuddy.render.bambu3mf import PlateParts, write_plates_3mf`, `from scadbuddy.render.split import ColourPart, Paint`):

```python
def painted_3mf() -> bytes:
    """The carrot as Bambu Studio would save it painted: every other face green."""
    mesh = carrot()
    codes = tuple("8" if i % 2 else "" for i in range(len(mesh.faces)))
    part = ColourPart(0, "Carrot", "#F08A24", mesh, Paint(codes, ("#F08A24", "#3A7D2C")))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "carrot.3mf"
        write_plates_3mf([PlateParts((part,), (1,))], ["#F08A24", "#3A7D2C"], path, thumbnails=None)
        return path.read_bytes()


def test_a_painted_scene_notes_only_where_its_painting_is_not_carried_whole(
    client: TestClient,
) -> None:
    slug = upload(client, data=painted_3mf(), filename="carrot.3mf").json()["slug"]
    scene = client.get(f"/api/v1/models/{slug}/scene").json()
    assert scene["scene"]["objects"][0]["parts"][0]["painted"] is True
    assert scene["notes"] == []
    assert edit(client, slug, {"edit": "transform", "object": "o1", "scale": [-1, 1, 1]}).status_code == 200
    [note] = client.get(f"/api/v1/models/{slug}/scene").json()["notes"]
    assert note.startswith("o1 (") and "is mirrored" in note
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_ingest.py tests/api/test_mesh_models.py -k "painted" -v`
Expected: FAIL: the ingest test sees the phase-1 note; the API test sees it too, and no mirror note.

- [ ] **Step 3: Implement**

In `backend/scadbuddy/meshmodel/ingest.py`, the module docstring's last sentence becomes "A painted part's codes are kept beside its mesh, and the render carries them (`paint.py`, spec §7).", and delete the block

```python
        if any(p.painted for p in parts):
            # Nothing reads the .paint.json files until paint transfer (spec §7, phase 3):
            # OpenSCAD's import drops paint, so renders and prints carry none.
            notes.append(
                f"{obj.name}: painted in Bambu Studio; the paint is not carried into renders "
                "or prints until phase 3, so each part prints in its own colour"
            )
```

In `backend/scadbuddy/api/mesh_models.py`, import `from scadbuddy.meshmodel.paint import scene_notes` and in `get_scene`'s `read`:

```python
        return SceneView(
            scene=scene, notes=scene_notes(scene), hand_edited=hand_edited(directory, scene)
        )
```

and `SceneView.notes` gains a comment:

```python
    #: What reading the file dropped, and where a painted object's painting is not
    #: carried whole (meshmodel/paint.py `scene_notes`).
    notes: list[str]
```

- [ ] **Step 4: Run them to see them pass**

Run: `cd backend && uv run --frozen pytest tests/meshmodel tests/api/test_mesh_models.py -v`
Expected: PASS (the API tests need Postgres, git and Temporal).

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/meshmodel/ingest.py backend/scadbuddy/api/mesh_models.py backend/tests/meshmodel/test_ingest.py backend/tests/api/test_mesh_models.py
git commit -m "feat(meshmodel): scene notes name a mirrored painted object, not phase 3 (#2146)"
```

---

### Task 6: Acceptance — the Carrot Garden's painted leaves print green

**Files:**
- Create: `backend/tests/test_mesh_models_paint.py`

**Interfaces:**
- Consumes: `render_once` (`tests/conftest.py`), `opened` (`tests/test_mesh_models_fixtures.py`), `part_loader`, `paint_file` (Task 3), `compile_scene`, `apply_edits`, `measure`, `read_objects`.
- Produces: nothing later tasks import.

- [ ] **Step 1: Write the tests**

```python
# backend/tests/test_mesh_models_paint.py
"""Paint transfer on the real Carrot Garden (#2146, spec 2026-10-11 §7, §10): Bambuddy
library file 688 rendered through the production pipeline (`render_job` into the image's
openscad, `tests/conftest.py` `render_once`) carries its painting into the 3MF. Re-measures
meshmodel/paint.py's docstring on the pinned image; tens of seconds each."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np
import pytest
from pydantic import TypeAdapter

from scadbuddy.core.paths import DataPaths
from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.edits import Edit, apply_edits
from scadbuddy.meshmodel.meshes import part_loader
from scadbuddy.meshmodel.paint import paint_file
from scadbuddy.meshmodel.scene import SCENE_NAME, Scene, dump_scene, text_sha256, value
from scadbuddy.meshmodel.section import Section, measure
from scadbuddy.render.objects3mf import read_objects
from scadbuddy.render.read_budget import ReadBudget
from tests.conftest import render_once
from tests.test_mesh_models_fixtures import CARROT, GREEN, opened

pytestmark = [pytest.mark.requires_openscad, pytest.mark.requires_fixtures]
EDITS = TypeAdapter(list[Edit])
SLUG = "carrot-garden"


def compiled_into(directory: Path, scene: Scene) -> None:
    text = compile_scene(scene)
    (directory / "model.scad").write_text(text)
    scene = scene.model_copy(update={"generated_sha256": text_sha256(text)})
    (directory / SCENE_NAME).write_text(dump_scene(scene))


def source_codes(directory: Path, scene: Scene) -> tuple[Counter[str], tuple[str, ...]]:
    """Every painted source face's code, and the palette they number."""
    codes: Counter[str] = Counter()
    palette: tuple[str, ...] = ()
    seen: set[str] = set()
    for obj in scene.objects:
        for part in obj.parts:
            if part.painted and part.mesh not in seen:
                seen.add(part.mesh)
                raw = json.loads((directory / paint_file(part)).read_text())
                codes.update(code for code in raw["codes"] if code)
                palette = tuple(raw["colours"])
    return codes, palette


def printed_codes(model_3mf: Path, palette: tuple[str, ...]) -> Counter[str]:
    """Every painted face of the 3MF, its code numbered against the source's palette (the
    writer numbers codes against the file's own filament list)."""
    found: Counter[str] = Counter()
    for obj in read_objects(model_3mf.read_bytes(), ReadBudget()):
        for part in obj.parts:
            if part.paint is not None:
                found.update(code for code in part.paint.renumbered(palette).codes if code)
    return found


def neck(directory: Path, scene: Scene, name: str) -> tuple[str, float, Section]:
    """The object named ``name``, the height of its neck and the section there: as
    test_mesh_models_fixtures.py finds it, the narrowest section from 8 mm below the
    lowest painted face to 4 mm above it."""
    obj = next(o for o in scene.objects if o.name.startswith(name))
    mesh = part_loader(directory)(obj.parts[0].mesh)
    codes = json.loads((directory / paint_file(obj.parts[0])).read_text())["codes"]
    painted = np.array([bool(code) for code in codes])
    leaves_from = float(mesh.triangles_center[painted][:, 2].min())
    sections = {
        float(z): measure(mesh, [0.0, 0.0, float(z)], [0.0, 0.0, 1.0])
        for z in np.arange(leaves_from - 8.0, leaves_from + 4.0, 0.5)
    }
    z, found = min(
        ((z, s) for z, s in sections.items() if s is not None and s.inside),
        key=lambda zs: zs[1].room,
    )
    return obj.id, z, found


async def test_the_carrot_gardens_painting_arrives_whole(tmp_path: Path) -> None:
    """Op-free (spec §10): every painted face's code is in the 3MF, verbatim."""
    paths = DataPaths(tmp_path)
    paths.ensure()
    directory = paths.model_dir(SLUG)
    directory.mkdir(parents=True)
    _, scene = opened(CARROT, directory)
    compiled_into(directory, scene)

    _, result = await render_once(paths, SLUG, {})

    expected, palette = source_codes(directory, scene)
    assert sum(expected.values()) == 13_873  # manifest.json painted_triangles
    assert printed_codes(paths.root / result.model_3mf, palette) == expected
    assert GREEN in result.colors


async def test_the_anchored_carrots_painted_leaves_print_green(tmp_path: Path) -> None:
    """The user's case (#2146): the small carrot anchored with a green cylinder through
    its neck (phase 2's coloured add) renders with its leaves' green paint on every face
    the anchor left as it was, and the anchor and the paint share one green filament."""
    paths = DataPaths(tmp_path)
    paths.ensure()
    directory = paths.model_dir(SLUG)
    directory.mkdir(parents=True)
    _, scene = opened(CARROT, directory)
    oid, z, section = neck(directory, scene, "Small Carrot")
    small = next(o for o in scene.objects if o.id == oid)
    assert [value(v) for v in small.transform.rotate] == [0.0, 0.0, 0.0]
    assert [value(v) for v in small.transform.scale] == [1.0, 1.0, 1.0]
    centre = section.centroid
    radius = section.room + 1.5
    at = [float(centre[0]) + value(small.transform.translate[0]), float(centre[1]) + value(small.transform.translate[1]), z + value(small.transform.translate[2])]
    anchored = apply_edits(
        scene,
        EDITS.validate_python(
            [
                {
                    "edit": "add",
                    "object": oid,
                    "primitive": "cylinder",
                    "size": [2 * radius, 2 * radius, 6],
                    "at": at,
                    "colour": GREEN,
                }
            ]
        ),
        part_loader(directory),
    )
    compiled_into(directory, anchored.model_copy(update={"objects": [o for o in anchored.objects if o.id == oid]}))

    _, result = await render_once(paths, SLUG, {})

    mesh = part_loader(directory)(small.parts[0].mesh)
    codes = json.loads((directory / paint_file(small.parts[0])).read_text())["codes"]
    corners = mesh.triangles
    off_axis = np.hypot(corners[:, :, 0] - centre[0], corners[:, :, 1] - centre[1])
    clear = (
        (off_axis > radius + 1e-3) | (corners[:, :, 2] < z - 3 - 1e-3) | (corners[:, :, 2] > z + 3 + 1e-3)
    ).all(axis=1)
    expected = Counter(code for code, ok in zip(codes, clear, strict=True) if ok and code)
    _, palette = source_codes(directory, scene)
    printed = printed_codes(paths.root / result.model_3mf, palette)
    # Measured 2026-10-13: exactly these 5,639 of the 6,319 painted faces.
    assert expected <= printed
    assert sum(printed.values()) <= sum(1 for code in codes if code)
    assert any(code == "8" for code in printed)  # whole-green leaf faces (extruder 2: green)
    assert result.colors.count(GREEN) == 1
```

- [ ] **Step 2: Run them in the image**

Run:
```bash
docker build --target test -t scadbuddy:test .
docker run --rm -v "$PWD/test-fixtures:/app/test-fixtures:ro" -e SCADBUDDY_TEST_FIXTURES=/app/test-fixtures scadbuddy:test \
  uv run --frozen --no-sync pytest tests/test_mesh_models_paint.py -v
```
Expected: PASS, two tests, each well under a minute. (Locally without `openscad` both skip: `uv run --frozen pytest tests/test_mesh_models_paint.py -v` shows `SKIPPED`.) If the first fails on the count, read `printed - expected` and `expected - printed` before changing anything: a drop means the image's export changed (re-measure, and say so in `meshmodel/paint.py`'s docstring), not that the tolerance needs loosening.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_mesh_models_paint.py
git commit -m "test(meshmodel): the Carrot Garden's painted leaves print green, anchored or not (#2146)"
```

---

### Task 7: A section's area at every layer, vectorised

**Files:**
- Modify: `backend/scadbuddy/meshmodel/section.py` (add `layer_areas`)
- Test: `backend/tests/meshmodel/test_section.py`

**Interfaces:**
- Produces: `layer_areas(vertices: np.ndarray, faces: np.ndarray, zs: np.ndarray) -> np.ndarray` — the level section's area at each of the ascending heights `zs`, equal to `measure(mesh, (0, 0, z), (0, 0, 1)).area` (0 where `measure` gives None).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/meshmodel/test_section.py` (add `layer_areas` to its import from `scadbuddy.meshmodel.section`):

```python
def test_layer_areas_are_measures_areas() -> None:
    carrot = neck()
    zs = np.arange(0.1, 45.0, 0.2)
    areas = layer_areas(np.asarray(carrot.vertices), np.asarray(carrot.faces), zs)
    expected = []
    for z in zs:
        section = measure(carrot, (0, 0, float(z)), (0, 0, 1))
        expected.append(0.0 if section is None else section.area)
    assert np.allclose(areas, expected, atol=1e-9)


def test_layer_areas_are_zero_off_the_mesh() -> None:
    box = trimesh.creation.box((10, 10, 10))
    areas = layer_areas(np.asarray(box.vertices), np.asarray(box.faces), np.array([-6.0, -4.9, 0.0, 4.9, 6.0]))
    assert np.allclose(areas, [0, 100, 100, 100, 0])


def test_layer_areas_of_nothing() -> None:
    assert layer_areas(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), np.array([1.0])).tolist() == [0.0]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_section.py -k layer_areas -v`
Expected: FAIL with `ImportError: cannot import name 'layer_areas'`.

- [ ] **Step 3: Implement**

Append to `backend/scadbuddy/meshmodel/section.py`:

```python
def layer_areas(vertices: np.ndarray, faces: np.ndarray, zs: np.ndarray) -> np.ndarray:
    """The level section's area at each of the ascending heights ``zs`` (weak joints,
    spec 2026-10-11 §9): :func:`measure`'s Green's-theorem sum over the segments each face
    leaves on the plane, oriented the same way, but every plane at once, each face
    visited only for the planes it crosses (``lo < z <= hi``). Measured 2026-10-13: within
    5e-13 mm² of `measure` on the test carrot, and the Carrot Garden's whole profile in
    milliseconds where `measure` per layer took seconds."""
    areas = np.zeros(len(zs))
    if len(faces) == 0 or len(zs) == 0:
        return areas
    corners = vertices[faces]
    heights = corners[:, :, 2]
    first = np.searchsorted(zs, heights.min(axis=1), side="right")
    last = np.searchsorted(zs, heights.max(axis=1), side="right")
    count = np.maximum(last - first, 0)
    total = int(count.sum())
    if total == 0:
        return areas
    face = np.repeat(np.arange(len(faces)), count)
    plane = np.repeat(first, count) + np.arange(total) - np.repeat(np.cumsum(count) - count, count)
    tri = corners[face]
    side = tri[:, :, 2] - zs[plane][:, None]
    starts, ends = np.array([0, 1, 2]), np.array([1, 2, 0])
    crossing = (side[:, starts] >= 0) != (side[:, ends] >= 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = side[:, starts] / (side[:, starts] - side[:, ends])
        points = tri[:, starts] + t[:, :, None] * (tri[:, ends] - tri[:, starts])
    # The two edges the plane crosses, in edge order.
    pick = np.argsort(~crossing, axis=1, kind="stable")[:, :2]
    ends_xy = np.take_along_axis(points, pick[:, :, None], axis=1)[:, :, :2]
    a, b = ends_xy[:, 0], ends_xy[:, 1]
    # As `_segments`: interior on the left by the face's outward normal.
    normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    d = b - a
    flip = (-d[:, 1] * normal[:, 0] + d[:, 0] * normal[:, 1]) > 0
    a, b = np.where(flip[:, None], b, a), np.where(flip[:, None], a, b)
    np.add.at(areas, plane, (a[:, 0] * b[:, 1] - b[:, 0] * a[:, 1]) / 2)
    return areas
```

- [ ] **Step 4: Run them to see them pass**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_section.py -v && uv run --frozen mypy`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/meshmodel/section.py backend/tests/meshmodel/test_section.py
git commit -m "feat(meshmodel): every layer's section area in one pass (#2146)"
```

---

### Task 8: `weak_joints` in `analyze_geometry`

**Files:**
- Modify: `backend/scadbuddy/render/geometry.py` (module docstring, constants, `JointSuggestion`, `SplitSuggestion`, `WeakJoint`, `GeometryAnalysis.weak_joints`, `_pieces`, `_suggestion`, `weak_joints`, `analyze_geometry`, `ANALYSIS_VERSION = 3`)
- Test: `backend/tests/test_geometry.py`, `backend/tests/test_mesh_models_fixtures.py`

**Interfaces:**
- Consumes: `layer_areas`, `measure`, `Section` (Task 7, `meshmodel/section.py`); `match_points` (Task 2); `WALL`, `JointSpec` (`meshmodel/edits.py`); `_islands`, `_welded`, `_point`, `_box` (same module).
- Produces: `WEAK_LAYER_MM = 0.2`, `WEAK_WINDOW_MM = 2.0`, `WEAK_RATIO = 0.25`, `WEAK_MIN_VOLUME_MM3 = 1000.0`; models `JointSuggestion(diameter, depth, clearance, key, peg_on)`, `SplitSuggestion(edit="split_at_plane", object: str | None, point, normal, joint: JointSuggestion | None)` (its `model_dump()` is a valid `edit_scene` edit once `object` is set), `WeakJoint(z, area_mm2, neighbour_area_mm2, volume_above_mm3, centroid, room_mm, parts: list[int], bbox, suggestion)`; `GeometryAnalysis.weak_joints: list[WeakJoint]`; `weak_joints(parts, welded, numbers, split) -> list[WeakJoint]`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_geometry.py` (imports: `from pydantic import TypeAdapter`, `from scadbuddy.meshmodel.edits import Edit, apply_edits`, `from tests.meshmodel.test_section import neck`):

```python
EDITS = TypeAdapter(list[Edit])


def revolved(profile: list[tuple[float, float]]) -> trimesh.Trimesh:
    return trimesh.creation.revolve(np.array(profile, dtype=float), sections=64)


def test_a_carrots_neck_is_a_weak_joint_with_a_split_that_fits() -> None:
    """Spec 2026-10-11 §9 on the test carrot (body r 10 to z 30, neck r 3 to 35, tuft r 6
    to 45): the neck is a quarter of the body below it, with 1.2 cm³ above."""
    [joint] = analyze_geometry([_part(neck())]).weak_joints
    assert 30 < joint.z < 35
    assert joint.area_mm2 < 0.25 * joint.neighbour_area_mm2
    assert joint.volume_above_mm3 >= 1000
    assert joint.room_mm == pytest.approx(3 * math.cos(math.pi / 64), rel=1e-6)
    suggestion = joint.suggestion
    assert suggestion.object is None
    assert suggestion.point == pytest.approx((0, 0, joint.z), abs=1e-6)
    assert suggestion.normal == (0, 0, 1)
    assert suggestion.joint is not None
    assert suggestion.joint.diameter == round(1.2 * joint.room_mm, 2)
    assert suggestion.joint.peg_on == "above"  # the socket goes into the wide body
    # The suggestion is an edit_scene edit once it names its object.
    [edit] = EDITS.validate_python([{**suggestion.model_dump(), "object": "o1"}])
    assert edit.edit == "split_at_plane"


def test_two_colours_printed_as_one_piece_are_measured_together() -> None:
    """The demo's carrot: an orange body and a green neck and tuft sharing the z = 30
    face. Apart, neither has a weak section; together the neck is one."""
    body = revolved([(0, 0), (10, 0), (10, 30), (3, 30), (0, 30)])
    leaves = revolved([(0, 30), (3, 30), (3, 35), (6, 35), (6, 45), (0, 45)])
    analysis = analyze_geometry([_part(body, "#F08A24", 1), _part(leaves, "#3A7D2C", 2)])
    [joint] = analysis.weak_joints
    assert joint.parts == [1, 2]
    assert 30 < joint.z < 35


def test_two_carrots_are_two_weak_joints() -> None:
    other = neck()
    other.apply_translation((40, 0, 0))
    joints = analyze_geometry([_part(trimesh.util.concatenate([neck(), other]))]).weak_joints
    assert sorted(round(j.centroid[0]) for j in joints) == [0, 40]


def test_a_straight_cylinder_has_no_weak_joint() -> None:
    assert analyze_geometry([_part(trimesh.creation.cylinder(radius=10, height=40))]).weak_joints == []


def test_a_small_tip_is_no_weak_joint() -> None:
    """A cone narrows to nothing, but under 1 cm³ is above any narrow layer."""
    cone = revolved([(0, 0), (10, 0), (0, 30)])
    assert analyze_geometry([_part(cone)]).weak_joints == []


def test_a_thin_wall_on_a_floor_is_no_weak_joint() -> None:
    """A cup: its walls are an eighth of the floor below them, but the section is a
    ring, whose centre is outside it (measured: a planter in file 688)."""
    cup = revolved([(0, 0), (30, 0), (30, 30), (28, 30), (28, 2), (0, 2)])
    assert analyze_geometry([_part(cup)]).weak_joints == []


def test_a_neck_too_thin_for_a_peg_suggests_no_joint() -> None:
    """Room 1.5 mm: a 1.8 mm peg needs 0.9 + 0.2 + 0.8 = 1.9 mm."""
    thin = revolved([(0, 0), (10, 0), (10, 30), (1.5, 30), (1.5, 35), (8, 35), (8, 45), (0, 45)])
    [joint] = analyze_geometry([_part(thin)]).weak_joints
    assert joint.suggestion.joint is None


def test_an_open_split_part_is_not_sectioned() -> None:
    assert analyze_geometry([_part(neck())], split={"#FF0000"}).weak_joints == []
```

Append to `backend/tests/test_mesh_models_fixtures.py` (imports: `import trimesh`, `from scadbuddy.meshmodel.meshes import part_loader`, `from scadbuddy.meshmodel.paint import placement`, `from scadbuddy.render.geometry import analyze_geometry`, `from scadbuddy.render.split import ColourPart`):

```python
def placed_parts(scene: Scene, directory: Path) -> list[ColourPart]:
    """What the op-free render's solids are, without openscad: each colour's parts at
    their objects' places (measured identical to the render, meshmodel/paint.py)."""
    load = part_loader(directory)
    by_colour: dict[str, list[trimesh.Trimesh]] = {}
    for obj in scene.objects:
        m, t = placement(obj)
        for part in obj.parts:
            if part.negative:
                continue
            mesh = load(part.mesh)
            world = np.asarray(mesh.vertices) @ m.T + t
            by_colour.setdefault(part.colour, []).append(
                trimesh.Trimesh(vertices=world, faces=mesh.faces, process=False)
            )
    parts = []
    for extruder, (colour, meshes) in enumerate(by_colour.items(), start=1):
        joined = trimesh.util.concatenate(meshes)
        assert isinstance(joined, trimesh.Trimesh)
        parts.append(ColourPart(extruder, colour, colour, joined))
    return parts


def test_the_carrot_gardens_weak_joints_are_its_two_necks(tmp_path: Path) -> None:
    """Measured 2026-10-13: the necks at z 30.9 (small carrot) and 33.5 (big); the
    planter's walls and the big carrot's hollow body pass the 25 % rule but their
    sections are rings, so they are not reported."""
    _, scene = opened(CARROT, tmp_path)
    joints = analyze_geometry(placed_parts(scene, tmp_path)).weak_joints
    assert len(joints) == 2
    small, big = sorted(joints, key=lambda j: j.z)
    assert 30.0 < small.z < 32.0
    assert 32.5 < big.z < 35.0
    assert all(j.suggestion.joint is not None for j in joints)
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_geometry.py tests/test_mesh_models_fixtures.py -k "weak or neck or carrot or cylinder or tip or wall_on or open_split" -v`
Expected: FAIL with `AttributeError: 'GeometryAnalysis' object has no attribute 'weak_joints'`.

- [ ] **Step 3: Implement**

In `backend/scadbuddy/render/geometry.py`:

Add to the module docstring's methods, after *Smallest feature*:

```
    *Weak joints* (spec 2026-10-11 §9). The parts are taken as *pieces*: each part's
    islands, joined across parts where they share a vertex (two colours printed as one).
    Each piece is cut level every :data:`WEAK_LAYER_MM` up its height
    (`meshmodel/section.py` `layer_areas`); a layer whose section is under
    :data:`WEAK_RATIO` of the larger of the sections :data:`WEAK_WINDOW_MM` below and
    above it, with at least :data:`WEAK_MIN_VOLUME_MM3` of the piece above (the layer
    areas above it times the layer height), is weak. Weak layers less than the window
    apart are one joint, reported at its narrowest weak layer (the middle one of equal
    narrowest ones), and only when that section's centroid lies inside it: a ring (a
    cup's walls on its floor) is neither a neck nor a place a peg goes. Each carries a
    `split_at_plane` edit with a joint sized to the section as `edit_scene` sizes one.
    It knows nothing of the layer height actually printed or of the material.
```

Change `ANALYSIS_VERSION` and add the constants after `MAX_REPORTED_EDGES`:

```python
#: Bumped whenever a measurement changes meaning, so a cached analysis written by
#: an older version is computed again rather than served. 3: weak joints (#2146).
ANALYSIS_VERSION = 3
```

```python
#: Weak joints (spec 2026-10-11 §9): the layer, the window either side, the ratio, and
#: the volume above that makes a narrow layer a joint that can snap.
WEAK_LAYER_MM = 0.2
WEAK_WINDOW_MM = 2.0
WEAK_RATIO = 0.25
WEAK_MIN_VOLUME_MM3 = 1000.0
#: The most layers a piece is cut into: a taller piece's layers are spread thinner.
MAX_WEAK_LAYERS = 2000
MAX_WEAK_JOINTS = 20
#: Two parts' islands are one piece where they share a vertex within this, mm.
TOUCH_MM = 1e-3
```

Imports to add:

```python
import itertools

from scadbuddy.meshmodel.edits import WALL, JointSpec
from scadbuddy.meshmodel.section import Section, layer_areas, measure
from scadbuddy.render.meshmatch import match_points
```

The models, after `FeatureEstimate`:

```python
class JointSuggestion(BaseModel):
    """`split_at_plane`'s ``joint`` (meshmodel/edits.py `JointSpec`), sized to the section."""

    diameter: float
    depth: float
    clearance: float
    key: Literal["none", "flat", "hex"]
    peg_on: Literal["above", "below"]


class SplitSuggestion(BaseModel):
    """An `edit_scene` edit (spec 2026-10-11 §5): cut level through the section with a
    keyed peg on the narrower side and its socket in the wider. ``object`` is the mesh
    model's scene object the section is in, which `GET /outputs/{id}/geometry` names
    for a mesh model's render (`meshmodel/locate.py`); None otherwise. ``joint`` is None
    where no peg fits the section with two walls around its socket."""

    edit: Literal["split_at_plane"] = "split_at_plane"
    object: str | None = None
    point: Point
    normal: Point = (0.0, 0.0, 1.0)
    joint: JointSuggestion | None


class WeakJoint(BaseModel):
    """A level section much narrower than the piece just below or above it, with enough
    of the piece above it to snap it (spec 2026-10-11 §9)."""

    z: float
    area_mm2: float
    #: The larger of the sections `WEAK_WINDOW_MM` below and above.
    neighbour_area_mm2: float
    volume_above_mm3: float
    centroid: Point
    #: From the centroid to the section's nearest edge.
    room_mm: float
    #: The extruders of the parts the piece is made of.
    parts: list[int]
    #: The piece's box.
    bbox: BoundingBox
    suggestion: SplitSuggestion
```

`GeometryAnalysis` gains, after `smallest_feature`:

```python
    #: Where a piece is narrow enough to snap (spec 2026-10-11 §9), narrowest first.
    weak_joints: list[WeakJoint] = Field(default_factory=list)
```

The functions, after `_islands`:

```python
def _pieces(
    welded: Sequence[tuple[FloatArray, IntArray]],
) -> list[tuple[list[int], FloatArray, IntArray]]:
    """The pieces the parts print as: each part's islands, joined where islands of two
    parts share a vertex. Each is the indices of its parts, and its vertices and faces
    (every member part's vertices, its own faces)."""
    labels = [_islands(len(vertices), faces) for vertices, faces in welded]
    nodes: dict[tuple[int, int], int] = {}
    for part, ((_, faces), label) in enumerate(zip(welded, labels, strict=True)):
        for island in np.unique(label[faces[:, 0]]).tolist() if len(faces) else []:
            nodes[(part, int(island))] = len(nodes)
    parent = list(range(len(nodes)))

    def root(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for i, j in itertools.combinations(range(len(welded)), 2):
        hit = match_points(welded[i][0], welded[j][0], TOUCH_MM)
        touching = np.flatnonzero(hit >= 0)
        pairs = set(
            zip(labels[i][hit[touching]].tolist(), labels[j][touching].tolist(), strict=True)
        )
        for a, b in pairs:
            if (i, a) in nodes and (j, b) in nodes:
                parent[root(nodes[(i, a)])] = root(nodes[(j, b)])
    groups: dict[int, list[tuple[int, int]]] = {}
    for node, at in nodes.items():
        groups.setdefault(root(at), []).append(node)
    pieces: list[tuple[list[int], FloatArray, IntArray]] = []
    for members in groups.values():
        vertices: list[FloatArray] = []
        faces: list[IntArray] = []
        offset = 0
        for part, island in members:
            v, f = welded[part]
            vertices.append(v)
            faces.append(f[labels[part][f[:, 0]] == island] + offset)
            offset += len(v)
        pieces.append(
            (sorted({part for part, _ in members}), np.concatenate(vertices), np.concatenate(faces))
        )
    return pieces


def _suggestion(
    section: Section, z: float, below: float, above: float, low: float, high: float
) -> SplitSuggestion:
    """A level cut through the section with `edit_scene`'s joint (meshmodel/edits.py):
    the socket in the wider side, no deeper than that side has room for, and the
    default diameter (1.2 times the room) when it fits with two walls around it."""
    spec = JointSpec()
    peg_on: Literal["above", "below"] = "above" if below >= above else "below"
    socket_side = (z - low) if peg_on == "above" else (high - z)
    diameter = round(1.2 * section.room, 2)
    fits = section.room >= diameter / 2 + spec.clearance + WALL
    joint = (
        JointSuggestion(
            diameter=diameter,
            depth=round(min(spec.depth, max(1.0, socket_side - 1.0)), 1),
            clearance=spec.clearance,
            key=spec.key,
            peg_on=peg_on,
        )
        if fits
        else None
    )
    return SplitSuggestion(point=_point(section.centroid), joint=joint)


def weak_joints(
    parts: Sequence[ColourPart],
    welded: Sequence[tuple[FloatArray, IntArray]],
    numbers: Sequence[int],
    split: Collection[str],
) -> list[WeakJoint]:
    """Each piece's weak joints (see the module docstring), the narrowest for its
    neighbours first, at most :data:`MAX_WEAK_JOINTS`. Parts whose mesh is the open
    split are left out: a section of an open mesh is not one."""
    checked = [
        i
        for i, part in enumerate(parts)
        if part.colour not in split and not part.negative and len(welded[i][1])
    ]
    found: list[WeakJoint] = []
    for members, vertices, faces in _pieces([welded[i] for i in checked]):
        corners = vertices[faces]
        low, high = float(corners[:, :, 2].min()), float(corners[:, :, 2].max())
        layer = max(WEAK_LAYER_MM, (high - low) / MAX_WEAK_LAYERS)
        count = int((high - low) / layer)
        if count < 1:
            continue
        zs = low + layer * (np.arange(count) + 0.5)
        areas = layer_areas(vertices, faces, zs)
        window = max(1, round(WEAK_WINDOW_MM / layer))
        padded = np.concatenate([np.zeros(window), areas, np.zeros(window)])
        below, above = padded[:count], padded[2 * window : 2 * window + count]
        neighbour = np.maximum(below, above)
        volume_above = (areas[::-1].cumsum()[::-1] - areas) * layer
        weak = np.flatnonzero(
            (areas > 0) & (areas < WEAK_RATIO * neighbour) & (volume_above >= WEAK_MIN_VOLUME_MM3)
        )
        if len(weak) == 0:
            continue
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        used = corners.reshape(-1, 3)
        box = _box(used.min(axis=0), used.max(axis=0))
        extruders = [numbers[checked[m]] for m in members]
        for run in np.split(weak, np.flatnonzero(np.diff(weak) * layer >= WEAK_WINDOW_MM) + 1):
            narrowest = float(areas[run].min())
            ties = run[areas[run] <= narrowest * (1 + 1e-6)]
            at = int(ties[len(ties) // 2])
            z = float(zs[at])
            section = measure(mesh, (0.0, 0.0, z), (0.0, 0.0, 1.0))
            if section is None or not section.inside:
                continue
            found.append(
                WeakJoint(
                    z=z,
                    area_mm2=float(areas[at]),
                    neighbour_area_mm2=float(neighbour[at]),
                    volume_above_mm3=float(volume_above[at]),
                    centroid=_point(section.centroid),
                    room_mm=section.room,
                    parts=extruders,
                    bbox=box,
                    suggestion=_suggestion(
                        section, z, float(below[at]), float(above[at]), low, high
                    ),
                )
            )
    found.sort(key=lambda joint: joint.area_mm2 / joint.neighbour_area_mm2)
    return found[:MAX_WEAK_JOINTS]
```

In `analyze_geometry`, before the `return`, and in the `GeometryAnalysis(...)` call:

```python
    joints = weak_joints(parts, welded, list(numbers), split)
```

```python
        smallest_feature=feature,
        weak_joints=joints,
    )
```

- [ ] **Step 4: Run them to see them pass, with every geometry user**

Run: `cd backend && uv run --frozen pytest tests/test_geometry.py tests/test_mesh_models_fixtures.py tests/meshmodel -v && uv run --frozen mypy`
Expected: PASS (the fixture test skips without LFS fixtures; run it with them), clean. Then the API geometry tests: `uv run --frozen pytest tests/api/test_outputs.py -k geometry -v` (they compare a cached file to the answer; the new field round-trips).

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/render/geometry.py backend/tests/test_geometry.py backend/tests/test_mesh_models_fixtures.py
git commit -m "feat(render): analyze_geometry reports weak joints with a split that fits (#2146)"
```

---

### Task 9: A weak joint names its scene object

**Files:**
- Create: `backend/scadbuddy/meshmodel/locate.py`
- Modify: `backend/scadbuddy/api/outputs.py` (`get_output_geometry`, new `_attributed`)
- Test: `backend/tests/meshmodel/test_locate.py`, `backend/tests/api/test_outputs.py`, `backend/tests/test_mesh_models_fixtures.py`

**Interfaces:**
- Consumes: `GeometryAnalysis`, `WeakJoint` (Task 8); `part_loader`, `MeshOf` (Task 3); `placement`, `SOURCE_NAME` (Task 3); `source_directory`, `SnapshotUnavailableError` (`render/jobs.py`); `COMMIT_ID_PATTERN`, `GitError`, `RevisionNotFoundError` (`core/gitrepo.py`).
- Produces: `SLACK_MM = 0.5`; `object_at(scene: Scene, mesh_of: MeshOf, point: Sequence[float]) -> str | None`; `attribute(analysis: GeometryAnalysis, directory: Path) -> GeometryAnalysis`. `GET /outputs/{id}/geometry` fills `weak_joints[].suggestion.object` for a mesh model's unarranged output.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/meshmodel/test_locate.py
"""Which scene object a point of a render lies in (spec 2026-10-11 §9)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import TypeAdapter

from scadbuddy.meshmodel.edits import Edit, apply_edits
from scadbuddy.meshmodel.locate import attribute, object_at
from scadbuddy.meshmodel.meshes import part_loader
from scadbuddy.meshmodel.scene import Transform
from scadbuddy.render.geometry import analyze_geometry
from scadbuddy.render.objects3mf import ReadObject
from scadbuddy.render.split import ColourPart
from tests.meshmodel.test_paint import ORANGE, save, written
from tests.meshmodel.test_section import neck

EDITS = TypeAdapter(list[Edit])


def carrots(directory: Path, count: int = 2) -> None:
    written(directory, [ReadObject("Carrot", count, [ColourPart(0, "Body", ORANGE, neck())])])


def test_each_object_holds_its_own_points(tmp_path: Path) -> None:
    carrots(tmp_path)
    from scadbuddy.meshmodel.scene import load_scene

    scene = load_scene((tmp_path / "scene.json").read_text())
    load = part_loader(tmp_path)
    left, right = (o.transform.translate[0] for o in scene.objects)
    assert object_at(scene, load, (left, 0, 32)) == "o1"
    assert object_at(scene, load, (right, 0, 32)) == "o2"
    assert object_at(scene, load, (left, 0, 60)) is None


def test_a_split_object_holds_each_half_on_its_side(tmp_path: Path) -> None:
    carrots(tmp_path, count=1)
    from scadbuddy.meshmodel.scene import load_scene

    scene = load_scene((tmp_path / "scene.json").read_text())
    load = part_loader(tmp_path)
    split = apply_edits(scene, EDITS.validate_python([{"edit": "split_at_plane", "object": "o1", "point": [0, 0, 20], "normal": [0, 0, 1]}]), load)
    assert object_at(split, load, (0, 0, 10)) == "o1"
    assert object_at(split, load, (0, 0, 32)) == "o2"


def test_a_turned_object_is_found_in_its_own_frame(tmp_path: Path) -> None:
    carrots(tmp_path, count=1)
    from scadbuddy.meshmodel.scene import load_scene

    scene = load_scene((tmp_path / "scene.json").read_text())
    lying = scene.objects[0].model_copy(update={"transform": Transform(rotate=(0.0, 90.0, 0.0))})
    scene = scene.model_copy(update={"objects": [lying]})
    # rotate y 90 sends +z to +x: the neck (z 32) lies at x 32.
    assert object_at(scene, part_loader(tmp_path), (32, 0, 0)) == "o1"
    assert object_at(scene, part_loader(tmp_path), (0, 0, 32)) is None


def test_attribute_names_the_object_of_each_weak_joint(tmp_path: Path) -> None:
    carrots(tmp_path, count=1)
    analysis = analyze_geometry([ColourPart(1, "Body", ORANGE, neck())])
    [joint] = attribute(analysis, tmp_path).weak_joints
    assert joint.suggestion.object == "o1"


def test_attribute_leaves_a_hand_edited_or_plain_model_alone(tmp_path: Path) -> None:
    analysis = analyze_geometry([ColourPart(1, "Body", ORANGE, neck())])
    assert attribute(analysis, tmp_path) == analysis  # no scene
    carrots(tmp_path, count=1)
    (tmp_path / "model.scad").write_text("cube(1);\n")
    assert attribute(analysis, tmp_path) == analysis
```

(`save` is imported for symmetry with the other helpers; drop it if ruff flags it unused.)

In `backend/tests/api/test_outputs.py` (imports: `from scadbuddy.meshmodel.ingest import scene_from_objects`, `from scadbuddy.meshmodel.scene import SCENE_NAME, SceneSource, dump_scene, text_sha256`, `from scadbuddy.render.objects3mf import ReadObject`, `from tests.meshmodel.test_section import neck`; `write_plates_3mf`, `PlateParts`, `ColourPart` are imported already):

```python
def _carrot_output(client: TestClient, model: str, paths: DataPaths, *, scene: bool) -> str:
    """An output of ``model`` whose 3MF is the test carrot; with ``scene``, the model is
    a mesh model of that carrot (its scene's hash is the model.scad already there)."""
    directory = paths.model_dir(model)
    if scene:
        read = [ReadObject("Carrot", 1, [ColourPart(0, "Body", "#F08A24", neck())])]
        built, files = scene_from_objects(read, SceneSource(kind="upload", name="neck.stl", sha256="ab" * 32))
        for name, data in files.items():
            (directory / name).parent.mkdir(parents=True, exist_ok=True)
            (directory / name).write_bytes(data)
        source = (directory / "model.scad").read_text()
        (directory / SCENE_NAME).write_text(dump_scene(built.model_copy(update={"generated_sha256": text_sha256(source)})))
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}, headers=press()
    ).json()
    part = ColourPart(1, "Body", "#F08A24", neck())
    write_plates_3mf([PlateParts((part,), (1,))], ["#F08A24"], paths.output_dir(model, created["id"]) / "model.3mf", thumbnails=None)
    return str(created["id"])


def test_a_mesh_models_weak_joint_names_its_scene_object(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output = _carrot_output(client, model, paths, scene=True)
    [joint] = client.get(f"/api/v1/outputs/{output}/geometry").json()["weak_joints"]
    assert joint["suggestion"]["object"] == "o1"
    assert joint["suggestion"]["edit"] == "split_at_plane"


def test_a_weak_joint_names_no_object_outside_a_mesh_models_render(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """A template's render, and an arranged output (its objects moved off the scene's
    frame): the joint is reported, with no object rather than a wrong one."""
    plain = _carrot_output(client, model, paths, scene=False)
    [joint] = client.get(f"/api/v1/outputs/{plain}/geometry").json()["weak_joints"]
    assert joint["suggestion"]["object"] is None
    arranged = _carrot_output(client, model, paths, scene=True)
    (paths.output_dir(model, arranged) / "arranged_from.json").write_text(json.dumps([plain]))
    [joint] = client.get(f"/api/v1/outputs/{arranged}/geometry").json()["weak_joints"]
    assert joint["suggestion"]["object"] is None
```

Append to `backend/tests/test_mesh_models_fixtures.py` (imports: `from scadbuddy.meshmodel.locate import object_at`):

```python
def test_the_carrot_gardens_suggested_splits_name_their_carrots_and_fit(tmp_path: Path) -> None:
    """The carrot loop (spec §9): each weak joint's suggestion, sent to edit_scene with
    the object `object_at` names, splits that carrot with a joint that fits."""
    _, scene = opened(CARROT, tmp_path)
    load = part_loader(tmp_path)
    joints = analyze_geometry(placed_parts(scene, tmp_path)).weak_joints
    named = {object_at(scene, load, joint.centroid): joint for joint in joints}
    painted = {o.id for o in scene.objects if any(p.painted for p in o.parts)}
    assert set(named) == painted
    for oid, joint in named.items():
        edit = {**joint.suggestion.model_dump(), "object": oid}
        split = apply_edits(scene, EDITS.validate_python([edit]), load)
        assert len(split.objects) == len(scene.objects) + 1
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/meshmodel/test_locate.py tests/test_mesh_models_fixtures.py -v` then `uv run --frozen pytest tests/api/test_outputs.py -k weak_joint -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scadbuddy.meshmodel.locate'`; the API tests with `object` None for the mesh model.

- [ ] **Step 3: Implement `locate.py`**

```python
# backend/scadbuddy/meshmodel/locate.py
"""Which scene object a point of a mesh model's render lies in (#2146, spec 2026-10-11
§9), so a weak joint's suggested `split_at_plane` names the object `edit_scene` takes.
`analyze_geometry` reads a 3MF, which knows no scene; `GET /outputs/{id}/geometry`
attributes its weak joints here, with the scene at the output's revision."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np

from scadbuddy.meshmodel.frames import unit
from scadbuddy.meshmodel.meshes import MeshOf, part_loader
from scadbuddy.meshmodel.paint import SOURCE_NAME, placement
from scadbuddy.meshmodel.scene import SCENE_NAME, Cut, Scene, load_scene, text_sha256, value
from scadbuddy.render.geometry import GeometryAnalysis

#: How far outside an object's mesh box, or past one of its cuts, a point may lie and
#: still be in it, mm: a section's centroid is inside, but a cut face is a boundary.
SLACK_MM = 0.5


def _keeps(cut: Cut, local: np.ndarray) -> bool:
    side = float((local - np.asarray([value(x) for x in cut.point])) @ unit(cut.normal))
    return side >= -SLACK_MM if cut.keep == "above" else side <= SLACK_MM


def object_at(scene: Scene, mesh_of: MeshOf, point: Sequence[float]) -> str | None:
    """The object whose printed parts' box, in its own frame and grown by
    :data:`SLACK_MM`, holds ``point`` (world) on the kept side of each of its cuts; the
    one with the smallest box when several do; None when none does."""
    world = np.asarray(point, dtype=float)
    best: tuple[float, str] | None = None
    for obj in scene.objects:
        m, t = placement(obj)
        if abs(float(np.linalg.det(m))) < 1e-12:
            continue
        printed = [mesh_of(part.mesh) for part in obj.parts if not part.negative]
        if not printed:
            continue
        local = np.linalg.solve(m, world - t)
        low = np.min([mesh.bounds[0] for mesh in printed], axis=0) - SLACK_MM
        high = np.max([mesh.bounds[1] for mesh in printed], axis=0) + SLACK_MM
        if not ((local >= low) & (local <= high)).all():
            continue
        if not all(_keeps(op, local) for op in obj.ops if isinstance(op, Cut)):
            continue
        size = float(np.prod(high - low))
        if best is None or size < best[0]:
            best = (size, obj.id)
    return None if best is None else best[1]


def attribute(analysis: GeometryAnalysis, directory: Path) -> GeometryAnalysis:
    """``analysis`` with each weak joint's suggestion naming the object its section's
    centroid is in, for a render of the mesh model in ``directory``. Unchanged where
    there is no scene, or model.scad was edited by hand (the scene no longer describes
    the geometry)."""
    if not analysis.weak_joints:
        return analysis
    try:
        scene = load_scene((directory / SCENE_NAME).read_text(encoding="utf-8"))
        source = (directory / SOURCE_NAME).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return analysis
    if text_sha256(source) != scene.generated_sha256:
        return analysis
    mesh_of = part_loader(directory)
    joints = [
        joint.model_copy(
            update={
                "suggestion": joint.suggestion.model_copy(
                    update={"object": object_at(scene, mesh_of, joint.centroid)}
                )
            }
        )
        for joint in analysis.weak_joints
    ]
    return analysis.model_copy(update={"weak_joints": joints})
```

- [ ] **Step 4: Implement the route**

In `backend/scadbuddy/api/outputs.py` (imports: `import re` if absent; `from scadbuddy.core.gitrepo import COMMIT_ID_PATTERN, GitError, RevisionNotFoundError`; `from scadbuddy.meshmodel.locate import attribute`; `from scadbuddy.render.jobs import SnapshotUnavailableError, source_directory`; `from scadbuddy.library.history import ModelHistory` for the annotation if not imported; `HistoryDep`, `PathsDep` are imported already):

```python
@router.get(
    "/outputs/{output_id}/geometry",
    response_model=GeometryAnalysis,
    summary="Mesh geometry analysis",
)
async def get_output_geometry(
    output_id: OutputIdPath,
    outputs: OutputsDep,
    paths: PathsDep,
    history: HistoryDep,
    plate: Annotated[int, Query(ge=1, description="The plate to measure (1-based)")] = 1,
) -> GeometryAnalysis:
    """Printability measurements of the closed per-colour solids on one plate of the
    output (#284): open and non-manifold edges with their locations, bounding box,
    bed contact, height-to-base ratio, overhang area by angle, estimates of the
    thinnest wall and smallest feature, and weak joints (#2146): level sections narrow
    enough to snap, each with a `split_at_plane` edit whose `object` names the scene
    object for a mesh model's output. Coordinates are the model's own (mm, Z up), as in
    the preview. A 3MF with more than one plate (#289) is measured a plate at a time;
    ``plates`` in the result says how many there are. Computed on first ask and cached
    beside the output."""
    require_output(outputs, output_id)
    try:
        analysis = await asyncio.to_thread(outputs.geometry, output_id, plate)
    except FileNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no 3MF") from None
    except NoSuchPlateError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r}: {error}") from None
    except (ValueError, zipfile.BadZipFile) as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the 3MF of output {output_id!r} cannot be analysed: {error}",
        ) from None
    return await _attributed(analysis, outputs, output_id, paths=paths, history=history)


async def _attributed(
    analysis: GeometryAnalysis,
    outputs: OutputStore,
    output_id: str,
    *,
    paths: DataPaths,
    history: ModelHistory,
) -> GeometryAnalysis:
    """``analysis`` with its weak joints naming their scene objects, when the output is
    a render of a mesh model (spec 2026-10-11 §9), read at the output's revision. An
    arranged output's objects were moved off the scene's frame, so it is left as it is,
    as is one whose revision cannot be read."""
    if not analysis.weak_joints or await asyncio.to_thread(outputs.arranged_from, output_id):
        return analysis
    meta = await asyncio.to_thread(outputs.get, output_id)
    version = meta.model_version
    requested = version if version is not None and re.fullmatch(COMMIT_ID_PATTERN, version) else None
    try:
        directory, _ = await source_directory(meta.slug, requested, paths=paths, history=history)
    except (GitError, RevisionNotFoundError, SnapshotUnavailableError, OSError):
        return analysis
    return await asyncio.to_thread(attribute, analysis, directory)
```

(`OutputStore` and `DataPaths` are the types behind `OutputsDep` and `PathsDep`; import them for the annotations if `outputs.py` does not already.)

- [ ] **Step 5: Run them to see them pass**

Run: `cd backend && uv run --frozen pytest tests/meshmodel tests/test_mesh_models_fixtures.py -v && uv run --frozen pytest tests/api/test_outputs.py -v && uv run --frozen mypy`
Expected: PASS (API tests need Postgres and Temporal; the fixture tests need the LFS files), clean.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/meshmodel/locate.py backend/scadbuddy/api/outputs.py backend/tests/meshmodel/test_locate.py backend/tests/api/test_outputs.py backend/tests/test_mesh_models_fixtures.py
git commit -m "feat(meshmodel): a mesh model's weak joint names the scene object to split (#2146)"
```

---

### Task 10: Tool descriptions and the authoring skill

**Files:**
- Modify: `agent/src/tools/outputs.ts` (`analyze_geometry`), `agent/src/tools/meshModels.ts` (`get_scene`, `edit_scene`)
- Modify: `plugins/scadbuddy/skills/authoring/SKILL.md` (§14)
- Test: `agent/test/tools.test.ts` (`describe('analyze_geometry')`)

**Interfaces:**
- Consumes: the backend fields of Tasks 5, 8, 9 (`notes`, `weak_joints`, `suggestion.object`).
- Produces: nothing code-level; the eval (Task 11) relies on `analyze_geometry`, `get_scene`, `edit_scene`, `render_model` as they are.

- [ ] **Step 1: Write the failing test**

In `agent/test/tools.test.ts`, inside `describe('analyze_geometry', ...)`:

```ts
  it('tells the model about weak joints and the split each suggests', () => {
    const t = tool('analyze_geometry')
    expect(t.description).toContain('weak_joints')
    expect(t.description).toContain('split_at_plane')
  })
```

- [ ] **Step 2: Run it to see it fail**

Run: `cd agent && pnpm exec vitest run test/tools.test.ts -t analyze_geometry`
Expected: FAIL: the description has no `weak_joints`.

- [ ] **Step 3: Implement**

`agent/src/tools/outputs.ts`, `analyze_geometry`'s description:

```ts
    description:
      "Printability measurements of an output's closed per-colour solids: open and non-manifold edges with " +
      'their locations, bounding box, bed contact, height-to-base ratio, overhang area by angle, and ' +
      'estimates of the thinnest wall and smallest feature (mm, Z up, model coordinates). weak_joints lists ' +
      'level sections much narrower than the print 2 mm below or above them with at least 1 cm³ above, where ' +
      "layer lines snap (a carrot's neck); each carries `suggestion`, an edit_scene split_at_plane edit that " +
      'cuts there with a peg sized to the section (`joint` null where no peg fits). For a mesh model\'s render ' +
      '`suggestion.object` names the scene object, so the edit can be sent as it is.',
```

`agent/src/tools/meshModels.ts`, `get_scene`'s description gains, before the `hand_edited` sentence: `'notes say what reading the file dropped and where a painted object\'s painting is not carried whole (a mirrored object). '`; `edit_scene`'s description gains, after its first sentence: `'Painting done in Bambu Studio is carried into every render and print. '`.

`plugins/scadbuddy/skills/authoring/SKILL.md` §14: replace the paragraph that starts "Printing one colour in place into another" with:

```markdown
Printing one colour in place into another, when the colours are separate parts (a leaves
part into a body part): anchor it with a coloured `add`, a `cylinder` in the leaves' colour,
centred on the neck where the leaves meet the body, wider than the neck and a few mm tall,
through it. An `add` with a `colour` takes its whole volume from every other colour, so no
volume has two colours, and the green prints joined into the orange as one piece. An `add`
without a `colour` is in the object's first colour and carves the others the same way. Keep
it short enough to touch nothing else on the object, or it joins that too. Painted leaves
need no anchor for their colour (Paint, below). Sources: the compiler
`backend/scadbuddy/meshmodel/compile.py` (`_apply`); measured on a MakerWorld carrot in
`backend/tests/test_mesh_models_fixtures.py`
(`test_the_carrot_neck_anchored_in_green_prints_as_one_piece`: one piece, the two colours'
volumes summing to the one-colour render's).

Finding where it snaps: render, save the output, and run `analyze_geometry` on it.
`weak_joints` lists each level section much narrower than the print 2 mm below or above it
with at least 1 cm³ above (a carrot's neck), narrowest first. For a mesh model each one's
`suggestion` is a `split_at_plane` edit naming its object, with a `joint` sized to the
section: send it with `edit_scene`, with a `lay_flat` of the new half (the next object id)
in the same call, render, save, and run `analyze_geometry` again; the neck is gone from the
list. A `joint` of `null` means no peg fits there with two walls round its socket: cut
where the section is wider, or split without a joint. Sources:
`docs/superpowers/specs/2026-10-11-mesh-models-design.md` §9;
`backend/scadbuddy/render/geometry.py` (`weak_joints`);
`backend/scadbuddy/meshmodel/locate.py`.
```

and replace the paragraph that starts "Paint: a part painted in Bambu Studio" with:

```markdown
Paint: a part painted in Bambu Studio keeps its codes beside its mesh, and every render
carries them onto the rendered part: a triangle the render left as it was keeps its
painting exactly, and a new face (a cut, a socket, an `add` carved out of it) takes the
colour of the painted triangle it lies on when that triangle is one colour, otherwise the
part's own. A mirrored object (a negative scale) keeps only whole-triangle colours, and
`get_scene`'s notes say so. A render made before this was released, at the same revision
and parameters, is reused as it was, without paint: any edit renders it afresh. Sources:
`docs/superpowers/specs/2026-10-11-mesh-models-design.md` §7;
`backend/scadbuddy/meshmodel/paint.py`; measured on a MakerWorld carrot in
`backend/tests/test_mesh_models_paint.py`.
```

The `PostToolUse` hook bumps `plugins/scadbuddy/.claude-plugin/plugin.json`'s `version` on the branch's first plugin edit and mirrors it into `agent/plugins/scadbuddy/.claude-plugin/plugin.json`. Check both moved past `0.1.19` (if the hook did not run, bump both to `0.1.20` by hand).

- [ ] **Step 4: Run the tests and the plugin checks**

Run:
```bash
cd agent && pnpm lint && pnpm typecheck && pnpm exec vitest run test/tools.test.ts test/meshModelTools.test.ts
cd .. && .github/scripts/lint-plugin.sh && claude plugin validate plugins/scadbuddy
```
Expected: PASS; the lint finds every cited source.

- [ ] **Step 5: Commit**

```bash
git add agent/src/tools/outputs.ts agent/src/tools/meshModels.ts agent/test/tools.test.ts plugins/scadbuddy agent/plugins/scadbuddy
git commit -m "feat(agent): tell the model about weak joints and carried paint (#2146)"
```

---

### Task 11: The eval "the leaves of this carrot snap off; fix it"

**Files:**
- Modify: `agent/evals/backend.ts` (mesh-model routes, the seeded carrot, the geometry answer)
- Modify: `agent/evals/scenarios.ts` (the `carrotWeakNeck` scenario, in `SCENARIOS`)
- Modify: `agent/test/evals.test.ts` (negative controls)
- Modify: `docs/ai/evals.md` (the scenario table)

**Interfaces:**
- Consumes: the tools `analyze_geometry`, `get_scene`, `edit_scene`, `render_model`, `eject_scene`, `update_source`; `EvalBackend`, `routePattern`, `version` (`evals/backend.ts`).
- Produces: `EvalBackend.seedCarrot(): { slug: string; output: string }`; `EvalBackend.nextOutputId(slug: string, params?: Record<string, unknown>): string`; `CARROT_NECK: readonly [number, number]`; `CARROT_WEAK_JOINT`; `splitsTheNeck(edit: Record<string, unknown>): boolean`; scenario `carrotWeakNeck` (id `mesh-carrot-weak-neck`). The durable run (`test/evals.durable.e2e.test.ts`) picks it up from `SCENARIOS` with no edit.

- [ ] **Step 1: Write the failing tests**

In `agent/test/evals.test.ts`, import `carrotWeakNeck` from `../evals/scenarios.js` and add to the scripted describe block, after the authoring control:

```ts
  const NECK_SPLIT = {
    edit: 'split_at_plane',
    object: 'o1',
    point: [0, 0, 31.3],
    normal: [0, 0, 1],
    joint: { diameter: 3.59, depth: 6, clearance: 0.2, key: 'flat', peg_on: 'above' },
  }
  const LAY_FLAT = { edit: 'lay_flat', object: 'o2', normal: 'largest_flat' }

  it('carrot: the scene edits reached the backend, then a saved render, asking nothing', async () => {
    const { outcome } = await play(carrotWeakNeck)
    const edits = outcome.backend.requests(/^POST \/api\/v1\/models\/carrot\/scene\/edits$/)
    expect(edits).toHaveLength(1)
    expect(edits[0]?.body).toMatchObject({ edits: [NECK_SPLIT, LAY_FLAT] })
    // The last analysis is of the render after the split: no weak joint left.
    const last = outcome.toolCalls.filter((c) => c.tool === 'analyze_geometry').at(-1)
    expect(JSON.parse(last?.result ?? '{}').weak_joints).toEqual([])
  }, 90_000)

  it('control: a split away from the neck, or with no joint, fails the split check', async () => {
    for (const split of [{ ...NECK_SPLIT, point: [0, 0, 10] }, { ...NECK_SPLIT, joint: undefined }]) {
      const { failed } = await play(carrotWeakNeck, (own) => [
        own[0]!,
        own[1]!,
        { tool: 'edit_scene', input: { slug: 'carrot', edits: [split, LAY_FLAT] } },
        { tool: 'render_model', input: { slug: 'carrot', params: {}, save_output: true } },
        { text: 'Split and rendered.' },
      ])
      expect(failed).toEqual(['split the carrot at its neck with a keyed joint'])
    }
  }, 180_000)

  it('control: editing without rendering again fails the render check', async () => {
    const { failed } = await play(carrotWeakNeck, (own) => [own[0]!, own[1]!, own[2]!, { text: 'Fixed.' }])
    expect(failed).toEqual(['rendered the carrot after its last scene edit, and saved that render'])
  }, 90_000)

  it('control: ejecting the scene and editing the source fails the scene checks', async () => {
    const { failed } = await play(carrotWeakNeck, (own) => [
      own[0]!,
      { tool: 'eject_scene', input: { slug: 'carrot' } },
      { tool: 'update_source', input: { slug: 'carrot', source: '// cut by hand\n' } },
      { tool: 'render_model', input: { slug: 'carrot', params: {}, save_output: true } },
      { text: 'Done.' },
    ])
    expect(failed).toEqual([
      'split the carrot at its neck with a keyed joint',
      'laid the leaves half flat',
      'rendered the carrot after its last scene edit, and saved that render',
      'kept editing it as a scene (no eject_scene, no update_source)',
    ])
  }, 90_000)
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && pnpm exec vitest run test/evals.test.ts`
Expected: FAIL: `carrotWeakNeck` is not exported.

- [ ] **Step 3: Implement the recorded backend's side**

In `agent/evals/backend.ts`, extend the types:

```ts
export type EvalModel = {
  slug: string
  name: string
  description: string
  readme: string | null
  schema: { parameters: Record<string, unknown>[]; groups: string[] }
  /** Newest last. The model's current source is the last version's. */
  versions: Version[]
  /** A mesh model's scene (#2146): what GET /models/{slug}/scene answers. */
  scene?: { objects: Record<string, unknown>[] } & Record<string, unknown>
  /** Each successful POST …/scene/edits' edits, in order. */
  sceneEdits?: Record<string, unknown>[][]
}

export type EvalJob = { id: string; slug: string; params: Record<string, unknown>; version: string; edits: number }
```

`startJob` records how many edit calls the model had: `const job: EvalJob = { id, slug, params, version: …, edits: this.models.get(slug)?.sceneEdits?.length ?? 0 }`. `record()`'s origin becomes `origin: m.slug === 'name-keychain' ? 'builtin' : 'user'`.

Add, after `keychain3mf`:

```ts
/**
 * The carrot of models/mesh-joint-demo/make.py before its edits: an orange body (r 10, z 0
 * to 30) and green leaves on a neck (r 3, z 30 to 35, then r 6 to 45), one object.
 */
function carrotScene(): NonNullable<EvalModel['scene']> {
  return {
    version: 1,
    generated_sha256: hex('carrot.scad', 64),
    source: { kind: 'upload', name: 'carrot (made by make.py)', sha256: '0'.repeat(64), file_id: null, output_id: null, licence: null },
    objects: [
      {
        id: 'o1',
        name: 'Carrot',
        parts: [
          { id: 'p1', mesh: 'meshes/o1-p1.3mf', colour: '#F08A24', painted: false, negative: false },
          { id: 'p2', mesh: 'meshes/o1-p2.3mf', colour: '#3A7D2C', painted: false, negative: false },
        ],
        ops: [],
        transform: { translate: [0, 0, 0], rotate: [0, 0, 0], scale: [1, 1, 1] },
      },
    ],
    notes: [],
  }
}

/** The carrot's neck, z from and to (mm). */
export const CARROT_NECK = [30, 35] as const

/**
 * What backend/scadbuddy/render/geometry.py `weak_joints` reports for that carrot (the
 * shape of backend/tests/test_geometry.py's two-colour carrot), its object named as
 * `GET /outputs/{id}/geometry` names it for a mesh model.
 */
export const CARROT_WEAK_JOINT = {
  z: 31.3,
  area_mm2: 28.2,
  neighbour_area_mm2: 313.7,
  volume_above_mm3: 1225,
  centroid: [0, 0, 31.3],
  room_mm: 2.99,
  parts: [1, 2],
  bbox: { min: [-10, -10, 0], max: [10, 10, 45], size: [20, 20, 45] },
  suggestion: {
    edit: 'split_at_plane',
    object: 'o1',
    point: [0, 0, 31.3],
    normal: [0, 0, 1],
    joint: { diameter: 3.59, depth: 6, clearance: 0.2, key: 'flat', peg_on: 'above' },
  },
}

/** A `split_at_plane` with a joint whose plane is level (within about 25°) through the neck. */
export function splitsTheNeck(edit: Record<string, unknown>): boolean {
  if (edit.edit !== 'split_at_plane' || !edit.joint) return false
  const point = edit.point
  const normal = edit.normal
  if (!Array.isArray(point) || !Array.isArray(normal) || point.length !== 3 || normal.length !== 3) return false
  const [, , z] = point as number[]
  const [nx, ny, nz] = normal as number[]
  const length = Math.hypot(nx!, ny!, nz!)
  return z! > CARROT_NECK[0] && z! < CARROT_NECK[1] && length > 0 && Math.abs(nz!) / length >= 0.9
}
```

Methods on `EvalBackend`:

```ts
  /** The mesh model `carrot` (its leaves snap at the neck) and a saved render of it, as opened. */
  seedCarrot(): { slug: string; output: string } {
    this.models.set('carrot', {
      slug: 'carrot',
      name: 'Carrot',
      description: 'A carrot opened from a mesh.',
      readme: null,
      schema: { groups: [], parameters: [] },
      versions: [version('carrot', 0, 'Open carrot.stl', '// Generated from scene.json by ScadBuddy\n')],
      scene: carrotScene(),
      sceneEdits: [],
    })
    return { slug: 'carrot', output: this.seedOutput('carrot', 'Carrot as opened') }
  }

  /** The id the next render of `slug` with `params` is saved under, as `startJob` and the outputs route name them. */
  nextOutputId(slug: string, params: Record<string, unknown> = {}): string {
    return hex(`output:${hex(`job:${this.jobs.size}:${slug}:${JSON.stringify(params)}`)}`)
  }
```

Routes in `route()`, after the readme route:

```ts
    if (model && sub === 'scene' && !subId && method === 'GET') {
      if (!model.scene) return problem(404, 'Not Found', `"${model.slug}" is not a mesh model (it has no scene)`)
      return Response.json({ scene: model.scene, notes: [], hand_edited: false })
    }
    if (model && sub === 'scene' && subId === 'edits' && method === 'POST') {
      if (!model.scene) return problem(404, 'Not Found', `"${model.slug}" is not a mesh model (it has no scene)`)
      const { edits, message } = (body ?? {}) as { edits?: Record<string, unknown>[]; message?: string }
      if (!Array.isArray(edits) || edits.length === 0) return problem(422, 'Unprocessable Entity', 'edits is required')
      model.sceneEdits = [...(model.sceneEdits ?? []), edits]
      // A split's other half is the next object id (backend/scadbuddy/meshmodel/edits.py).
      for (const edit of edits) {
        if (edit.edit === 'split_at_plane') model.scene.objects.push({ ...model.scene.objects[0], id: `o${model.scene.objects.length + 1}`, name: 'Carrot (top)' })
      }
      model.versions.push(version(model.slug, model.versions.length, message || 'Edit the scene', this.source(model.slug)))
      return Response.json(this.record(model))
    }
```

and, beside the other output routes:

```ts
    if (resource === 'outputs' && id && sub === 'geometry' && method === 'GET') {
      const out = this.outputs.get(id)
      const job = out ? this.jobs.get(out.job_id) : undefined
      if (!out || !job) return problem(404, 'Not Found', `no output "${id}"`)
      return Response.json(this.geometry(job))
    }
```

and the method:

```ts
  /**
   * A render's analysis (backend/scadbuddy/render/geometry.py, `weak_joints` only of
   * substance): the carrot's neck is a weak joint until the render follows a split with a
   * joint through it.
   */
  private geometry(job: EvalJob) {
    const model = this.models.get(job.slug)
    const edits = (model?.sceneEdits ?? []).slice(0, job.edits).flat()
    const weak = model?.scene && !edits.some(splitsTheNeck) ? [CARROT_WEAK_JOINT] : []
    const box = { min: [-10, -10, 0], max: [10, 10, 45], size: [20, 20, 45] }
    return {
      version: 3,
      plate: 1,
      plates: 1,
      parts: [],
      bbox: box,
      bed_z: 0,
      height_mm: 45,
      bed_contact_area_mm2: 314.2,
      overhangs: [],
      edges: [],
      edges_truncated: false,
      weak_joints: weak,
    }
  }
```

`jobStatus`'s `colors` gains the carrot: `colors: job.slug === 'name-keychain' ? [...] : job.slug === 'carrot' ? ['#F08A24', '#3A7D2C'] : ['#ffffff']`.

- [ ] **Step 4: Implement the scenario**

In `agent/evals/scenarios.ts`, import `CARROT_NECK`, `splitsTheNeck` from `./backend.js` and add before `SCENARIOS`:

```ts
// ── 6. Fix a carrot whose leaves snap off (spec 2026-10-11 §9, §10) ────────

const EDITS_ROUTE = /^POST \/api\/v1\/models\/carrot\/scene\/edits$/

/** Every edit the backend accepted for the carrot, in order. */
function carrotEdits(o: Outcome): Record<string, unknown>[] {
  return o.backend
    .requests(EDITS_ROUTE)
    .filter((r) => (r.status ?? 0) < 300)
    .flatMap((r) => (r.body as { edits?: Record<string, unknown>[] } | undefined)?.edits ?? [])
}

export const carrotWeakNeck: Scenario = {
  id: 'mesh-carrot-weak-neck',
  title: 'Fix a carrot whose leaves snap off at the neck',
  prepare: (backend) => {
    const seededBackend = seeded(backend)
    const { output } = seededBackend.seedCarrot()
    const next = seededBackend.nextOutputId('carrot')
    return {
      prompt:
        `The leaves of my carrot (the mesh model "carrot"; its last render is saved as output ${output}) snap off ` +
        'at the neck when I print it. Fix it so it prints strong, then render it again and save the render.',
      script: [
        { tool: 'analyze_geometry', input: { output_id: output } },
        { tool: 'get_scene', input: { slug: 'carrot' } },
        {
          tool: 'edit_scene',
          input: {
            slug: 'carrot',
            edits: [
              {
                edit: 'split_at_plane',
                object: 'o1',
                point: [0, 0, 31.3],
                normal: [0, 0, 1],
                joint: { diameter: 3.59, depth: 6, clearance: 0.2, key: 'flat', peg_on: 'above' },
              },
              { edit: 'lay_flat', object: 'o2', normal: 'largest_flat' },
            ],
            message: 'Split the leaves off at the neck with a keyed peg, and lay them flat',
          },
        },
        { tool: 'render_model', input: { slug: 'carrot', params: {}, save_output: true } },
        { tool: 'analyze_geometry', input: { output_id: next } },
        {
          text:
            'The neck was the weak joint. I split the leaves off there with a keyed peg on them and a socket in the ' +
            'body, laid the leaves flat so their layers run along them, and rendered and saved it; the new render ' +
            'has no weak joint.',
        },
      ],
    }
  },
  checks: [
    ...INVARIANTS,
    {
      name: 'split the carrot at its neck with a keyed joint',
      run: (o) => {
        const edits = carrotEdits(o)
        return edits.some((e) => e.object === 'o1' && splitsTheNeck(e))
          ? true
          : `no split_at_plane of o1 with a joint, level through z ${CARROT_NECK[0]} to ${CARROT_NECK[1]}; edits: ${JSON.stringify(edits)}`
      },
    },
    {
      name: 'laid the leaves half flat',
      run: (o) => {
        const edits = carrotEdits(o)
        const split = edits.findIndex((e) => e.object === 'o1' && e.edit === 'split_at_plane')
        const flat = edits.findIndex((e, i) => i > split && e.edit === 'lay_flat' && e.object !== 'o1')
        return split >= 0 && flat > split ? true : `edits: ${edits.map((e) => `${String(e.edit)} ${String(e.object)}`).join(', ') || 'none'}`
      },
    },
    {
      name: 'rendered the carrot after its last scene edit, and saved that render',
      run: (o) => {
        const log = o.backend.log
        const lastEdit = log.findLastIndex((r) => EDITS_ROUTE.test(`${r.method} ${r.path}`) && (r.status ?? 0) < 300)
        if (lastEdit < 0) return 'no scene edit was made'
        const render = log.findIndex((r, i) => i > lastEdit && r.method === 'POST' && r.path === '/api/v1/models/carrot/render')
        if (render < 0) return 'no render after the last edit'
        const saved = log.findIndex(
          (r, i) => i > render && r.method === 'POST' && r.path === '/api/v1/models/carrot/outputs' && (r.status ?? 0) < 300,
        )
        return saved >= 0 ? true : 'the render after the last edit was not saved'
      },
    },
    {
      name: 'kept editing it as a scene (no eject_scene, no update_source)',
      run: (o) => {
        const hits = o.backend.requests(/^(POST \/api\/v1\/models\/carrot\/scene\/eject|PUT \/api\/v1\/models\/carrot\/source)$/)
        return hits.length ? `backend received ${hits.map((r) => `${r.method} ${r.path}`).join(', ')}` : true
      },
    },
    noApprovalNeeded,
  ],
}
```

and `export const SCENARIOS: readonly Scenario[] = [customise, authoring, printStops, injection, citedAdvice, carrotWeakNeck]`.

- [ ] **Step 5: Document it**

In `docs/ai/evals.md`'s scenario table, add a row:

```markdown
| `mesh-carrot-weak-neck` | The leaves of the mesh model "carrot" snap off at the neck; fix it so it prints strong, render it again and save it. | A `split_at_plane` of `o1` with a `joint`, its plane level through the neck (z 30 to 35); a `lay_flat` of the other half; a render of `carrot` after the last scene edit, saved as an output; no `eject_scene` or `update_source`; no approval asked. The recorded backend seeds the unsplit carrot `models/mesh-joint-demo/make.py` starts from, with a saved render, and answers `analyze_geometry` with the neck as a weak joint (as `backend/scadbuddy/render/geometry.py` `weak_joints` reports it) until a render made after such a split. The real geometry is covered by the backend's tests (`backend/tests/test_geometry.py`, `backend/tests/test_mesh_models_fixtures.py`) and the demo's `verify.sh`, not by this scenario. |
```

and in the negative controls list, add: "a carrot split away from its neck or without a joint; a carrot edited and never rendered again; a carrot whose scene was ejected and its source edited instead."

- [ ] **Step 6: Run the agent suite**

Run: `cd agent && pnpm lint && pnpm typecheck && pnpm exec vitest run test/evals.test.ts test/evalScoring.test.ts && pnpm test`
Expected: PASS: `mesh-carrot-weak-neck: the scripted run passes every check`, the three controls, and "every scenario checks the approval invariants". With `SCADBUDDY_TEST_DATABASE_URL`, uv and a Temporal CLI, also run `pnpm exec vitest run test/evals.durable.e2e.test.ts`: `mesh-carrot-weak-neck: the scripted durable run passes every check`.

- [ ] **Step 7: Commit**

```bash
git add agent/evals/backend.ts agent/evals/scenarios.ts agent/test/evals.test.ts docs/ai/evals.md
git commit -m "test(agent): eval the carrot whose leaves snap off, scored on its edits and render (#2146)"
```

---

### Task 12: The spec and the agents' notes

**Files:**
- Modify: `docs/superpowers/specs/2026-10-11-mesh-models-design.md`
- Modify: `CLAUDE.md` (Layout)

**Interfaces:** none.

- [ ] **Step 1: Update the spec**

- Status line: `Status: phases 1–2 merged (#2585); phase 3 planned in docs/superpowers/plans/2026-10-13-mesh-models-phase-3.md.`
- §4, the sentence "A painted part's paint is kept beside its mesh but not carried into renders or prints until §7 (phase 3); its object's note says so." becomes "A painted part's paint is kept beside its mesh and carried into renders and prints (§7)."
- §7: after step 3 add:

```markdown
Measured 2026-10-13 (openscad/openscad:dev.2026-10-12, file 688 op-free): every output
triangle matched its source within 1e-4 mm, but OpenSCAD's import holds coordinates as
32-bit floats (only 87 % shared a 1e-4 mm grid cell, so the match is by distance), and
about two thirds started at another vertex: a copied code also turns its triangle's
vertices back to the source's order, since a code splits its triangle by vertex position.
The step runs in the piece's `render_solids` activity (a `paint` stage, `render/jobs.py`
`paint_layout`), not between the pieces and `pack`: `pack` sees only boxes, and every
3MF (the piece's own, an output's, an arranged one's) is written from the piece's
`layout.json`, which names each painted part's carried codes. A mirrored part keeps the
colour of each triangle that is one colour (rule 2); only finer painting is lost.
A piece rendered before this shipped, at the same revision and parameters, is reused
without paint (`piece_key` carries no code version).
```

- §9: after its paragraph add:

```markdown
As built (plan 2026-10-13): sections are of each *piece* (a part's connected islands,
joined across parts where they share a vertex), since a part is one colour across the
whole plate; only sections whose centroid lies inside them are reported (on file 688 the
rule also flagged a planter's walls over its floor and a hollow carrot body, both rings);
the volume above is the layer areas above times the layer height; and the suggestion's
`object` is filled by `GET /outputs/{id}/geometry` for a mesh model's unarranged output
(`meshmodel/locate.py`), null otherwise.
```

- §10, the agent bullet: "…an eval scenario "the leaves of this carrot snap off; fix it" on the demo's unsplit carrot, seeded in the recorded backend (`agent/evals/scenarios.ts` `mesh-carrot-weak-neck`), scored on the edits made and the render that follows them."
- §11, item 3: "Paint transfer (§7) and `weak_joints` (§9), with the carrot eval (§10). Acceptance: file 688's painted leaves print green, anchored or not (`backend/tests/test_mesh_models_paint.py`)."

- [ ] **Step 2: Update `CLAUDE.md`'s Layout**

After the `backend/scadbuddy/render/` bullet, add:

```markdown
- `backend/scadbuddy/meshmodel/` — mesh models (#2146, spec 2026-10-11): `scene.py`,
  `compile.py`, `edits.py`, `operations.py` (the `library` kinds), `paint.py` (paint
  transfer: a painted part's `.paint.json` codes carried onto its rendered solid by the
  render's `paint` stage, `render/jobs.py` `paint_layout`; a layout part names its carried
  codes file, `PartSource.paint`), `locate.py` (the scene object a weak joint is in).
  `render/geometry.py` `weak_joints` finds where a print snaps; `render/meshmatch.py`
  matches points and triangles in numpy (trimesh's proximity needs rtree, not installed).
```

- [ ] **Step 3: Check the links and commit**

Run: `grep -n "2026-10-13-mesh-models-phase-3" docs/superpowers/specs/2026-10-11-mesh-models-design.md` (the plan link resolves) and `cd backend && uv run --frozen pytest -n auto` once more over the whole suite.
Expected: one match; the suite passes (skips as usual without Postgres, Temporal, openscad or the fixtures).

```bash
git add docs/superpowers/specs/2026-10-11-mesh-models-design.md CLAUDE.md
git commit -m "docs(meshmodel): phase 3 as built: paint transfer, weak joints, the carrot eval (#2146)"
```

---

## Self-review

- **Spec coverage.** §7 steps 1–3: Tasks 3 (`transfer`, `carry_paint`), 4 (the stage, the writer's `Paint`). §7's mirrored-part warning in `get_scene`: Task 5; in the render: Task 3 (`paint_warnings` in `carry_paint`'s warnings). §7's fidelity measurement: Task 6. §9: Tasks 7–9. §10's paint unit tests: Task 3 (op-free verbatim, cut faces own colour); its eval: Task 11. §6's "`analyze_geometry` gains `weak_joints`": Task 8; "the plugin's `authoring` skill": Task 10. §11 phase 3: all of it; phase 4 (§8) is not touched.
- **Placeholders.** None: every code step has its code; the one hand step (the plugin version, if the hook did not run) names the exact files and value.
- **Type consistency.** `CarriedPaint(paint, turns)`, `carry_paint(directory, params, parts)`, `read_carried(path, mesh)`, `write_carried(path, carried)`, `turned(mesh, turns)` are used with those signatures in Tasks 3, 4; `PartSource(file, solid, paint)` in Task 4; `layer_areas(vertices, faces, zs)` in Tasks 7, 8; `WeakJoint.suggestion: SplitSuggestion` with `object`, `point`, `normal`, `joint` in Tasks 8, 9, 11; `object_at(scene, mesh_of, point)` and `attribute(analysis, directory)` in Task 9; `placement(obj)` in Tasks 3, 8 (fixture helper), 9. The eval's `CARROT_WEAK_JOINT` is shaped as `WeakJoint` dumps.
- **Review Focus.** Each of the five lines has its test in the owning task (named there).
