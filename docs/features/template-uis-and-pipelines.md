# Template-owned UIs and pipelines

## What it does

A template can replace the generated form with its own interface (a panel or a whole
page), and can run its own Python pipeline: render several pieces, pack them onto plates
and write outputs (for example `models/dollhouse-kit`, `models/maze-puzzle`).

## How it works

`model.json` declares `ui: {module, slot, api}` and `pipeline: {module, api}`
(`backend/scadbuddy/library/catalogue.py:264-275`, `:368-382`). The UI module is loaded by
the browser into a shadow root, not sandboxed
([frontend.md "Template-owned UIs"](../architecture/frontend.md#template-owned-uis)). The
pipeline's `pipeline.py` runs inside the `TemplatePipeline` workflow on the render worker,
and its `activities.py` in a child process
([workflows.md](../architecture/workflows.md#render-split-by-colour-3mf)). The design is
the [template-pipelines spec](../superpowers/specs/2026-09-27-template-pipelines-design.md).

## Code

- Frontend host: `frontend/src/template-ui/TemplateUi.tsx:63`, `host.ts:85`,
  `types.ts:14-18`, `elements.ts`.
- UI files: `GET /api/v1/models/{slug}/ui/{path}` and the versioned
  `/models/{slug}/versions/{version}/ui/{path}` (`backend/scadbuddy/api/template_ui.py:98-118`).
- Pipeline: `backend/scadbuddy/workflows/pipelines.py:524`, `ctx.py`,
  `pipeline_activities.py:270`, `template_process.py:106-130`; the import surface for
  templates `backend/scadbuddy/template.py`.
- Input migration: `POST /api/v1/models/{slug}/inputs/migrate` (`backend/scadbuddy/api/jobs.py:900`).

## Data

- Blob keys `act-<sha>` for template activity outputs, `output-<job>-<i>` for outputs.

## Tests

- Backend: `backend/tests/test_template_pipeline.py`, `test_template_pipeline_replay.py`,
  `test_pipeline_sandbox.py`, `test_template_activities.py`, `test_dollhouse_pipeline.py`,
  `test_verify_pipeline.py`.
- e2e: `frontend/e2e/template-ui.spec.ts`, `dollhouse-designer.spec.ts`.
- Each pipeline template's `verify.sh` runs the pipeline on an in-memory worker
  (`backend/scadbuddy/workflows/verify_pipeline.py`).

## Limits and known issues

- Template Python runs in the render worker's pod with its credentials
  ([trust-boundaries.md](../security/trust-boundaries.md#where-untrusted-code-runs));
  moving it out is [#5296](https://github.com/eh-homelab/ScadBuddy/issues/5296).
- Storefront listings refuse pipeline templates
  ([#5701](https://github.com/eh-homelab/ScadBuddy/issues/5701)).
- Declared pages, the generalisation of template UIs, are
  [#4254](https://github.com/eh-homelab/ScadBuddy/issues/4254).
