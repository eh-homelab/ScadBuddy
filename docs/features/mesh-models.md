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

- Routes: `backend/scadbuddy/api/mesh_models.py` (`POST /models/from-mesh` `:232`: a
  library file, an output or a print's file (#6916); upload `:293`; scene read, edits, eject).
- The catalogue's "Open a mesh as a model": `frontend/src/components/MeshSourceDialog.tsx`.
  It picks a print's file with Import print's picker, `components/prints/PrintSourceChoice.tsx`.
- Scene and compile: `backend/scadbuddy/meshmodel/scene.py`, `compile.py:160`,
  `ingest.py:76`, `edits.py`, `paint.py`.
- Operations: `backend/scadbuddy/meshmodel/operations.py:405-421` (`mesh_model_create`,
  `scene_edit`, `scene_eject`).
- Bundled example: `models/mesh-joint-demo/`.
- Agent tools: `agent/src/tools/meshModels.ts`.

## Data

- The models git repository (scene, generated `model.scad`, meshes).
- A release that reads mesh pointers but does not write them comes first (#7313,
  `backend/scadbuddy/library/mesh_blobs.py` `WRITE_POINTERS` is off). Once a later one
  turns them on, git holds a pointer per mesh, `meshes/<name>.3mf.blob`, and no new mesh.
  GitHub refuses a file over 100 MB
  ([docs](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github#file-size-limits));
  the one already in the history is #7314, so that alone does not make the off-box push
  succeed.
- The bytes are then on the data volume beside each pointer, and in the blob store as
  `attachment-<sha256>`, held in `blob_refs` by `model_mesh` for good. On the local store
  that is the same volume: a mesh takes twice its size, and every version of it stays.
- The off-box git copy then has pointers only. To restore a mesh from it, the blob store
  is needed too.

## Tests

- Backend: `backend/tests/meshmodel/`, `backend/tests/test_mesh_models_openscad.py`,
  `test_mesh_models_paint.py`, `backend/tests/api/test_mesh_models.py`; the meshes in
  the blob store: `backend/tests/test_history_meshes.py`,
  `backend/tests/store/test_mesh_blobs.py`.

## Limits and known issues

- None tracked in code comments. Plate-editor work that would supersede parts of the scene
  editor is the web Bambu Studio epic
  ([#6272](https://github.com/eh-homelab/ScadBuddy/issues/6272)).
