# Print dialog and slicing

## What it does

The print dialog is spool-first: pick spools, nozzle, quality tier and plate, and
ScadBuddy derives the printer, process and filament presets, slices and starts the print.
Advanced mode adds per-side nozzle flow, any slicer process setting
(`process_overrides`), per-slot filament presets, supports, the prime tower, copies and a
project. The dialog slices in the background to show a toolpath preview, and runs print
analyzers with one-click fixes. User guide: [Print picker](../user-guide.md#print-picker).

## How it works

The browser starts a `PrintRun` workflow through `POST /print/outputs/{id}/run` (or
`/print/library/{id}/run`). The plan merges `process_overrides` with the profile values,
the template's `print_settings` and the shorthands; then each plate is sliced, by the
native Rust slicer (`slicer_backend=native`) or by Bambuddy. See
[workflows.md "Slice"](../architecture/workflows.md#slice) and
["Print to the H2C"](../architecture/workflows.md#print-to-the-h2c). Any Bambu Studio
key goes through the one generic `process_overrides`, never a per-key field (root
[`CLAUDE.md` "PR conventions"](../../CLAUDE.md#pr-conventions)).

## Code

- UI: `frontend/src/components/PrintPicker.tsx:329` (design notes `:140-162`), steps in
  `frontend/src/components/print/`, `frontend/src/components/FilamentPicker.tsx:240`,
  `frontend/src/components/print/PreviewPane.tsx:203`, `ToolpathScene.tsx`,
  `AnalyzerPanel.tsx:310`.
- Routes: `backend/scadbuddy/api/printing.py:263` (`/print`: run `:928`, check `:1410`,
  preview slice `:1491`, choices `:1697`), `backend/scadbuddy/api/process_values.py:29`,
  `backend/scadbuddy/api/filament_profiles.py:26`, `backend/scadbuddy/api/analyzers.py:102`.
- Planning: `backend/scadbuddy/bambuddy/print_run.py` (`PrintRunRequest.overrides` `:481`,
  `plan_run` `:3172`, overrides merged `:3403-3415`).
- Slicing: `backend/scadbuddy/workflows/slicing.py:182`,
  `backend/scadbuddy/slicing/presets.py:324`, `workers/rust/crates/sb-slicer/src/runner.rs:103-135`;
  Bambuddy's slicer through `backend/scadbuddy/bambuddy/dispatch.py`.
- Background slice follow: `backend/scadbuddy/preview_follow/follows.py:69`.
- Analyzers: `backend/scadbuddy/analyzers/runner.py:141`.

## Data

- Tables: `print_runs`, `print_preview_slices`, `print_slice_starts`, `native_slices`,
  `model_print_choices`, `held_local_presets`, `analyzer_decisions`.
- Blob keys: `slice-<file>-<digest>` and kind `slice` blobs.
- Settings: `slicer_backend`, `printer_print_options`, `printer_bed_types`,
  `printer_plates`.

## Tests

- Backend: `backend/tests/test_print_workflow.py`, `test_print_native_workflow.py`,
  `test_slice_replay.py`, `backend/tests/slicing/`, `backend/tests/api/test_slice_workflow.py`,
  `backend/tests/analyzers/`.
- Rust: `workers/rust/crates/sb-slicer/tests/`.
- e2e: `frontend/e2e/print.spec.ts`, `print-nozzle-plan.spec.ts`.

## Limits and known issues

- Every dialog setting as a remembered choice:
  [#6219](https://github.com/eh-homelab/ScadBuddy/issues/6219); settings per object and
  part: [#6253](https://github.com/eh-homelab/ScadBuddy/issues/6253); labels and help in
  the settings list: [#6281](https://github.com/eh-homelab/ScadBuddy/issues/6281).
- The dialog's check reads Bambuddy about 17 times
  ([#4196](https://github.com/eh-homelab/ScadBuddy/issues/4196)).
- Bambu Cloud user presets are not offered on a native run
  ([#5113](https://github.com/eh-homelab/ScadBuddy/issues/5113)).
- The native slicer's output goes through the API, not a presigned PUT
  ([#4231](https://github.com/eh-homelab/ScadBuddy/issues/4231)); a sliced plate's cover
  ignores the filament colour ([#6170](https://github.com/eh-homelab/ScadBuddy/issues/6170)).
- Analyzers beyond the built-ins are [#284](https://github.com/eh-homelab/ScadBuddy/issues/284).
