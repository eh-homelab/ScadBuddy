# Arrange and plates

## What it does

Lay several outputs or library files out on plates for one print: pick sources, choose a
plate and printer, and save the arranged result as a new output. The plate size and the
H2C's dual-nozzle usable area come from the printer profile.

## How it works

Arrange is the same `TemplatePipeline` workflow as a render, with `kind == "arrange"`: it
packs the chosen pieces with `allow_own=False` and writes an output
([workflows.md](../architecture/workflows.md#render-split-by-colour-3mf)). Packing is a
rectangle packer with a fixed gap. For the H2C's plate rules (usable area, prime-tower
keep-out) see the repo skill `.claude/skills/h2c-plate-layout`.

## Code

- UI: `frontend/src/components/ArrangeDialog.tsx:166`, `ArrangedPlates.tsx`,
  `ArrangeSourcePicker.tsx`, `frontend/src/lib/arrange.ts`; page `/edit/:outputId`
  (`frontend/src/App.tsx:108`).
- Route: `POST /api/v1/outputs/arrange` (`backend/scadbuddy/api/outputs.py:728`) →
  `RenderService.arrange` (`backend/scadbuddy/render/submit.py:460-485`).
- Packing: `backend/scadbuddy/workflows/arrange.py:212`, `packing.py:16` (`GAP_MM`).
- Plates: `backend/scadbuddy/api/plates.py:18`, `backend/scadbuddy/render/plate_profiles.py`.

## Data

- Outputs (`output_index`), blob keys `output-<job>-<i>`; library pieces read through
  `backend/scadbuddy/render/objects3mf.py`.

## Tests

- Backend: `backend/tests/test_arrange_packing.py`, `test_arrange_workflow.py`,
  `test_arrange_outputs.py`, `test_arrange_recolour.py`, `backend/tests/api/test_arrange_api.py`.

## Limits and known issues

- The 5 mm gap ignores Bambu's by-object clearance
  ([#6246](https://github.com/eh-homelab/ScadBuddy/issues/6246)).
- Auto Arrange does not reserve the prime tower's real footprint
  ([#3770](https://github.com/eh-homelab/ScadBuddy/issues/3770)); dragging with clearance
  halos is [#3772](https://github.com/eh-homelab/ScadBuddy/issues/3772).
- Saving an arranged output waits on reading what a render worker just stored
  ([#2811](https://github.com/eh-homelab/ScadBuddy/issues/2811)).
