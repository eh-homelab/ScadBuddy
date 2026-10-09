# Arrange: plain library files as objects (#1863) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Bambuddy library file with no ScadBuddy output behind it arranges beside outputs: its
objects are read from the 3MF the library print path fetches, and packed like any output's.

**Architecture:** The API reads the file (`LibrarySource.fetch_3mf`, #1882: the 3MF, an STL wrapped
in ScadBuddy's own), parses it into objects (`render/objects3mf.py`: one per build item, or per
distinct orientation of one object, with a count; each object's closed meshes per filament colour)
and writes each object as a piece blob (`layout.json` + one split 3MF, the shape `PlateLayout.load`
reads), keyed by the file's sha256 so the same file gives the same keys. The objects become
`ManifestObject`s (`library_file_id` set) and go through `arrange_inputs` with the outputs'; the
workflow is unchanged, since `write_output` fetches every Part by key. The job holds a ref on
each piece from its submit, and the saved output holds them as its manifest's parts.
`GET /print/library/{file_id}/objects` lists a file's objects for the dialog.

**Decisions:**
- STL: arrangeable, one object of one filament (#FFFFFF, as a print wraps it).
- Sliced (`gcode.3mf`, or a 3MF holding plate gcode): refused, 422 `library_file_not_arrangeable`
  naming the file and why; print it from Bambuddy.
- Refused too, by the same code: past the download cap, no geometry, painted (multi-colour paint on
  one part), negative parts, more than 200 distinct objects. Modifiers and support blockers or
  enforcers are dropped (they print nothing; Arrange writes no per-object settings anyway).
- Filing: a mix with outputs files under one of their templates, as before. A result of library
  files only needs `slug`, any template (422 without it, 404 for an unknown one).

**Tech Stack:** FastAPI + trimesh (backend), React 19 + msw/vitest (frontend), Hono + zod (agent).

## Global Constraints

- Generated API files are never committed; the new GET gets a `coverage.ts` entry (the agent's
  `arrange` names whole files).
- A library file is untrusted: every archive read is capped (`MAX_UNCOMPRESSED_BYTES`).

---

### Task 1: Parser (`backend/scadbuddy/render/objects3mf.py`)

- [ ] Failing tests (`tests/test_objects3mf.py`): ScadBuddy's own 3MF reads back as its assemblies with
      their colours; a Bambu-style file with two items of one object is one object counted twice;
      a turned item is its own object; component and item transforms apply; extruder per part from
      `model_settings.config` and colour from `filament_colour`; a core 3MF's basematerials colour it;
      sliced, painted, negative-part, and empty files refuse; a modifier is dropped; pieces written by
      `write_piece` load back through `PlateLayout.load`.
- [ ] Implement; ruff, mypy, pytest.

### Task 2: Route (`api/outputs.py`, `bambuddy/library_objects.py`, `api/library_print.py`)

- [ ] Failing tests in `tests/api/test_arrange_api.py`: a plain library file arranges beside an
      output (pieces published, keys in the job's items, provenance `library_file_id`, job refs);
      a sliced file is 422 with the code and why; library-only needs `slug`; the GET lists objects.
- [ ] Implement; commit `feat(arrange): plain library files arrange from their 3MF`.

### Task 3: Frontend and agent

- [ ] Dialog: a plain file's objects listed (GET objects) with a count each, sent as
      `{library_file_id, part, count}`; a refusal is shown as left out with its reason; with no
      outputs, File under lists every template. msw handler + vitest.
- [ ] Agent: `arrange` description and the refusal message; coverage entry for the GET.
- [ ] lint, typecheck, test, build; commit; PR `Fixes #1863`.
