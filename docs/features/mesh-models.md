# Mesh models

## What it does

Make a model from a mesh file (an STL or 3MF) instead of OpenSCAD source. The objects
become a scene you can edit (move, colour, cut) and later eject to plain OpenSCAD.

## How it works

A mesh model's scene (`scene.json`) is compiled to a `model.scad` that imports the meshes,
so it renders like any template. Creating, editing and ejecting are `library` operations
run by the API's library worker ([workflows.md](../architecture/workflows.md#operations-one-shape-for-writes)).
Design: the [mesh-models spec](../superpowers/specs/2026-10-11-mesh-models-design.md).

## Code

- Routes: `backend/scadbuddy/api/mesh_models.py` (`POST /models/from-mesh` `:143`, upload
  `:179`, scene read, edits, eject).
- Scene and compile: `backend/scadbuddy/meshmodel/scene.py`, `compile.py:160`,
  `ingest.py:76`, `edits.py`, `paint.py`.
- Operations: `backend/scadbuddy/meshmodel/operations.py:405-421` (`mesh_model_create`,
  `scene_edit`, `scene_eject`).
- Bundled example: `models/mesh-joint-demo/`.
- Agent tools: `agent/src/tools/meshModels.ts`.

## Data

- The models git repository (scene, meshes, generated `model.scad`).

## Tests

- Backend: `backend/tests/meshmodel/`, `backend/tests/test_mesh_models_openscad.py`,
  `test_mesh_models_paint.py`, `backend/tests/api/test_mesh_models.py`.

## Limits and known issues

- None tracked in code comments. Plate-editor work that would supersede parts of the scene
  editor is the web Bambu Studio epic
  ([#6272](https://github.com/eh-homelab/ScadBuddy/issues/6272)).
