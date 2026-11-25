# Preview, render and 3MF export

## What it does

Every parameter change renders the model with OpenSCAD and shows the real result: the
per-colour parts and the bounding box. **Generate** saves an output: a multi-colour 3MF
with one closed solid per colour on its own extruder, plate cover images and a layout
sized for the target printer's plate. **Download 3MF** and **Send to Bambuddy** act on an
output. User guide: [Customizing](../user-guide.md#customizing),
[Multi-colour output](../user-guide.md#multi-colour-output).

## How it works

The render path (API → Temporal `TemplatePipeline` → render worker → openscad → split by
colour → closed solids → `write_plates_3mf` → blob store) is drawn in
[workflows.md "Render, split by colour, 3MF"](../architecture/workflows.md#render-split-by-colour-3mf).
The preview GLB is built from the open split meshes; the 3MF from the closed per-colour
solids. **Send** uploads the output's 3MF into the print library as the `send` operation;
printing is the print dialog's job.

## Code

- UI: `frontend/src/components/ActionBar.tsx:126` (Generate `:632`, Download 3MF `:666`,
  Send `:671`, Print `:675-689`), `frontend/src/components/Preview.tsx:192` (three.js),
  `frontend/src/components/SendDialog.tsx:33`.
- Routes: `POST /api/v1/models/{slug}/render` (`backend/scadbuddy/api/jobs.py:401`),
  `GET /api/v1/jobs/{id}`, `backend/scadbuddy/api/outputs.py:141`.
- Render: `backend/scadbuddy/render/runner.py:653`, `backend/scadbuddy/render/split.py:172`,
  `backend/scadbuddy/render/solids.py:92`, `backend/scadbuddy/render/jobs.py`,
  `backend/scadbuddy/render/bambu3mf.py:862`, `backend/scadbuddy/render/glb.py`,
  `backend/scadbuddy/render/thumbnail.py`.
- Workflows: `backend/scadbuddy/workflows/pipelines.py:386` (`RenderPiece`), `:524`
  (`TemplatePipeline`).

## Data

- Tables: `render_jobs`, `blob_refs`, `piece_geometry`, `outputs` (each output's record,
  #7255; it also lists them, #7257), `output_files` and `output_derived` (#7256),
  `output_cutover` and `output_claims` (#7257), and `output_index` (still written until
  #7257's second release).
- Blob keys: a piece's key (64-hex), `output-<job>-<i>`; a saved output's bytes,
  `output-<id>-<role>` and `output-<id>-file-<hash>`, kind `output` (#7256; the folder
  under `data/outputs/` stays the fallback until #7257's second release; on `local` the
  blobs are under `data/output-blobs/`)
  ([data.md](../architecture/data.md#keys-and-content-addressing)).

## Tests

- Backend: `backend/tests/test_split.py`, `test_solids.py`, `test_bambu3mf.py`,
  `test_glb.py`, `test_thumbnail.py`, `test_workflows.py`, `backend/tests/api/test_jobs.py`,
  `backend/tests/api/test_real_render.py` (needs openscad).
- e2e: `frontend/e2e/customize.spec.ts`, `preview-overlays.spec.ts`, `phone-preview.spec.ts`,
  `downloads.spec.ts`, `real-backend.spec.ts`.
- Bundled models: `models/*/verify.sh`.

## Limits and known issues

- A Rust tool for thumbnails and the split is
  [#3824](https://github.com/eh-homelab/ScadBuddy/issues/3824).
- A template plate is one object, so print by object does nothing
  ([#6245](https://github.com/eh-homelab/ScadBuddy/issues/6245)).
