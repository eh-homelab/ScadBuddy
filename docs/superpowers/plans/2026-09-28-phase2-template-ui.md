# Phase 2: Template-shipped UI and inputs. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A template can ship its own UI (`ui/index.js`, mounted in the panel or page slot through a versioned `Host` object), and presets, outputs and renders all carry one JSON *inputs* value in place of bare `params`. `maze-puzzle` and `dollhouse-kit` are the first two templates to use it (epic #425).

**Architecture:** The backend gains `render/inputs.py`, which normalizes inputs and reads a bare `params` body as `{"params": …, "v": 0}`. `RenderRequest`, presets (`saved_presets.inputs`, model.json `presets[].inputs`) and outputs (`inputs.json`) store it. The default pipeline still renders `inputs["params"]` against `model.scad`. `model.json` gains a `ui` declaration, validated leniently so that a bad one costs only the custom UI. Two routes serve `ui/**` from the live template or from a pinned revision's export. Both send `nosniff`, a `sandbox` CSP and revision-bound caching. The SPA document gets a page CSP that stops cross-origin script and subresource/XHR beacons (Google Fonts style and font files excepted). It does not stop same-origin API use or navigation. The frontend splits page state into `params` plus `extra` (every other inputs key). `TemplateUi` loads the module with `import()`, mounts it into an open shadow root through `createHost()`, and falls back to `ParameterPanel` with a banner on any failure. The host's widgets are custom elements (`sb-param`, `sb-preview`, `sb-generate`) that register with the mounting `TemplateUi`, which renders the existing React widgets into them with portals.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, psycopg 3, pytest; React 19, Vite, TypeScript, vitest + msw, Playwright; plain ES modules (no build step) for template UIs.

**Spec:** `docs/superpowers/specs/2026-09-27-template-pipelines-design.md` (§4, §8.1, §9, §10, §11 item 2). Phase 1 interfaces this builds on: `docs/superpowers/plans/2026-09-28-phase1-render-on-temporal.md` (`Job.inputs`, `JobProjection.submit`, `RenderService.submit`, `piece_key`, the Rollout addendum).

## Global Constraints

- `model.json` gains one optional object: `"ui": {"module": "ui/index.js", "slot": "panel", "api": 1}` (§4.1).
- `slot`: `"panel"` replaces `ParameterPanel`; the host keeps preview, presets, history, Generate and Print. `"page"` gets the whole content area under the header; the host's preview and Generate are available as custom elements (§4.1).
- "No `ui` → today's generated form." (§4.1) A template with no `ui` is pixel-identical to today (epic #425 "Done when").
- `export function mount(root: ShadowRoot, host: Host, ctx: {slot, version, theme}): (() => void) | void`: mounted into a shadow root for style isolation, **not** sandboxed (§4.2, §9).
- "If `mount` throws, or the declared `api` major is unsupported, the host renders the generated form with an error banner naming the template file and the error. A broken UI never bricks a template." (§4.2)
- "The Customize page shows the template's origin (built-in / mine / imported from URL) beside a custom UI." (§4.2)
- Host API v1 members: `inputs.get/set/subscribe`, `schema(file?)`, `files.url(path)`, `generate()`, `openPrint(outputId)`, `presets.list/save/load`, `describe?` (§4.3).
- "The host supports the current major and the previous one; an unsupported major falls back (UI: the generated form with a banner)." (§8.1) "Minor additions never break a template."
- "`inputs` is the one piece of state. It is JSON the template owns. For a template with no custom UI, inputs are exactly the parameter values, so today's `{params}` is one shape of inputs. Presets store inputs; outputs record inputs; reopening an output restores inputs; the agent reads and writes inputs." (§4.3)
- `POST /models/{slug}/render`: body `{inputs, version?, supersedes?}`; `params` still accepted and wrapped as `{"params": …}` (§10).
- Presets: `params` → `inputs` with `v`; old ones read as `{"params": …, "v": 0}` (§10).
- "Decided: template code is **not sandboxed**." (§9) Containment is by reach. This phase adds a page CSP, which stops cross-origin script loading and subresource/XHR beacons (Google Fonts style and font files excepted). It does not stop same-origin API abuse, navigation or WebRTC exfiltration. Served `ui/` files also carry a `sandbox` CSP.
- Backend: every task ends with `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest` green in `backend/` (CLAUDE.md). Frontend: `pnpm lint && pnpm typecheck && pnpm test && pnpm build` in `frontend/`. Agent: the same four in `agent/`.
- Generated API files (#492) are never committed. After any API model or route change, regenerate locally in this order before frontend or agent typecheck: `cd frontend && pnpm gen:api`, then `cd agent && pnpm gen:api` (each exports `backend/openapi.json` with uv first). Run `pnpm exec msw init public --save` only after an msw bump; this plan has none.
- Backend schema changes are a NEW file `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one (CLAUDE.md "Migrations").
- Every `/api/v1` operation needs an agent tool or an `agent/src/tools/coverage.ts` `NOT_A_TOOL` entry, or `agent/test/coverage.test.ts` fails (CLAUDE.md).
- No docstrings or comments on unchanged code. Conventional-commit PR titles, body `Part of #425` (the last PR says `Fixes #425`).
- Phase 1 names are fixed: `RenderService.submit(slug, params, *, model_version, supersedes) -> Job` in `render/submit.py`, `RenderDep`, `Job.inputs: dict[str, Any]`, `render_key(slug, params, model_version)`, `piece_key(slug, revision, file, params)`. Phase 2 keeps `render_key` and `piece_key` over `params` only. See "Disagreements" at the end.

## Review Focus

1. **A `ui/` path that tries to leave `ui/`** (`../model.scad`, `%2e%2e/model.scad`, a symlink `ui/x.js -> ../model.scad`, `ui/page.html`): the answer is 404 and never the template's source or an HTML document. Pinned in Task 4 by `test_ui_paths_never_leave_ui`.
2. **A metadata edit, or a malformed `ui`, on a template with a custom UI.** A `PATCH` of name/tags keeps `ui` in `model.json`. `"ui": {"slot": "sidebar"}` still lists and customizes the template, with the problem in `ui_error`. Pinned in Task 4 by `test_a_patch_keeps_ui` and `test_a_malformed_ui_costs_only_the_ui`.
3. **A UI that keeps writing after it is unmounted** (a timer, or a subscription it never released) **or sets a parameter `model.scad` does not have.** The late write changes nothing on the page, and in particular not the next template's state. The unknown name throws `HostInputError` naming the parameter. Pinned in Task 6 by `ignores writes after dispose` and `refuses a parameter the schema lacks`.
4. **Reopening an output or applying a preset saved before phase 2** (only `params.json`; a `saved_presets` row whose `inputs` is `{}`): it reads as `{"params": …, "v": 0}`. A custom UI's extra keys survive a save → reopen round trip. Pinned in Task 3 by `test_an_output_from_before_inputs_reads_as_params_v0` and in Task 5 by `reopens an output with its UI state`.
5. **Two submits with the same params but different UI state, coalesced onto one job.** The output records the inputs the client saved, not the coalesced job's. Inputs whose `params` the job did not render are refused with 422. Pinned in Task 3 by `test_an_output_records_the_inputs_it_was_saved_with` and `test_an_output_refuses_inputs_the_job_did_not_render`.

---

## File Structure

Backend:
- `backend/scadbuddy/render/inputs.py` (new): `InputsError`, `MAX_INPUTS_BYTES`, `normalize_inputs`, `legacy_inputs`. The one definition of what inputs may be.
- `backend/scadbuddy/api/jobs.py`: `RenderRequest.inputs`, `JobStatus.inputs`, `render_model` wiring.
- `backend/scadbuddy/render/submit.py` (phase 1): `RenderService.submit(..., inputs=)`.
- `backend/scadbuddy/library/presets.py`, `backend/scadbuddy/api/presets.py`, new migration `…_saved_presets_inputs.sql`: presets store inputs.
- `backend/scadbuddy/library/outputs.py`, `backend/scadbuddy/api/outputs.py`: outputs record inputs (`inputs.json`).
- `backend/scadbuddy/library/catalogue.py`: `UiDeclaration`, `ModelMeta.ui`, `ModelMeta.ui_error`, `ModelRecord.ui_error`.
- `backend/scadbuddy/api/template_ui.py` (new): the two `ui/` routes; discovered by `main.py` `_api_router()`, no registration.
- `backend/scadbuddy/api/static.py`: `PAGE_CSP` on the SPA's responses.

Frontend:
- `frontend/src/lib/inputs.ts` (new): `Json`, `JsonObject`, `InputsExtra`, `NO_EXTRA`, `splitInputs`, `joinInputs`, `mergePatch`, `getPath`, `setPath`.
- `frontend/src/lib/saveOutput.ts` (new): the Generate body, shared by `ActionBar` and the host.
- `frontend/src/template-ui/types.ts`, `host.ts`, `loadModule.ts`, `styles.ts`, `TemplateUi.tsx` (new): Host v1 and mounting.
- `frontend/src/template-ui/elements.ts`, `HostElementContent.tsx` (new): custom elements and their portal content.
- `frontend/src/pages/CustomizePage.tsx`: the shell (panel/page slot, fallback banner, origin badge, host wiring).
- `frontend/src/components/ActionBar.tsx`, `PresetPicker.tsx`, `frontend/src/lib/useRenderJob.ts`, `frontend/src/lib/presets.ts`, `frontend/src/api/client.ts`: inputs plumbing.
- `frontend/src/mocks/handlers.ts`, `fixtures.ts`, `templateUi.ts` (new): msw for inputs and `ui/` modules.
- `frontend/e2e/template-ui.spec.ts` (new), `frontend/vite.config.ts`, `frontend/page-csp.txt` (new).

Templates, agent, docs:
- `models/maze-puzzle/ui/index.js`, `models/maze-puzzle/model.json`.
- `models/dollhouse-kit/ui/index.js`, `models/dollhouse-kit/ui/pieces.js`, `models/dollhouse-kit/model.json`.
- `agent/src/tools/customizer.ts`, `agent/src/tools/coverage.ts`, `agent/test/tools.test.ts`.
- `plugins/scadbuddy/skills/authoring/SKILL.md`, `CLAUDE.md`.

## Task order and parallelism

Task 1 comes first because it defines `render/inputs.py`. After it, Tasks 2, 3 and 4 can run in parallel worktrees: they touch disjoint files, apart from `api/outputs.py`, which only Task 3 touches. Task 5 needs Tasks 1–3 (API shapes), Task 6 needs Task 5, and Task 7 needs Task 6. Task 8 needs Tasks 4 and 7. Tasks 9 and 10 both need Task 8 and can run in parallel with each other. Task 11 needs Tasks 1 and 4 and can run any time after them. Suggested PRs: (1) Tasks 1–4, backend; (2) Tasks 5–8, frontend shell; (3) Task 9; (4) Task 10; (5) Task 11.

---

### Task 1: Inputs on the render request and the job

**Files:**
- Create: `backend/scadbuddy/render/inputs.py`
- Modify: `backend/scadbuddy/api/jobs.py` (`RenderRequest`, `JobStatus`, `_job_status`, `render_model`)
- Modify: `backend/scadbuddy/render/submit.py` (`RenderService.submit`)
- Test: `backend/tests/test_inputs.py` (new), `backend/tests/api/test_jobs.py`

**Interfaces:**
- Consumes: phase 1 `RenderService.submit(slug, params, *, model_version, supersedes) -> Job`, `Job.inputs`, `RenderDep`.
- Produces:
  ```python
  # render/inputs.py
  MAX_INPUTS_BYTES: int = 65536
  class InputsError(ValueError): ...
  def normalize_inputs(inputs: Mapping[str, Any] | None, params: Mapping[str, ParamValue] | None) -> dict[str, Any]
      # -> {"params": {...}, "v": int, **other keys}; raises InputsError
  def legacy_inputs(params: Mapping[str, ParamValue]) -> dict[str, Any]   # {"params": dict(params), "v": 0}
  # api/jobs.py
  class RenderRequest: inputs: dict[str, Any] | None; params: dict[str, ParamValue] | None; version; supersedes
  class JobStatus: + inputs: dict[str, Any]
  # render/submit.py
  async def RenderService.submit(self, slug, params, *, model_version, supersedes, inputs: Mapping[str, Any] | None = None) -> Job
  ```

- [ ] **Step 1: Write the failing unit tests**

Create `backend/tests/test_inputs.py`:

```python
"""Template inputs (spec 2026-09-27 §4.3, §10)."""

from __future__ import annotations

import pytest

from scadbuddy.render.inputs import MAX_INPUTS_BYTES, InputsError, legacy_inputs, normalize_inputs


def test_bare_params_are_read_as_version_zero_inputs() -> None:
    assert normalize_inputs(None, {"width": 12}) == {"params": {"width": 12}, "v": 0}


def test_nothing_at_all_is_empty_params() -> None:
    assert normalize_inputs(None, None) == {"params": {}, "v": 0}


def test_ui_keys_are_kept_beside_params() -> None:
    raw = {"params": {"width": 12}, "house": {"storeys": 2}, "v": 3}
    assert normalize_inputs(raw, None) == raw


def test_inputs_without_params_get_empty_params() -> None:
    assert normalize_inputs({"tab": "lid"}, None) == {"tab": "lid", "params": {}, "v": 0}


def test_params_that_agree_with_inputs_are_accepted() -> None:
    assert normalize_inputs({"params": {"width": 1}}, {"width": 1})["params"] == {"width": 1}


def test_params_that_disagree_with_inputs_are_refused() -> None:
    with pytest.raises(InputsError, match="disagree"):
        normalize_inputs({"params": {"width": 1}}, {"width": 2})


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"params": [1, 2]}, "inputs.params must be an object"),
        ({"params": {"width": [1]}}, "inputs.params.width must be a number, string or boolean"),
        ({"params": {"width": None}}, "inputs.params.width must be a number, string or boolean"),
        ({"params": {}, "v": -1}, "inputs.v must be a non-negative integer"),
        ({"params": {}, "v": True}, "inputs.v must be a non-negative integer"),
    ],
)
def test_malformed_inputs_are_refused(raw: dict[str, object], message: str) -> None:
    with pytest.raises(InputsError, match=message):
        normalize_inputs(raw, None)


def test_oversized_inputs_are_refused() -> None:
    with pytest.raises(InputsError, match=f"at most {MAX_INPUTS_BYTES}"):
        normalize_inputs({"params": {}, "blob": "x" * MAX_INPUTS_BYTES}, None)


def test_legacy_inputs() -> None:
    assert legacy_inputs({"width": 3}) == {"params": {"width": 3}, "v": 0}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run --frozen pytest tests/test_inputs.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'scadbuddy.render.inputs'`

- [ ] **Step 3: Implement `render/inputs.py`**

```python
"""Template inputs (spec 2026-09-27 §4.3): the one piece of customizer state.

A JSON object the template owns. ``params`` is the reserved key the default pipeline
renders: the `-D` values of ``model.scad``. ``v`` is the template's inputs version,
0 when the template declares none (phase 4's ``migrate`` reads it). Every other key
is the template UI's own state: stored with presets and outputs, never rendered.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from scadbuddy.render.schema import ParamValue

#: A preset or output holds a few KB of state; this is far past any real UI's and
#: keeps one request from filling a jsonb column or an output directory.
MAX_INPUTS_BYTES = 65536


class InputsError(ValueError):
    """Inputs that are not a template's inputs; the message names the key."""


def legacy_inputs(params: Mapping[str, ParamValue]) -> dict[str, Any]:
    """What a params-only record (a pre-inputs preset, output or request) reads as."""
    return {"params": dict(params), "v": 0}


def normalize_inputs(
    inputs: Mapping[str, Any] | None, params: Mapping[str, ParamValue] | None
) -> dict[str, Any]:
    if inputs is None:
        return legacy_inputs(params or {})
    result = dict(inputs)
    raw = result.get("params", {})
    if not isinstance(raw, dict):
        raise InputsError("inputs.params must be an object of parameter values")
    checked: dict[str, ParamValue] = {}
    for name, value in raw.items():
        if not isinstance(value, bool | int | float | str):
            raise InputsError(f"inputs.params.{name} must be a number, string or boolean")
        checked[name] = value
    if params and dict(params) != checked:
        raise InputsError("params and inputs.params disagree; send inputs only")
    result["params"] = checked
    version = result.setdefault("v", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise InputsError("inputs.v must be a non-negative integer")
    size = len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > MAX_INPUTS_BYTES:
        raise InputsError(f"inputs are {size} bytes; at most {MAX_INPUTS_BYTES}")
    return result
```

- [ ] **Step 4: Run the unit tests**

Run: `cd backend && uv run --frozen pytest tests/test_inputs.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Write the failing API tests**

Append to `backend/tests/api/test_jobs.py`, reusing that module's `client` and `model` fixtures and imports (add `import pytest` and `from fastapi.testclient import TestClient` if they are not already imported):

```python
def test_a_render_takes_inputs_and_the_job_reports_them(client: TestClient, model: str) -> None:
    body = {"inputs": {"params": {"width": 12}, "ui": {"tab": "lid"}}}
    accepted = client.post(f"/api/v1/models/{model}/render", json=body)
    assert accepted.status_code == 202
    job = client.get(accepted.json()["status_url"]).json()
    assert job["params"] == {"width": 12}
    assert job["inputs"] == {"params": {"width": 12}, "ui": {"tab": "lid"}, "v": 0}


def test_a_params_body_is_still_accepted_as_inputs(client: TestClient, model: str) -> None:
    accepted = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert accepted.status_code == 202
    job = client.get(accepted.json()["status_url"]).json()
    assert job["inputs"] == {"params": {"width": 12}, "v": 0}


@pytest.mark.parametrize(
    "body",
    [
        {"inputs": {"params": {"width": 1}}, "params": {"width": 2}},
        {"inputs": {"params": {"nope": 1}}},
        {"inputs": {"params": {"width": [1]}}},
        {"inputs": {"params": {}, "blob": "x" * 70000}},
    ],
)
def test_bad_inputs_are_refused_before_a_job_exists(
    client: TestClient, model: str, body: dict[str, object]
) -> None:
    assert client.post(f"/api/v1/models/{model}/render", json=body).status_code == 422
```

- [ ] **Step 6: Run to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test uv run --frozen pytest tests/api/test_jobs.py -q -k "inputs or params_body"`
Expected: FAIL. `job["inputs"]` is a `KeyError`, and the disagreeing body is accepted with 202.

- [ ] **Step 7: Wire inputs through the route and `RenderService.submit`**

In `backend/scadbuddy/api/jobs.py`, add `from typing import Any` and `from scadbuddy.render.inputs import InputsError, normalize_inputs`. Replace `RenderRequest`'s `params` line with:

```python
    #: Template inputs (spec 2026-09-27 §4.3). Their `params` are what is rendered.
    inputs: dict[str, Any] | None = None
    #: The body before inputs: still accepted, and read as `{"params": …, "v": 0}`.
    params: dict[str, ParamValue] | None = None
```

In `JobStatus`, after `params`:

```python
    #: What the job was submitted with (spec §4.3); `{"params": …}` for a job the
    #: legacy queue inserted without any.
    inputs: dict[str, Any] = Field(default_factory=dict)
```

In `_job_status`, after `params=job.params,`:

```python
        inputs=job.inputs or {"params": job.params},
```

In `render_model`, replace everything from `require_valid_params(schema, body.params)` up to and including the `render.submit(...)` call with the following. The `queue` → `render: RenderDep` rename is phase 1 Task 7's; the dependency parameter is already `render` when this task starts.

```python
    try:
        inputs = normalize_inputs(body.inputs, body.params)
    except InputsError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    params = inputs["params"]
    require_valid_params(schema, params)
    try:
        # A `file` parameter's value must name an upload or one of the revision's
        # own sample files (#204): checked here, so a bad one is a 422 rather than a
        # job that fails later or renders without it.
        await asyncio.to_thread(file_assets, schema, params, assets, source.scad.parent)
    except ValueError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None

    # Refused only when SCADBUDDY_RENDER_QUEUE_MAX is set and reached; by default
    # the queue accepts every render and works through them.
    try:
        job = await render.submit(
            slug,
            params,
            inputs=inputs,
            model_version=source.version,
            supersedes=body.supersedes,
        )
```

In `backend/scadbuddy/render/submit.py`, give `submit` the keyword and use it:

```python
    async def submit(
        self,
        slug: str,
        params: Mapping[str, ParamValue],
        *,
        model_version: str | None,
        supersedes: str | None,
        inputs: Mapping[str, Any] | None = None,
    ) -> Job:
        job = Job(id=uuid.uuid4().hex, slug=slug, params=dict(params),
                  inputs=dict(inputs) if inputs is not None else {"params": dict(params)},
                  model_version=model_version, created_at=now())
```

The rest of `submit` does not change. `render_key` stays over `params`, so two submits that differ only in UI state still coalesce. Task 3 is what keeps the output's inputs right when they do. If the legacy `RenderQueue` from the Rollout addendum still exists when this lands, give its `submit` the same `inputs` keyword and pass it into its `Job(...)` in the same way.

- [ ] **Step 8: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_inputs.py tests/api/test_jobs.py tests/test_submit.py -q`
Expected: PASS

- [ ] **Step 9: Lint, types, full suite, regenerate the clients**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest -q`
Expected: all green.
Run: `cd frontend && pnpm gen:api && pnpm typecheck`, then `cd agent && pnpm gen:api && pnpm typecheck`
Expected: both typechecks PASS. Every schema change here is an optional field, and PR (1) runs the CI `frontend` and `agent` jobs against the regenerated schema, so red here is a bug to fix in this task. Nothing generated is committed.

- [ ] **Step 10: Commit**

```bash
git add backend/scadbuddy/render/inputs.py backend/scadbuddy/api/jobs.py backend/scadbuddy/render/submit.py backend/tests/test_inputs.py backend/tests/api/test_jobs.py
git commit -m "feat(render): renders take template inputs; params still accepted (#425)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Presets store inputs

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_saved_presets_inputs.sql` (name it with `date -u +%Y%m%dT%H%MZ`)
- Modify: `backend/scadbuddy/library/presets.py` (`_PresetBody`, `ParamPresetUpdate`, `ParamPreset`, `PresetStore.template_presets`, `_view`, `create`, `update`, `copy`)
- Modify: `backend/scadbuddy/api/presets.py` (the duplicate route's `ParamPresetCreate`)
- Test: `backend/tests/api/test_presets.py`

**Interfaces:**
- Consumes: `normalize_inputs`, `legacy_inputs`, `InputsError` (Task 1).
- Produces:
  ```python
  class _PresetBody: name; params: dict[str, ParamValue]; inputs: dict[str, Any] | None
      # after validation: inputs is normalized and params == inputs["params"]
  class ParamPresetUpdate: name; params: dict | None; inputs: dict[str, Any] | None
  class ParamPreset: + inputs: dict[str, Any] = Field(default_factory=dict)
  # saved_presets gains column inputs jsonb NOT NULL DEFAULT '{}'; params is still written.
  ```

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/api/test_presets.py` (it already uses `client` and `model`; add imports for `json`, `DataPaths` and `TestClient` if they are missing):

```python
def test_a_preset_saves_inputs_and_reads_them_back(client: TestClient, model: str) -> None:
    body = {"name": "Lid", "inputs": {"params": {"width": 12}, "ui": {"tab": "lid"}}}
    created = client.post(f"/api/v1/models/{model}/presets", json=body)
    assert created.status_code == 201
    preset = created.json()
    assert preset["params"] == {"width": 12}
    assert preset["inputs"] == {"params": {"width": 12}, "ui": {"tab": "lid"}, "v": 0}
    listed = client.get(f"/api/v1/models/{model}/presets").json()
    assert [p["inputs"] for p in listed if p["origin"] == "mine"] == [preset["inputs"]]


def test_a_params_only_save_reads_as_version_zero_inputs(client: TestClient, model: str) -> None:
    created = client.post(
        f"/api/v1/models/{model}/presets", json={"name": "Wide", "params": {"width": 20}}
    ).json()
    assert created["inputs"] == {"params": {"width": 20}, "v": 0}


def test_a_params_only_update_keeps_the_ui_state(client: TestClient, model: str) -> None:
    body = {"name": "Lid", "inputs": {"params": {"width": 12}, "ui": {"tab": "lid"}}}
    preset = client.post(f"/api/v1/models/{model}/presets", json=body).json()
    updated = client.patch(
        f"/api/v1/models/{model}/presets/{preset['id']}", json={"params": {"width": 14}}
    ).json()
    assert updated["inputs"] == {"params": {"width": 14}, "ui": {"tab": "lid"}, "v": 0}


def test_preset_inputs_are_checked_as_a_render_is(client: TestClient, model: str) -> None:
    bad = {"name": "Bad", "inputs": {"params": {"nope": 1}}}
    assert client.post(f"/api/v1/models/{model}/presets", json=bad).status_code == 422
    clash = {"name": "Clash", "params": {"width": 1}, "inputs": {"params": {"width": 2}}}
    assert client.post(f"/api/v1/models/{model}/presets", json=clash).status_code == 422


def test_template_presets_carry_inputs_or_read_as_v0(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    meta["presets"] = [
        {"name": "Plain", "params": {"width": 5}},
        {"name": "Designed", "inputs": {"params": {"width": 6}, "ui": {"tab": "b"}, "v": 2}},
    ]
    paths.model_meta(model).write_text(json.dumps(meta), encoding="utf-8")
    listed = client.get(f"/api/v1/models/{model}/presets").json()
    by_name = {p["name"]: p for p in listed}
    assert by_name["Plain"]["inputs"] == {"params": {"width": 5}, "v": 0}
    assert by_name["Designed"]["inputs"] == {"params": {"width": 6}, "ui": {"tab": "b"}, "v": 2}
    assert by_name["Designed"]["params"] == {"width": 6}
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api/test_presets.py -q -k inputs`
Expected: FAIL. `KeyError: 'inputs'`, and the clash body is accepted with 201.

- [ ] **Step 3: The migration**

Create the file named by `echo "backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_saved_presets_inputs.sql"`:

```sql
-- Presets store template inputs (spec 2026-09-27 §4.3, §10). `params` stays and is
-- still written as the inputs' `params`, so a rollback reads every preset.
ALTER TABLE saved_presets ADD COLUMN inputs jsonb NOT NULL DEFAULT '{}'::jsonb;
UPDATE saved_presets SET inputs = jsonb_build_object('params', params, 'v', 0);
```

- [ ] **Step 4: The models**

In `backend/scadbuddy/library/presets.py`, add `from typing import Any`, `from pydantic import model_validator` and `from scadbuddy.render.inputs import InputsError, legacy_inputs, normalize_inputs`. Then:

```python
class _PresetBody(BaseModel):
    """What a preset is, wherever it is written: in a template's ``presets.json`` or
    in the body of a save."""

    name: str = Field(min_length=1, max_length=MAX_PRESET_NAME)
    params: dict[str, ParamValue] = Field(default_factory=dict)
    #: Template inputs (spec 2026-09-27 §4.3). Given, they win and ``params`` is read
    #: from them; left out, ``params`` is read as ``{"params": …, "v": 0}``.
    inputs: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, name: str) -> str:
        return _clean_name(name)

    @model_validator(mode="after")
    def _one_state(self) -> _PresetBody:
        try:
            self.inputs = normalize_inputs(self.inputs, self.params)
        except InputsError as error:
            raise ValueError(str(error)) from None
        self.params = self.inputs["params"]
        return self
```

```python
class ParamPresetUpdate(BaseModel):
    """A rename, a new set of values, or both. ``params`` or ``inputs`` replaces the
    old ones whole; ``params`` alone keeps the preset's other inputs keys."""

    name: str | None = Field(default=None, min_length=1, max_length=MAX_PRESET_NAME)
    params: dict[str, ParamValue] | None = None
    inputs: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, name: str | None) -> str | None:
        return None if name is None else _clean_name(name)

    @model_validator(mode="after")
    def _one_state(self) -> ParamPresetUpdate:
        if self.inputs is not None:
            try:
                self.inputs = normalize_inputs(self.inputs, self.params)
            except InputsError as error:
                raise ValueError(str(error)) from None
            self.params = self.inputs["params"]
        return self
```

In `ParamPreset`, after `params`:

```python
    #: The preset's template inputs (spec §4.3); ``params`` is their ``params``.
    inputs: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **Step 5: The store**

In `template_presets`, build each view with `inputs=preset.inputs or legacy_inputs(preset.params),` beside `params=preset.params,`. After `_PresetBody` validation `inputs` is always set, and the `or` exists only for mypy's `None`.

`_view`:

```python
    @staticmethod
    def _view(row: DictRow) -> ParamPreset:
        return ParamPreset(
            id=row["id"],
            name=row["name"],
            params=row["params"],
            inputs=row["inputs"] or legacy_inputs(row["params"]),
            origin="mine",
            updated_at=row["updated_at"],
        )
```

`create`: the INSERT becomes

```python
                "INSERT INTO saved_presets"
                " (model_id, id, name, params, inputs, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (model_id, uuid.uuid4().hex, body.name, Jsonb(body.params),
                 Jsonb(body.inputs), now, now),
```

(keep the statement's existing tail, whether that is `RETURNING *` or a following `SELECT`, as it is today).

`update`: before the UPDATE, compute

```python
            current_inputs = current["inputs"] or legacy_inputs(current["params"])
            if patch.inputs is not None:
                inputs = patch.inputs
            elif patch.params is not None:
                inputs = {**current_inputs, "params": patch.params}
            else:
                inputs = current_inputs
```

and change the statement to `"UPDATE saved_presets SET name = %s, params = %s, inputs = %s, updated_at = %s"` (same WHERE as today), passing `Jsonb(inputs["params"]), Jsonb(inputs)` in place of the single params argument.

`copy`: add `inputs` to the column list and `Jsonb(row["inputs"]),` after `Jsonb(row["params"]),`.

In `backend/scadbuddy/api/presets.py`, the duplicate route builds `ParamPresetCreate(name=body.name, params=source.params)`. Change it to `ParamPresetCreate(name=body.name, inputs=source.inputs or None)` so the copy keeps the UI state. `_require_valid` still receives `body.params`, which the validator derived from inputs.

- [ ] **Step 6: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api/test_presets.py tests/test_bundled_presets.py tests/test_pg_migrations.py -q`
Expected: PASS

- [ ] **Step 7: Lint, types, full suite, regenerate**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest -q`, then `cd frontend && pnpm gen:api`, `cd agent && pnpm gen:api && pnpm typecheck`
Expected: green; nothing generated is committed.

- [ ] **Step 8: Commit**

```bash
git add backend/scadbuddy/migrations/*_saved_presets_inputs.sql backend/scadbuddy/library/presets.py backend/scadbuddy/api/presets.py backend/tests/api/test_presets.py
git commit -m "feat(presets): presets store template inputs; params-only ones read as v0 (#425)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Outputs record inputs

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py` (`INPUTS_NAME`, `OutputStore.create`, `OutputStore.inputs`)
- Modify: `backend/scadbuddy/api/outputs.py` (`CreateOutputRequest`, `OutputDetail`, `EditTarget`, `_detail`, the create route, `get_edit_target`)
- Test: `backend/tests/api/test_outputs.py`

**Interfaces:**
- Consumes: `normalize_inputs`, `legacy_inputs`, `InputsError` (Task 1); `Job.inputs`.
- Produces:
  ```python
  INPUTS_NAME = "inputs.json"
  def OutputStore.create(self, job, *, name=None, public_url=None, inputs: Mapping[str, Any] | None = None) -> OutputMeta
  def OutputStore.inputs(self, output_id: str) -> dict[str, Any]     # inputs.json, else legacy_inputs(params.json)
  class CreateOutputRequest: job_id; name; + inputs: dict[str, Any] | None = None
  class OutputDetail: + inputs: dict[str, Any] = Field(default_factory=dict)
  class EditTarget: + inputs: dict[str, Any] = Field(default_factory=dict)
  ```

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/api/test_outputs.py`. This helper waits for the job without relying on any other test's helper:

```python
import time

from scadbuddy.core.paths import DataPaths


def _rendered(client: TestClient, model: str, body: dict[str, object]) -> str:
    accepted = client.post(f"/api/v1/models/{model}/render", json=body)
    assert accepted.status_code == 202, accepted.text
    url = accepted.json()["status_url"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(url).json()
        if job["status"] in {"done", "failed"}:
            assert job["status"] == "done", job
            return str(job["id"])
        time.sleep(0.02)
    raise AssertionError("the render did not finish")


def test_an_output_records_the_inputs_it_was_saved_with(client: TestClient, model: str) -> None:
    job_id = _rendered(client, model, {"inputs": {"params": {"width": 12}, "ui": {"tab": "a"}}})
    sent = {"params": {"width": 12}, "ui": {"tab": "b"}}
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id, "inputs": sent}
    )
    assert created.status_code == 201, created.text
    output = created.json()
    assert output["inputs"] == {**sent, "v": 0}
    assert client.get(f"/api/v1/outputs/{output['id']}").json()["inputs"] == output["inputs"]
    assert client.get(f"/api/v1/outputs/{output['id']}/edit").json()["inputs"] == output["inputs"]


def test_an_output_refuses_inputs_the_job_did_not_render(client: TestClient, model: str) -> None:
    job_id = _rendered(client, model, {"params": {"width": 12}})
    refused = client.post(
        f"/api/v1/models/{model}/outputs",
        json={"job_id": job_id, "inputs": {"params": {"width": 13}}},
    )
    assert refused.status_code == 422


def test_an_output_saved_without_inputs_records_the_jobs(client: TestClient, model: str) -> None:
    job_id = _rendered(client, model, {"inputs": {"params": {"width": 12}, "ui": {"tab": "a"}}})
    output = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id}).json()
    assert output["inputs"] == {"params": {"width": 12}, "ui": {"tab": "a"}, "v": 0}


def test_an_output_from_before_inputs_reads_as_params_v0(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    job_id = _rendered(client, model, {"params": {"width": 12}})
    output = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id}).json()
    (paths.output_dir(model, output["id"]) / "inputs.json").unlink()
    assert client.get(f"/api/v1/outputs/{output['id']}").json()["inputs"] == {
        "params": {"width": 12},
        "v": 0,
    }
    assert client.get(f"/api/v1/outputs/{output['id']}/edit").json()["inputs"] == {
        "params": {"width": 12},
        "v": 0,
    }
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api/test_outputs.py -q -k inputs`
Expected: FAIL, `KeyError: 'inputs'`

- [ ] **Step 3: The store**

In `backend/scadbuddy/library/outputs.py`, add `from collections.abc import Mapping`, `from typing import Any`, `from scadbuddy.render.inputs import legacy_inputs, normalize_inputs`, and `INPUTS_NAME = "inputs.json"` beside `PARAMS_NAME`.

`create` gains `inputs: Mapping[str, Any] | None = None` after `public_url`, and right after it writes `params.json`:

```python
        recorded = normalize_inputs(
            inputs if inputs is not None else (job.inputs or None), job.params
        )
        (directory / INPUTS_NAME).write_text(
            json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
```

(`normalize_inputs` with a job's own inputs never disagrees with `job.params`, because the route checked that when the job was submitted.)

After `params`:

```python
    def inputs(self, output_id: str) -> dict[str, Any]:
        path = self._find_dir(output_id) / INPUTS_NAME
        if not path.is_file():
            return legacy_inputs(self.params(output_id))
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded
```

- [ ] **Step 4: The routes**

In `backend/scadbuddy/api/outputs.py`:

```python
class OutputDetail(OutputSummary):
    params: dict[str, ParamValue] = Field(default_factory=dict)
    #: The template inputs this output was saved with (spec §4.3).
    inputs: dict[str, Any] = Field(default_factory=dict)


class CreateOutputRequest(BaseModel):
    job_id: str
    name: str | None = None
    #: The inputs on screen when Generate was pressed (spec §4.3). Their `params`
    #: must be the ones the job rendered; left out, the job's own inputs are recorded.
    inputs: dict[str, Any] | None = None
```

`EditTarget` gains `inputs: dict[str, Any] = Field(default_factory=dict)` after `params`. `_detail` gains `inputs=store.inputs(meta.id),` after `params=`. In `get_edit_target`, the 3MF branch passes `inputs=legacy_inputs(stamped.params),` and the record branch passes `inputs=outputs.inputs(output_id),`.

In the `POST /models/{slug}/outputs` route, after the route has looked up the finished `job` and before it calls `create(...)`:

```python
    inputs = None
    if body.inputs is not None:
        try:
            inputs = normalize_inputs(body.inputs, None)
        except InputsError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        if inputs["params"] != job.params:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"inputs.params are not the parameters job {job.id} rendered",
            )
```

Then pass `inputs=inputs` to the store's `create(...)` call. Imports: `from typing import Any` and `from scadbuddy.render.inputs import InputsError, legacy_inputs, normalize_inputs`.

- [ ] **Step 5: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api/test_outputs.py tests/test_provenance.py -q`
Expected: PASS

- [ ] **Step 6: Lint, types, full suite, regenerate**

Same commands as Task 2 Step 7. Expected: green.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/library/outputs.py backend/scadbuddy/api/outputs.py backend/tests/api/test_outputs.py
git commit -m "feat(outputs): outputs record template inputs; reopening returns them (#425)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The `ui` declaration, served modules, and the page CSP

**Files:**
- Modify: `backend/scadbuddy/library/catalogue.py` (`UiDeclaration`, `UI_MODULE_PATTERN`, `ModelMeta.ui`, `ModelMeta.ui_error`, `ModelRecord.ui_error`, `Catalogue.record`)
- Create: `backend/scadbuddy/api/template_ui.py` (picked up automatically: `main.py` `_api_router()` mounts every `scadbuddy.api` module's `router`, sorted by name; do not include it by hand, or `tests/api/test_routes.py` finds the routes twice)
- Modify: `backend/scadbuddy/api/static.py` (`PAGE_CSP`)
- Create: `frontend/page-csp.txt`
- Modify: `agent/src/tools/coverage.ts` (two `NOT_A_TOOL` entries)
- Test: `backend/tests/api/test_template_ui.py` (new), `backend/tests/test_bundled_ui.py` (new)

**Interfaces:**
- Produces:
  ```python
  UI_MODULE_PATTERN: str
  class UiDeclaration(BaseModel): module: str; slot: Literal["panel", "page"] = "panel"; api: int (>= 1)
  ModelMeta.ui: UiDeclaration | None; ModelMeta.ui_error: str | None (excluded from dumps)
  ModelRecord.ui_error: str | None    # on the wire
  GET /api/v1/models/{slug}/ui/{path}                       -> live file, Cache-Control: no-cache, ETag
  GET /api/v1/models/{slug}/versions/{commit}/ui/{path}     -> that revision's file; immutable when served from an export, no-cache when {commit} is the live revision
  UI_FILE_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox", "Cross-Origin-Resource-Policy": "same-origin"}
  PAGE_CSP: str   # static.py; frontend/page-csp.txt holds the same line
  ```
  `path` is relative to the template's `ui/` directory. `model.json`'s `module` is `ui/<path>`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/api/test_template_ui.py`:

```python
"""Template UI modules (spec 2026-09-27 §4.1, §9)."""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths

UI = {"module": "ui/index.js", "slot": "panel", "api": 1}


def _with_ui(paths: DataPaths, slug: str, files: dict[str, bytes], ui: object = UI) -> None:
    directory = paths.model_dir(slug)
    for name, body in files.items():
        target = directory / "ui" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    meta = json.loads(paths.model_meta(slug).read_text(encoding="utf-8"))
    meta["ui"] = ui
    paths.model_meta(slug).write_text(json.dumps(meta), encoding="utf-8")


def test_the_record_carries_the_declaration(client: TestClient, model: str, paths: DataPaths) -> None:
    _with_ui(paths, model, {"index.js": b"export function mount() {}\n"})
    record = client.get(f"/api/v1/models/{model}").json()
    assert record["ui"] == UI
    assert record["ui_error"] is None


def test_a_module_is_served_as_javascript(client: TestClient, model: str, paths: DataPaths) -> None:
    _with_ui(paths, model, {"index.js": b"export function mount() {}\n", "lib/a.js": b"export {}\n"})
    response = client.get(f"/api/v1/models/{model}/ui/index.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == "export function mount() {}\n"
    assert client.get(f"/api/v1/models/{model}/ui/lib/a.js").status_code == 200
    again = client.get(
        f"/api/v1/models/{model}/ui/index.js", headers={"If-None-Match": response.headers["etag"]}
    )
    assert again.status_code == 304


# httpx normalises `../model.scad` before sending (it arrives as `/models/demo/model.scad`, a
# 404 of its own); the encoded forms and the symlink are what reach `_ui_file`.
@pytest.mark.parametrize(
    "path", ["../model.scad", "%2e%2e/model.scad", "..%2fmodel.scad", "page.html", "missing.js", ".hidden.js"]
)
def test_ui_paths_never_leave_ui(
    client: TestClient, model: str, paths: DataPaths, path: str
) -> None:
    _with_ui(
        paths,
        model,
        {"index.js": b"export function mount() {}\n", "page.html": b"<script>alert(1)</script>"},
    )
    os.symlink(paths.model_source(model), paths.model_dir(model) / "ui" / "link.js")
    response = client.get(f"/api/v1/models/{model}/ui/{path}")
    assert response.status_code == 404
    assert b"width = 10" not in response.content
    assert client.get(f"/api/v1/models/{model}/ui/link.js").status_code == 404


def test_a_model_without_ui_has_no_module(client: TestClient, model: str) -> None:
    assert client.get(f"/api/v1/models/{model}").json()["ui"] is None
    assert client.get(f"/api/v1/models/{model}/ui/index.js").status_code == 404


def test_a_malformed_ui_costs_only_the_ui(client: TestClient, model: str, paths: DataPaths) -> None:
    _with_ui(paths, model, {}, ui={"module": "../model.scad", "slot": "sidebar", "api": "one"})
    record = client.get(f"/api/v1/models/{model}").json()
    assert record["ui"] is None
    assert "ui" in record["ui_error"] and "slot" in record["ui_error"]
    assert any(m["slug"] == model for m in client.get("/api/v1/models").json())
    assert client.get(f"/api/v1/models/{model}/schema").status_code == 200


def test_a_patch_keeps_ui(client: TestClient, model: str, paths: DataPaths) -> None:
    _with_ui(paths, model, {"index.js": b"export function mount() {}\n"})
    assert client.patch(f"/api/v1/models/{model}", json={"name": "Renamed"}).status_code == 200
    assert json.loads(paths.model_meta(model).read_text(encoding="utf-8"))["ui"] == UI
    assert "ui_error" not in json.loads(paths.model_meta(model).read_text(encoding="utf-8"))


def _commit(paths: DataPaths, message: str) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(paths.models), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("add", "-A")
    git("commit", "-qm", message)
    return git("rev-parse", "HEAD")


@pytest.mark.requires_git
def test_a_pinned_revision_serves_its_own_module_graph(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    _with_ui(paths, model, {"index.js": b"import './a.js'\n", "a.js": b"// one\n"})
    first = _commit(paths, "ui one")
    _with_ui(paths, model, {"a.js": b"// two\n"})
    _commit(paths, "ui two")
    pinned = client.get(f"/api/v1/models/{model}/versions/{first}/ui/a.js")
    assert pinned.status_code == 200
    assert pinned.text == "// one\n"
    assert pinned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get(f"/api/v1/models/{model}/ui/a.js").text == "// two\n"


def test_the_page_carries_the_csp() -> None:
    from pathlib import Path

    from scadbuddy.api.static import PAGE_CSP

    assert "script-src 'self';" in PAGE_CSP
    assert "connect-src 'self';" in PAGE_CSP
    assert "https://fonts.googleapis.com" in PAGE_CSP  # the font picker's previews
    assert "https://fonts.gstatic.com" in PAGE_CSP
    shared = Path(__file__).parents[3] / "frontend" / "page-csp.txt"
    if shared.is_file():  # absent in the image's test stage, which has no frontend tree
        assert shared.read_text(encoding="utf-8").strip() == PAGE_CSP


def test_the_spa_sends_the_csp_on_the_document_and_client_routes(tmp_path: object) -> None:
    from pathlib import Path

    from starlette.applications import Starlette
    from starlette.routing import Mount

    from scadbuddy.api.static import PAGE_CSP, SPAStaticFiles

    bundle = Path(str(tmp_path))
    (bundle / "index.html").write_text("<!doctype html>", encoding="utf-8")
    app = Starlette(routes=[Mount("/", SPAStaticFiles(bundle))])
    with TestClient(app) as spa:
        for url in ("/", "/m/demo"):
            response = spa.get(url)
            assert response.status_code == 200
            assert response.headers["content-security-policy"] == PAGE_CSP
```

Create `backend/tests/test_bundled_ui.py`:

```python
"""Every bundled template's `ui` names a module it ships (spec 2026-09-27 §4.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scadbuddy.library.catalogue import UiDeclaration

MODELS = Path(__file__).parents[2] / "models"
DECLARED = [
    path
    for path in sorted(MODELS.glob("*/model.json"))
    if "ui" in json.loads(path.read_text(encoding="utf-8"))
]


@pytest.mark.parametrize("meta", DECLARED, ids=lambda path: path.parent.name)
def test_a_bundled_ui_is_valid_and_present(meta: Path) -> None:
    ui = UiDeclaration.model_validate(json.loads(meta.read_text(encoding="utf-8"))["ui"])
    assert ui.api == 1
    assert (meta.parent / ui.module).is_file()
```

(With no bundled `ui` yet, the parametrized test collects zero cases. Tasks 9 and 10 add cases.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api/test_template_ui.py tests/test_bundled_ui.py -q`
Expected: FAIL. `ImportError: cannot import name 'UiDeclaration'`, and `record["ui"]` is a `KeyError`.

- [ ] **Step 3: The declaration**

In `backend/scadbuddy/library/catalogue.py` (imports: `Literal` from typing if it is not already there, `ConfigDict`, `model_validator` from pydantic), add above `ModelMeta`:

```python
#: `ui/` plus a relative path whose segments never start with a dot, ending `.js`
#: or `.mjs`: no `..`, no hidden file, nothing outside the template's `ui/`.
UI_MODULE_PATTERN = r"^ui/(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)*[A-Za-z0-9_-][A-Za-z0-9._-]*\.m?js$"


class UiDeclaration(BaseModel):
    """``model.json``'s ``ui`` (spec 2026-09-27 §4.1): the template's own interface."""

    module: str = Field(pattern=UI_MODULE_PATTERN, max_length=300)
    slot: Literal["panel", "page"] = "panel"
    #: The host-API major the UI was written against (§4.3, §8.1). Any positive
    #: major is a valid declaration; the page decides whether it can mount it.
    api: int = Field(ge=1)
```

In `ModelMeta`, after `media`:

```python
    #: The template's own UI (#425), or None for the generated form.
    ui: UiDeclaration | None = None
    #: Why a ``ui`` on disk could not be read. The template still lists and
    #: customizes with the generated form (§4.2); never written back to model.json.
    ui_error: str | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _readable_ui(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("ui") is None:
            return data
        try:
            UiDeclaration.model_validate(data["ui"])
        except ValidationError as error:
            problems = "; ".join(
                f"ui.{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}"
                for detail in error.errors()
            )
            return {**data, "ui": None, "ui_error": f"model.json's ui is not valid: {problems}"}
        return data
```

In `ModelRecord`, add `ui_error: str | None = None` (declared again without `exclude`, so it is on the wire). In `Catalogue.record`, the `ModelRecord(...)` call spreads `meta.model_dump(exclude={"media"})`, which leaves `ui_error` out, so pass it explicitly: add `ui_error=meta.ui_error,` after `media=media,`.

- [ ] **Step 4: The routes**

Create `backend/scadbuddy/api/template_ui.py`:

```python
"""A template's own UI files (spec 2026-09-27 §4.1, §9): served from its `ui/`, live or
at a pinned revision, so a module's relative imports stay inside one revision."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, Header, Path, status
from fastapi.responses import Response

from scadbuddy.api.deps import (
    CatalogueDep,
    CommitPath,
    FetcherDep,
    HistoryDep,
    PathsDep,
    SlugPath,
)
from scadbuddy.api.jobs import _resolve_version
from scadbuddy.api.models import _etag_matches, require_model_exists
from scadbuddy.core.problems import ApiError
from scadbuddy.render.jobs import resolve_source

router = APIRouter(tags=["models"])

UI_DIR = "ui"
#: What a template UI may ship. Anything else, HTML included, is not served: a
#: document from the app's origin would run with the whole app's reach.
UI_MEDIA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
}
#: `sandbox` makes a file opened directly (an SVG, say) an inert document; it does
#: not apply to the module the page imports, which runs under the page's own CSP.
UI_FILE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cross-Origin-Resource-Policy": "same-origin",
}
LIVE_CACHE_CONTROL = "no-cache"
#: A commit's files never change, and the URL names the commit.
PINNED_CACHE_CONTROL = "public, max-age=31536000, immutable"

#: Not a pattern: a path that fails one would be a 422, and every path that is not a
#: servable file under ui/ is the same 404, whatever shape it has.
UiPath = Annotated[
    str, Path(max_length=300, description="A file under the template's ui/ directory")
]


def _ui_file(directory: FsPath, path: str) -> FsPath:
    base = (directory / UI_DIR).resolve()
    target = (base / path).resolve()
    if (
        any(not part or part.startswith(".") for part in path.split("/"))
        # Resolved, so a symlink out of ui/ is caught as well as a `..`.
        or not target.is_relative_to(base)
        or target.suffix.lower() not in UI_MEDIA_TYPES
        or not target.is_file()
    ):
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no ui file {path!r}")
    return target


def _serve(file: FsPath, *, pinned: bool, if_none_match: str | None) -> Response:
    body = file.read_bytes()
    etag = f'"{hashlib.sha256(body).hexdigest()}"'
    headers = {
        **UI_FILE_HEADERS,
        "ETag": etag,
        "Cache-Control": PINNED_CACHE_CONTROL if pinned else LIVE_CACHE_CONTROL,
    }
    if _etag_matches(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(body, media_type=UI_MEDIA_TYPES[file.suffix.lower()], headers=headers)


@router.get(
    "/models/{slug}/ui/{path:path}",
    response_class=Response,
    responses={200: {"content": {"text/javascript": {}}}, 304: {"description": "Unchanged"}},
    summary="Template UI file",
)
async def get_ui_file(
    slug: SlugPath,
    path: UiPath,
    catalogue: CatalogueDep,
    paths: PathsDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    require_model_exists(catalogue, slug)
    # The live directory itself: no revision to resolve, no library checkout to fetch.
    file = await asyncio.to_thread(_ui_file, paths.model_dir(slug), path)
    return await asyncio.to_thread(_serve, file, pinned=False, if_none_match=if_none_match)


@router.get(
    "/models/{slug}/versions/{commit}/ui/{path:path}",
    response_class=Response,
    responses={200: {"content": {"text/javascript": {}}}, 304: {"description": "Unchanged"}},
    summary="Template UI file at a revision",
)
async def get_ui_file_at(
    slug: SlugPath,
    commit: CommitPath,
    path: UiPath,
    catalogue: CatalogueDep,
    history: HistoryDep,
    paths: PathsDep,
    fetcher: FetcherDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    require_model_exists(catalogue, slug)
    requested = await _resolve_version(history, slug, commit)
    source = await resolve_source(slug, requested, paths=paths, history=history, fetcher=fetcher)
    file = await asyncio.to_thread(_ui_file, source.scad.parent, path)
    # `resolve_source` answers the current revision with the live directory, which an
    # uncommitted edit can change; only an export is immutable.
    exported = source.scad.parent != paths.model_dir(slug)
    return await asyncio.to_thread(_serve, file, pinned=exported, if_none_match=if_none_match)
```

`CommitPath` is defined in `api/deps.py` (mypy strict rejects taking it through `api/versions.py`'s import). No `main.py` change: `_api_router()` discovers the module.

- [ ] **Step 5: The page CSP**

In `backend/scadbuddy/api/static.py`:

```python
#: The app document's policy (spec 2026-09-27 §9). A template UI runs unsandboxed in
#: this page. The policy stops it loading script from anywhere but this origin, and stops
#: fetch/XHR/WebSocket and image, media and font beacons to other hosts. Google Fonts is
#: the one exception, for style and font files only (the font picker's previews,
#: `frontend/src/lib/fonts.ts` `googleFontsCssUrl`). The policy does NOT stop same-origin
#: abuse: a module can call every /api/v1 route with the user's session. Nor does it stop
#: exfiltration by top-level navigation, window.open or WebRTC. No frame-ancestors:
#: Bambuddy's origin, which frames this page, is not known here.
PAGE_CSP = (
    "default-src 'self'; script-src 'self'; connect-src 'self'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'"
)
```

At the end of `SPAStaticFiles.get_response`, stamp every response it returns. Assign the response to `response` on each return path, then:

```python
        response.headers["Content-Security-Policy"] = PAGE_CSP
        return response
```

Create `frontend/page-csp.txt` holding exactly the same policy on one line (the concatenated string above, no trailing spaces).

- [ ] **Step 6: Agent coverage entries**

In `agent/src/tools/coverage.ts`, add to `NOT_A_TOOL`:

```ts
  {
    operation: 'GET /api/v1/models/{slug}/ui/{path}',
    reason:
      "Serves a template's own UI module and assets to the browser (#425). An agent reads the `ui` " +
      'declaration from get_model and has no page to mount a module in.',
  },
  {
    operation: 'GET /api/v1/models/{slug}/versions/{commit}/ui/{path}',
    reason: 'The same UI files at a pinned revision, for the browser (#425).',
  },
```

- [ ] **Step 7: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api/test_template_ui.py tests/test_bundled_ui.py tests/api/test_models.py tests/api/test_routes.py tests/api/test_openapi.py -q`
Expected: PASS
Run: `cd agent && pnpm gen:api && pnpm test -- coverage`
Expected: PASS

- [ ] **Step 8: Lint, types, full suite**

Same commands as Task 2 Step 7, plus `cd agent && pnpm lint && pnpm typecheck`. Expected: green.

- [ ] **Step 9: Commit**

```bash
git add backend/scadbuddy/library/catalogue.py backend/scadbuddy/api/template_ui.py backend/scadbuddy/api/static.py backend/tests/api/test_template_ui.py backend/tests/test_bundled_ui.py frontend/page-csp.txt agent/src/tools/coverage.ts
git commit -m "feat(models): model.json ui declaration, served ui/ files, page CSP (#425)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Frontend inputs plumbing

The page keeps `values` (params) exactly as today and gains `extra`, which holds every other inputs key. A template without a UI always has `extra = {}`, so its form, requests and presets behave as they do today, except that request bodies say `inputs`.

**Files:**
- Create: `frontend/src/lib/inputs.ts`, `frontend/src/lib/inputs.test.ts`, `frontend/src/lib/saveOutput.ts`
- Modify: `frontend/src/api/client.ts` (`render`, `createOutput`, `uiFileUrl`)
- Modify: `frontend/src/lib/useRenderJob.ts`, `frontend/src/lib/presets.ts`, `frontend/src/components/PresetPicker.tsx`, `frontend/src/components/ActionBar.tsx`, `frontend/src/pages/CustomizePage.tsx`
- Modify: `frontend/src/mocks/handlers.ts` (render, presets, outputs, edit target read and store inputs)
- Test: `frontend/src/lib/useRenderJob.test.ts`, `frontend/src/components/PresetPicker.test.tsx`, `frontend/src/pages/CustomizePage.test.tsx`

**Interfaces:**
- Consumes: the Task 1–3 API (`RenderRequest.inputs`, `ParamPreset.inputs`, `Output.inputs`, `EditTarget.inputs`, `CreateOutputRequest.inputs`).
- Produces:
  ```ts
  // lib/inputs.ts
  export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
  export type JsonObject = { [key: string]: Json }
  export type InputsExtra = JsonObject                  // every inputs key but `params`
  export const NO_EXTRA: InputsExtra
  export function isJsonObject(value: unknown): value is JsonObject
  export function splitInputs(raw: Record<string, unknown> | null | undefined, fallback?: ParamValues): { params: ParamValues; extra: InputsExtra }
  export function joinInputs(params: ParamValues, extra: InputsExtra): JsonObject
  export function mergePatch(target: Json | undefined, patch: Json): Json      // RFC 7386
  export function getPath(root: Json, path: string): Json | undefined          // dotted
  export function setPath(root: JsonObject, path: string, value: Json): JsonObject
  // api/client.ts
  api.render(slug: string, inputs: JsonObject, version?: string, supersedes?: string)
  api.createOutput(slug: string, jobId: string, name?: string, inputs?: JsonObject)
  // phase 4 (#427, Task 8) appends `index?: number` (a pipeline job's Nth output), sent only when defined
  api.uiFileUrl(slug: string, version: string | undefined, path: string): string
  // lib/useRenderJob.ts
  useRenderJob(slug, params, version, extra: InputsExtra = NO_EXTRA): RenderState
  // lib/presets.ts
  applyPreset(schema, preset): { values: ParamValues; skipped: string[]; extra: InputsExtra }
  presetInputs(schema, values, extra): JsonObject
  // lib/saveOutput.ts
  saveOutput(args: { slug: string; job: Job; extra: InputsExtra; capture: () => Promise<Blob | null> }): Promise<Output>
  // PresetPicker props: + extra: InputsExtra; onApply(values: ParamValues, extra: InputsExtra)
  // ActionBar props: + extra: InputsExtra
  ```

- [ ] **Step 1: Write the failing unit tests for `lib/inputs.ts`**

```ts
import { describe, expect, it } from 'vitest'
import { getPath, joinInputs, mergePatch, NO_EXTRA, setPath, splitInputs } from './inputs'

describe('splitInputs / joinInputs', () => {
  it('splits params from the UI state and joins them back', () => {
    const raw = { params: { width: 3 }, house: { storeys: 2 }, v: 0 }
    const { params, extra } = splitInputs(raw)
    expect(params).toEqual({ width: 3 })
    expect(extra).toEqual({ house: { storeys: 2 }, v: 0 })
    expect(joinInputs(params, extra)).toEqual(raw)
  })
  it('falls back to the given params when inputs are missing or malformed', () => {
    expect(splitInputs(undefined, { width: 1 })).toEqual({ params: { width: 1 }, extra: NO_EXTRA })
    expect(splitInputs({ params: 'x' }, { width: 1 }).params).toEqual({ width: 1 })
  })
})

describe('mergePatch', () => {
  it('merges objects, replaces the rest, deletes on null', () => {
    const target = { params: { width: 1, height: 2 }, tab: 'a', list: [1] }
    expect(mergePatch(target, { params: { width: 5 }, tab: null, list: [2] })).toEqual({
      params: { width: 5, height: 2 },
      list: [2],
    })
  })
})

describe('getPath / setPath', () => {
  it('reads and writes a dotted path without touching the original', () => {
    const root = { params: { width: 1 }, style: {} }
    const next = setPath(root, 'style.exterior', 'brick')
    expect(getPath(next, 'style.exterior')).toBe('brick')
    expect(getPath(root, 'style.exterior')).toBeUndefined()
    expect(getPath(next, 'params.width')).toBe(1)
    expect(setPath({}, 'a.b.c', 1)).toEqual({ a: { b: { c: 1 } } })
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/lib/inputs.test.ts`
Expected: FAIL, "Failed to resolve import './inputs'"

- [ ] **Step 3: Implement `lib/inputs.ts`**

```ts
import type { ParamValue } from '../api/types'
import type { ParamValues } from './params'

/** Template inputs (spec 2026-09-27 §4.3): JSON the template owns. */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
export type JsonObject = { [key: string]: Json }
/** Every inputs key except `params`: a template UI's own state, never rendered. */
export type InputsExtra = JsonObject

export const NO_EXTRA: InputsExtra = Object.freeze({}) as InputsExtra

export function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isParamValue(value: unknown): value is ParamValue {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
}

export function splitInputs(
  raw: Record<string, unknown> | null | undefined,
  fallback: ParamValues = {},
): { params: ParamValues; extra: InputsExtra } {
  if (!raw) return { params: fallback, extra: NO_EXTRA }
  const { params, ...rest } = raw
  const valid =
    isJsonObject(params) && Object.values(params).every(isParamValue)
      ? (params as ParamValues)
      : fallback
  return { params: valid, extra: Object.keys(rest).length ? (rest as InputsExtra) : NO_EXTRA }
}

export function joinInputs(params: ParamValues, extra: InputsExtra): JsonObject {
  return { ...extra, params }
}

export function mergePatch(target: Json | undefined, patch: Json): Json {
  if (!isJsonObject(patch)) return patch
  const result: JsonObject = isJsonObject(target) ? { ...target } : {}
  for (const [key, value] of Object.entries(patch)) {
    if (value === null) delete result[key]
    else result[key] = mergePatch(result[key], value)
  }
  return result
}

export function getPath(root: Json, path: string): Json | undefined {
  let node: Json | undefined = root
  for (const key of path.split('.')) {
    if (!isJsonObject(node)) return undefined
    node = node[key]
  }
  return node
}

export function setPath(root: JsonObject, path: string, value: Json): JsonObject {
  const [head = '', ...rest] = path.split('.')
  if (rest.length === 0) return { ...root, [head]: value }
  const child = root[head]
  return { ...root, [head]: setPath(isJsonObject(child) ? child : {}, rest.join('.'), value) }
}
```

- [ ] **Step 4: Run it**

Run: `cd frontend && pnpm exec vitest run src/lib/inputs.test.ts`
Expected: PASS

- [ ] **Step 5: Write the failing integration tests**

In `frontend/src/lib/useRenderJob.test.ts`, add a case that captures the render body with `server.use` (the file already renders the hook with `renderHook` and imports `server`, `http`, `HttpResponse`; follow its existing setup for slug and schema):

```ts
it('submits inputs: the params plus the UI state', async () => {
  let body: unknown
  server.use(
    http.post('/api/v1/models/:slug/render', async ({ request }) => {
      body = await request.json()
      return HttpResponse.json({ job_id: 'a'.repeat(32), status_url: '/api/v1/jobs/' + 'a'.repeat(32) }, { status: 202 })
    }),
  )
  renderHook(() => useRenderJob('name-keychain', { name: 'Hi' }, undefined, { tab: 'lid' }))
  await waitFor(() => expect(body).toEqual({ inputs: { params: { name: 'Hi' }, tab: 'lid' }, version: null }))
})
```

In `frontend/src/pages/CustomizePage.test.tsx`, using its `render(route, state)` helper (line 64, which calls `renderPage(<CustomizePage />, { route, path: '/m/:slug', state })` and returns `user`):

```ts
it('reopens an output with its UI state and saves it again with the output', async () => {
  const outputId = 'c'.repeat(32)
  server.use(
    http.get(`/api/v1/outputs/${outputId}/edit`, () =>
      HttpResponse.json({
        output_id: outputId,
        slug: 'name-keychain',
        name: 'Tagged',
        params: { name: 'Kai' },
        inputs: { params: { name: 'Kai' }, tab: 'lid', v: 0 },
        model_version: null,
        source: 'record',
      }),
    ),
  )
  const bodies: unknown[] = []
  server.use(
    http.post('/api/v1/models/:slug/outputs', async ({ request }) => {
      bodies.push(await request.json())
      return HttpResponse.json({ ...fixtures.outputs[0], id: 'd'.repeat(32) }, { status: 201 })
    }),
  )
  const { user } = render(`/m/name-keychain?from=${outputId}`)
  const generate = await screen.findByTestId('generate')
  await waitFor(() => expect(generate).toBeEnabled(), { timeout: 5000 })
  await user.click(generate)
  await waitFor(() =>
    expect(bodies[0]).toMatchObject({ inputs: { params: { name: 'Kai' }, tab: 'lid', v: 0 } }),
  )
})
```

(`fixtures` is `import * as fixtures from '../mocks/fixtures'`. Add that import if the file lacks it.)

In `frontend/src/components/PresetPicker.test.tsx`, add:

```ts
it('saves the UI state with the preset and hands it back on apply', async () => {
  let saved: unknown
  server.use(
    http.post('/api/v1/models/:slug/presets', async ({ request }) => {
      saved = await request.json()
      return HttpResponse.json(
        { id: 'e'.repeat(32), name: 'Lid', origin: 'mine', params: {}, inputs: { params: {}, tab: 'lid', v: 0 } },
        { status: 201 },
      )
    }),
  )
  const onApply = vi.fn()
  const { user } = renderPage(
    <PresetPicker slug="name-keychain" schema={keychainSchema} values={defaultValues(keychainSchema)} extra={{ tab: 'lid' }} onApply={onApply} />,
  )
  await savePresetNamed(user, 'Lid')
  await waitFor(() => expect(saved).toEqual({ name: 'Lid', inputs: { params: {}, tab: 'lid' } }))
})
```

`savePresetNamed` is the steps the file's existing "saves a preset" test performs: open the save control, type the name, confirm. Lift those lines into a helper in the same file and have that test call it too.

- [ ] **Step 6: Run to verify they fail**

Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/lib/useRenderJob.test.ts src/components/PresetPicker.test.tsx src/pages/CustomizePage.test.tsx`
Expected: FAIL. The body still has `params`, `extra` is not a prop, and the output body has no `inputs`.

- [ ] **Step 7: Client, hook, presets, Generate**

`frontend/src/api/client.ts`:

```ts
  render: (slug: string, inputs: JsonObject, version?: string, supersedes?: string) =>
    request<RenderAccepted>(`/models/${seg(slug)}/render`, {
      method: 'POST',
      body: JSON.stringify({ inputs, version: version ?? null, ...(supersedes ? { supersedes } : {}) }),
    }),
```

```ts
  createOutput: (slug: string, jobId: string, name?: string, inputs?: JsonObject) =>
    request<Output>(`/models/${seg(slug)}/outputs`, {
      method: 'POST',
      body: JSON.stringify({ job_id: jobId, name: name ?? null, ...(inputs ? { inputs } : {}) }),
    }),
```

```ts
  /** A file under a template's `ui/` (spec 2026-09-27 §4.1): pinned by revision when there is one. */
  uiFileUrl: (slug: string, version: string | undefined, path: string) =>
    `${API_BASE}/models/${seg(slug)}${version ? `/versions/${seg(version)}` : ''}/ui/${path
      .split('/')
      .map(seg)
      .join('/')}`,
```

`frontend/src/lib/useRenderJob.ts`: add the parameter `extra: InputsExtra = NO_EXTRA` after `version`, hold it in `const extraRef = useLatest(extra)` (from `./useLatest`), and change the submit line to `await api.render(slug, joinInputs(params, extraRef.current), version, supersedes)`. `extra` is deliberately not an effect dependency: UI state alone never re-renders. Run `grep -rn "api.render(" frontend/src` and pass `joinInputs(params, NO_EXTRA)` at any other call site.

`frontend/src/lib/presets.ts`:

```ts
export interface AppliedPreset {
  /** The whole set of values on screen once the preset is applied. */
  values: ParamValues
  /** The preset's parameters this template no longer has, which were left out. */
  skipped: string[]
  /** The preset's UI state (spec 2026-09-27 §4.3); empty for a params-only preset. */
  extra: InputsExtra
}

export function applyPreset(schema: CustomizerSchema, preset: ParamPreset): AppliedPreset {
  const known = new Set(allParams(schema).map((param) => param.name))
  const { params, extra } = splitInputs(preset.inputs, preset.params)
  const values: ParamValues = defaultValues(schema)
  const skipped: string[] = []
  for (const [name, value] of Object.entries(params)) {
    if (known.has(name)) values[name] = value
    else skipped.push(name)
  }
  return { values, skipped, extra }
}

/** What a preset saved from the page holds: the changed values, plus the UI state. */
export function presetInputs(schema: CustomizerSchema, values: ParamValues, extra: InputsExtra): JsonObject {
  return joinInputs(presetParams(schema, values), extra)
}
```

`frontend/src/components/PresetPicker.tsx`: `Props` gains `/** The UI state saved with a preset (spec §4.3). */ extra: InputsExtra` and `onApply: (values: ParamValues, extra: InputsExtra) => void`. Every create or update body that sends `params: presetParams(schema, values)` sends `inputs: presetInputs(schema, values, extra)` instead. Every `onApply(applied.values)` becomes `onApply(applied.values, applied.extra)`. Code that compares a preset with the values on screen keeps comparing `params`.

Create `frontend/src/lib/saveOutput.ts`:

```ts
import { api } from '../api/client'
import type { Job, Output } from '../api/types'
import { joinInputs, type InputsExtra } from './inputs'

/** Generate (spec §6): keep the render as an output, with the inputs on screen and the preview as its thumbnail. */
export async function saveOutput({
  slug,
  job,
  extra,
  capture,
}: {
  slug: string
  job: Job
  extra: InputsExtra
  capture: () => Promise<Blob | null>
}): Promise<Output> {
  const created = await api.createOutput(slug, job.id, undefined, joinInputs(job.params ?? {}, extra))
  const png = await capture()
  // A missing thumbnail is cosmetic: never fail the generate over it.
  if (png) await api.putThumbnail(created.id, png).catch(() => undefined)
  return created
}
```

`frontend/src/components/ActionBar.tsx`: `Props` gains `/** The UI state recorded with the output (spec §4.3). */ extra: InputsExtra`. In `generate`, replace the `createOutput`/`capture`/`putThumbnail` lines with `const created = await saveOutput({ slug, job, extra, capture })`.

- [ ] **Step 8: The page**

In `frontend/src/pages/CustomizePage.tsx`:

```ts
  const [edits, setEdits] = useState<{
    of: ParamValues | null
    values: ParamValues | null
    extra: InputsExtra | null
  }>({ of: null, values: null, extra: null })
```

The seed reads inputs:

```ts
  const reopenedInputs = useMemo(
    () => (reopened ? splitInputs(reopened.inputs, reopened.params) : null),
    [reopened],
  )
  const seed = useMemo(
    () =>
      schema && !resolving && !leaving
        ? reopenedInputs
          ? { ...defaultValues(schema), ...reopenedInputs.params }
          : defaultValues(schema)
        : null,
    [schema, reopenedInputs, resolving, leaving],
  )
  if (edits.of !== seed) setEdits({ of: seed, values: null, extra: null })
  const values = edits.values ?? seed ?? NOTHING
  const extra = edits.extra ?? reopenedInputs?.extra ?? NO_EXTRA
```

Every other `setEdits((current) => ({ of: current.of, values: … }))` keeps `extra: current.extra`. The exceptions are `onReset` and `onApplyPreset`:

```ts
  const onReset = useCallback(() => {
    if (schema) setEdits((current) => ({ of: current.of, values: defaultValues(schema), extra: NO_EXTRA }))
  }, [schema])

  const onApplyPreset = useCallback((next: ParamValues, nextExtra: InputsExtra) => {
    setEdits((current) => ({ of: current.of, values: next, extra: nextExtra }))
  }, [])
```

Pass `extra` to `useRenderJob(slug, settled ? debounced : undefined, version, extra)`, to `<PresetPicker … extra={extra} />` and to `<ActionBar … extra={extra} />`. The existing `setEdits({ of: seed, values: null })` in the source-changed reload becomes `setEdits({ of: seed, values: null, extra: null })`.

- [ ] **Step 9: The mocks**

In `frontend/src/mocks/handlers.ts`:
- Render: read `const body = (await request.json()) as { inputs?: { params?: Record<string, ParamValue> }; params?: Record<string, ParamValue> }`, then `const params = body.inputs?.params ?? body.params ?? {}`. Use `params` wherever the handler read `body.params`. Store `inputs: { ...(body.inputs ?? {}), params, v: 0 }` on the mock job, which the `GET /jobs/:id` handler returns.
- Presets create/update: `const inputs = body.inputs ?? { params: body.params ?? {} }`, `const params = (inputs.params ?? {}) as Record<string, ParamValue>`; run `presetRefusal` on `params`; store `{ …, params, inputs: { ...inputs, v: inputs.v ?? 0 } }`.
- Outputs create: when the body has `inputs`, store them on the created output as `{ ...inputs, v: 0 }`. Otherwise store `{ params: job.params, v: 0 }`.
- Edit target: return `inputs: output.inputs ?? { params: output.params, v: 0 }`.

- [ ] **Step 10: Run the frontend checks**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test`
Expected: all pass. Existing CustomizePage, ParameterPanel and PresetPicker tests pass unchanged apart from the `onApply` signature. If a test asserted a render body `{ params: … }`, change it to `{ inputs: { params: … } }`: that is the API change, not a regression.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/lib/inputs.ts frontend/src/lib/inputs.test.ts frontend/src/lib/saveOutput.ts frontend/src/api/client.ts frontend/src/lib/useRenderJob.ts frontend/src/lib/useRenderJob.test.ts frontend/src/lib/presets.ts frontend/src/components/PresetPicker.tsx frontend/src/components/PresetPicker.test.tsx frontend/src/components/ActionBar.tsx frontend/src/pages/CustomizePage.tsx frontend/src/pages/CustomizePage.test.tsx frontend/src/mocks/handlers.ts
git commit -m "feat(customize): the page carries template inputs through renders, presets and outputs (#425)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Host API v1 and mounting a template module

**Files:**
- Create: `frontend/src/template-ui/types.ts`, `host.ts`, `host.test.ts`, `loadModule.ts`, `styles.ts`, `TemplateUi.tsx`, `TemplateUi.test.tsx`

**Interfaces:**
- Consumes: `Json`, `JsonObject`, `mergePatch`, `isJsonObject` (Task 5); `api.uiFileUrl` (Task 5); `allParams` (`lib/params`); `useLatest` (`lib/useLatest`).
- Produces:
  ```ts
  // types.ts
  export const UI_API_CURRENT = 1
  export const UI_API_SUPPORTED: readonly number[]            // [1]
  export type UiSlot = 'panel' | 'page'
  export interface UiDeclaration { module: string; slot?: UiSlot; api: number }
  export interface MountContext { slot: UiSlot; version: string | null; theme: 'light' | 'dark'; api: number }
  export interface Host { api; inputs{get,set,subscribe}; schema(file?); files{url}; generate(): Promise<{jobId; outputId}>; openPrint(outputId); presets{list,save,load}; describe(fn) }
  export type MountResult = (() => void) | void
  export type Mount = (root: ShadowRoot, host: Host, ctx: MountContext) => MountResult | Promise<MountResult>
  export interface TemplateUiFailure { file: string; message: string }
  // host.ts
  export class HostInputError extends Error
  export interface HostDeps { slug; version: string | undefined; getSchema(): CustomizerSchema; getInputs(): JsonObject; setInputs(next: JsonObject): void; generate(): Promise<{ jobId: string; outputId: string }>; openPrint(outputId: string): void; presets: Host['presets']; onDescribe(fn: (() => string) | null): void }
  export interface HostHandle { host: Host; notify(inputs: JsonObject): void; dispose(): void }
  export function createHost(deps: HostDeps): HostHandle
  export function checkedUiPath(path: string): string
  // loadModule.ts
  export type UiModuleLoader = (url: string) => Promise<unknown>
  export function loadUiModule(url: string): Promise<unknown>
  export function setUiModuleLoader(next: UiModuleLoader | null): void
  // styles.ts
  export function adoptAppStyles(root: ShadowRoot): void
  // TemplateUi.tsx
  export function TemplateUi(props: { slug: string; ui: UiDeclaration; version: string | undefined; deps: HostDeps; inputs: JsonObject; onFailure(failure: TemplateUiFailure): void }): JSX.Element | null
  ```

- [ ] **Step 1: Write the failing host tests**

`frontend/src/template-ui/host.test.ts`:

```ts
import { describe, expect, it, vi } from 'vitest'
import { keychainSchema } from '../mocks/fixtures'
import type { JsonObject } from '../lib/inputs'
import { createHost, HostInputError, type HostDeps } from './host'

function deps(overrides: Partial<HostDeps> = {}): HostDeps & { state: { inputs: JsonObject } } {
  const state = { inputs: { params: { name: 'Hi' }, tab: 'a' } as JsonObject }
  return {
    state,
    slug: 'name-keychain',
    version: undefined,
    getSchema: () => keychainSchema,
    getInputs: () => state.inputs,
    setInputs: (next) => {
      state.inputs = next
    },
    generate: vi.fn(async () => ({ jobId: 'j', outputId: 'o' })),
    openPrint: vi.fn(),
    presets: { list: vi.fn(async () => []), save: vi.fn(), load: vi.fn(async () => undefined) },
    onDescribe: vi.fn(),
    ...overrides,
  }
}

describe('createHost', () => {
  it('applies a merge patch to the inputs', () => {
    const d = deps()
    createHost(d).host.inputs.set({ params: { name: 'Yo' }, tab: null, house: { storeys: 2 } })
    expect(d.state.inputs).toEqual({ params: { name: 'Yo' }, house: { storeys: 2 } })
  })

  it('hands out copies, so a UI cannot mutate the page state behind its back', () => {
    const d = deps()
    const got = createHost(d).host.inputs.get()
    ;(got['params'] as JsonObject)['name'] = 'mutated'
    expect(d.state.inputs).toEqual({ params: { name: 'Hi' }, tab: 'a' })
  })

  it('refuses a parameter the schema lacks', () => {
    const d = deps()
    expect(() => createHost(d).host.inputs.set({ params: { nope: 1 } })).toThrow(HostInputError)
    expect(() => createHost(d).host.inputs.set({ params: { nope: 1 } })).toThrow(/nope/)
    expect(d.state.inputs).toEqual({ params: { name: 'Hi' }, tab: 'a' })
  })

  it('refuses removing params or a non-scalar parameter value', () => {
    const d = deps()
    const { host } = createHost(d)
    expect(() => host.inputs.set({ params: null })).toThrow(HostInputError)
    expect(() => host.inputs.set({ params: { name: ['a'] } })).toThrow(/name/)
  })

  it('notifies subscribers until they unsubscribe', () => {
    const handle = createHost(deps())
    const seen: JsonObject[] = []
    const unsubscribe = handle.host.inputs.subscribe((inputs) => seen.push(inputs))
    handle.notify({ params: { name: 'A' } })
    unsubscribe()
    handle.notify({ params: { name: 'B' } })
    expect(seen).toEqual([{ params: { name: 'A' } }])
  })

  it('ignores writes after dispose', async () => {
    const d = deps()
    const handle = createHost(d)
    const seen = vi.fn()
    handle.host.inputs.subscribe(seen)
    handle.dispose()
    handle.host.inputs.set({ params: { name: 'late' } })
    handle.host.openPrint('o')
    handle.notify({ params: { name: 'x' } })
    await expect(handle.host.generate()).rejects.toThrow(/unmounted/)
    expect(d.state.inputs).toEqual({ params: { name: 'Hi' }, tab: 'a' })
    expect(d.openPrint).not.toHaveBeenCalled()
    expect(seen).not.toHaveBeenCalled()
    expect(d.onDescribe).toHaveBeenLastCalledWith(null)
  })

  it('serves only model.scad as a schema in v1', async () => {
    const { host } = createHost(deps())
    await expect(host.schema()).resolves.toBe(keychainSchema)
    await expect(host.schema('parts/roof.scad')).rejects.toThrow(/model.scad/)
  })

  it('builds asset URLs under ui/, pinned when there is a revision', () => {
    expect(createHost(deps()).host.files.url('img/roof.png')).toBe('/api/v1/models/name-keychain/ui/img/roof.png')
    expect(createHost(deps({ version: 'abc1234' })).host.files.url('a.css')).toBe(
      '/api/v1/models/name-keychain/versions/abc1234/ui/a.css',
    )
    expect(() => createHost(deps()).host.files.url('../model.scad')).toThrow()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/template-ui/host.test.ts`
Expected: FAIL, "Failed to resolve import './host'"

- [ ] **Step 3: Implement `types.ts` and `host.ts`**

`frontend/src/template-ui/types.ts`:

```ts
import type { CustomizerSchema, ParamPreset } from '../api/types'
import type { JsonObject } from '../lib/inputs'

export const UI_API_CURRENT = 1
/** The majors this host mounts: the current one and the one before it (spec §8.1). */
export const UI_API_SUPPORTED: readonly number[] = [UI_API_CURRENT]

export type UiSlot = 'panel' | 'page'

/** `model.json`'s `ui` (spec §4.1), as `ModelRecord.ui` carries it. */
export interface UiDeclaration {
  module: string
  slot?: UiSlot
  api: number
}

export interface MountContext {
  slot: UiSlot
  /** The revision the module was loaded from, or null for the live template. */
  version: string | null
  theme: 'light' | 'dark'
  /** The host-API major this host speaks. */
  api: number
}

/** Host API v1 (spec 2026-09-27 §4.3). */
export interface Host {
  readonly api: number
  inputs: {
    get(): JsonObject
    /** An RFC 7386 merge patch over the inputs; `null` deletes a key. */
    set(patch: JsonObject): void
    subscribe(fn: (inputs: JsonObject) => void): () => void
  }
  schema(file?: string): Promise<CustomizerSchema>
  files: { url(path: string): string }
  /** Waits for the render of the current inputs, then keeps it as an output. */
  generate(): Promise<{ jobId: string; outputId: string }>
  openPrint(outputId: string): void
  presets: {
    list(): Promise<ParamPreset[]>
    save(name: string): Promise<ParamPreset>
    load(id: string): Promise<void>
  }
  describe(fn: () => string): void
}

export type MountResult = (() => void) | void
export type Mount = (root: ShadowRoot, host: Host, ctx: MountContext) => MountResult | Promise<MountResult>

export interface TemplateUiFailure {
  /** The template file at fault: the module, or model.json. */
  file: string
  message: string
}
```

`frontend/src/template-ui/host.ts`:

```ts
import { api } from '../api/client'
import type { CustomizerSchema } from '../api/types'
import { isJsonObject, mergePatch, type JsonObject } from '../lib/inputs'
import { allParams } from '../lib/params'
import { UI_API_CURRENT, type Host } from './types'

/** A write a template UI made that the page refuses; the message names the key. */
export class HostInputError extends Error {
  override name = 'HostInputError'
}

export interface HostDeps {
  slug: string
  version: string | undefined
  getSchema(): CustomizerSchema
  getInputs(): JsonObject
  setInputs(next: JsonObject): void
  generate(): Promise<{ jobId: string; outputId: string }>
  openPrint(outputId: string): void
  presets: Host['presets']
  onDescribe(fn: (() => string) | null): void
}

export interface HostHandle {
  host: Host
  /** Tell the UI the inputs changed (a preset, a reopen, an agent, a widget). */
  notify(inputs: JsonObject): void
  /** The UI is unmounted: everything it still holds becomes a no-op. */
  dispose(): void
}

const UI_PATH = /^(?:[A-Za-z0-9_-][A-Za-z0-9._-]*\/)*[A-Za-z0-9_-][A-Za-z0-9._-]*$/

export function checkedUiPath(path: string): string {
  if (!UI_PATH.test(path)) throw new Error(`not a file under ui/: ${JSON.stringify(path)}`)
  return path
}

function checkedParams(schema: CustomizerSchema, patch: JsonObject): void {
  if (!('params' in patch)) return
  const params = patch['params']
  if (!isJsonObject(params)) throw new HostInputError('inputs.params cannot be removed or replaced by a non-object')
  const names = new Set(allParams(schema).map((param) => param.name))
  for (const [name, value] of Object.entries(params)) {
    if (!names.has(name)) throw new HostInputError(`inputs.params.${name}: model.scad has no parameter "${name}"`)
    if (!['string', 'number', 'boolean'].includes(typeof value)) {
      throw new HostInputError(`inputs.params.${name} must be a number, string or boolean`)
    }
  }
}

export function createHost(deps: HostDeps): HostHandle {
  const listeners = new Set<(inputs: JsonObject) => void>()
  let live = true
  const host: Host = {
    api: UI_API_CURRENT,
    inputs: {
      get: () => structuredClone(deps.getInputs()),
      set: (patch) => {
        if (!live) {
          console.warn('ScadBuddy: a template UI wrote its inputs after it was unmounted; ignored')
          return
        }
        checkedParams(deps.getSchema(), patch)
        const next = mergePatch(deps.getInputs(), structuredClone(patch))
        if (!isJsonObject(next)) throw new HostInputError('inputs must stay a JSON object')
        deps.setInputs(next)
      },
      subscribe: (fn) => {
        listeners.add(fn)
        return () => {
          listeners.delete(fn)
        }
      },
    },
    schema: async (file = 'model.scad') => {
      if (file !== 'model.scad') {
        throw new Error(`only model.scad has a customizer schema in host API v1, not ${file}`)
      }
      return deps.getSchema()
    },
    files: { url: (path) => api.uiFileUrl(deps.slug, deps.version, checkedUiPath(path)) },
    generate: () => (live ? deps.generate() : Promise.reject(new Error('the template UI is unmounted'))),
    openPrint: (outputId) => {
      if (live) deps.openPrint(outputId)
    },
    presets: {
      list: () => deps.presets.list(),
      save: (name) => deps.presets.save(name),
      load: (id) => deps.presets.load(id),
    },
    describe: (fn) => {
      if (live) deps.onDescribe(fn)
    },
  }
  return {
    host,
    notify: (inputs) => {
      if (!live) return
      for (const fn of listeners) fn(structuredClone(inputs))
    },
    dispose: () => {
      live = false
      listeners.clear()
      deps.onDescribe(null)
    },
  }
}
```

- [ ] **Step 4: Run the host tests**

Run: `cd frontend && pnpm exec vitest run src/template-ui/host.test.ts`
Expected: PASS

- [ ] **Step 5: Write the failing mount tests**

`frontend/src/template-ui/TemplateUi.test.tsx`:

```tsx
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { keychainSchema } from '../mocks/fixtures'
import type { HostDeps } from './host'
import { setUiModuleLoader } from './loadModule'
import { TemplateUi } from './TemplateUi'
import type { Mount } from './types'

const UI = { module: 'ui/index.js', slot: 'panel' as const, api: 1 }

function deps(): HostDeps {
  return {
    slug: 'name-keychain',
    version: 'abc1234',
    getSchema: () => keychainSchema,
    getInputs: () => ({ params: { name: 'Hi' } }),
    setInputs: vi.fn(),
    generate: vi.fn(),
    openPrint: vi.fn(),
    presets: { list: vi.fn(), save: vi.fn(), load: vi.fn() },
    onDescribe: vi.fn(),
  }
}

function withModule(mount: Mount | undefined) {
  const urls: string[] = []
  setUiModuleLoader(async (url) => {
    urls.push(url)
    return mount ? { mount } : {}
  })
  return urls
}

function shadow(container: HTMLElement): ShadowRoot {
  const root = container.querySelector('[data-testid="template-ui"]')?.shadowRoot
  if (!root) throw new Error('no shadow root')
  return root
}

afterEach(() => setUiModuleLoader(null))

describe('TemplateUi', () => {
  it('loads the module from the pinned revision and mounts it into a shadow root', async () => {
    const urls = withModule((root, host, ctx) => {
      const p = document.createElement('p')
      p.textContent = `${ctx.slot} ${ctx.version} api${host.api} ${String(host.inputs.get()['params'] && 'inputs')}`
      root.append(p)
    })
    const { container } = render(
      <TemplateUi slug="name-keychain" ui={UI} version="abc1234" deps={deps()} inputs={{ params: {} }} onFailure={vi.fn()} />,
    )
    await waitFor(() => expect(shadow(container).textContent).toBe('panel abc1234 api1 inputs'))
    expect(urls).toEqual(['/api/v1/models/name-keychain/versions/abc1234/ui/index.js'])
  })

  it.each([
    ['mount throws', (() => { throw new Error('boom') }) as Mount, 'boom'],
    ['async mount rejects', (async () => { throw new Error('later') }) as Mount, 'later'],
    ['no mount export', undefined, 'does not export a mount function'],
  ])('reports a failure when %s', async (_name, mount, message) => {
    withModule(mount)
    const onFailure = vi.fn()
    render(<TemplateUi slug="name-keychain" ui={UI} version={undefined} deps={deps()} inputs={{ params: {} }} onFailure={onFailure} />)
    await waitFor(() => expect(onFailure).toHaveBeenCalledWith({ file: 'ui/index.js', message: expect.stringContaining(message) }))
  })

  it('refuses an unsupported api major without loading anything', async () => {
    const urls = withModule(vi.fn())
    const onFailure = vi.fn()
    render(<TemplateUi slug="s" ui={{ ...UI, api: 7 }} version={undefined} deps={deps()} inputs={{ params: {} }} onFailure={onFailure} />)
    await waitFor(() => expect(onFailure).toHaveBeenCalledWith({ file: 'ui/index.js', message: expect.stringMatching(/API 7.*supports 1/) }))
    expect(urls).toEqual([])
  })

  it('runs the cleanup and empties the root on unmount, and notifies input changes', async () => {
    const cleanup = vi.fn()
    const seen: unknown[] = []
    withModule((root, host) => {
      root.append(document.createElement('span'))
      host.inputs.subscribe((inputs) => seen.push(inputs))
      return cleanup
    })
    const props = { slug: 's', ui: UI, version: undefined, deps: deps(), onFailure: vi.fn() }
    const { container, rerender, unmount } = render(<TemplateUi {...props} inputs={{ params: { a: 1 } }} />)
    await waitFor(() => expect(shadow(container).childNodes.length).toBe(1))
    rerender(<TemplateUi {...props} inputs={{ params: { a: 2 } }} />)
    await waitFor(() => expect(seen).toContainEqual({ params: { a: 2 } }))
    const root = shadow(container)
    unmount()
    expect(cleanup).toHaveBeenCalledOnce()
    expect(root.childNodes.length).toBe(0)
  })
})
```

- [ ] **Step 6: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/template-ui/TemplateUi.test.tsx`
Expected: FAIL, "Failed to resolve import './TemplateUi'"

- [ ] **Step 7: Implement the loader, styles and component**

`frontend/src/template-ui/loadModule.ts`:

```ts
export type UiModuleLoader = (url: string) => Promise<unknown>

const importModule: UiModuleLoader = (url) => import(/* @vite-ignore */ url)
let loader: UiModuleLoader = importModule

export function loadUiModule(url: string): Promise<unknown> {
  return loader(url)
}

/** Tests only: jsdom cannot `import()` a URL. `null` restores the real loader. */
export function setUiModuleLoader(next: UiModuleLoader | null): void {
  loader = next ?? importModule
}
```

`frontend/src/template-ui/styles.ts`:

```ts
/**
 * A shadow root inherits no stylesheet, so the host's widgets rendered into a template
 * UI would lose the app's look. The app's own rules are copied in as one constructed
 * sheet; custom properties (the theme tokens) inherit across the boundary on their own.
 */
export function adoptAppStyles(root: ShadowRoot): void {
  if (typeof CSSStyleSheet === 'undefined' || !('replaceSync' in CSSStyleSheet.prototype)) return
  const rules: string[] = []
  for (const sheet of Array.from(document.styleSheets)) {
    try {
      for (const rule of Array.from(sheet.cssRules)) rules.push(rule.cssText)
    } catch {
      // A cross-origin sheet's rules cannot be read; the page CSP allows none.
    }
  }
  const sheet = new CSSStyleSheet()
  sheet.replaceSync(rules.join('\n'))
  root.adoptedStyleSheets = [sheet]
}
```

`frontend/src/template-ui/TemplateUi.tsx`:

```tsx
import { useEffect, useRef } from 'react'
import { api } from '../api/client'
import type { JsonObject } from '../lib/inputs'
import { useLatest } from '../lib/useLatest'
import { createHost, type HostDeps, type HostHandle } from './host'
import { loadUiModule } from './loadModule'
import { adoptAppStyles } from './styles'
import { UI_API_SUPPORTED, type Mount, type TemplateUiFailure, type UiDeclaration } from './types'

interface Props {
  slug: string
  ui: UiDeclaration
  /** The revision to load the module from; undefined for the live template. */
  version: string | undefined
  deps: HostDeps
  inputs: JsonObject
  onFailure: (failure: TemplateUiFailure) => void
}

function mountOf(module: unknown): Mount | undefined {
  const mount = (module as { mount?: unknown } | null)?.mount
  return typeof mount === 'function' ? (mount as Mount) : undefined
}

function theme(): 'light' | 'dark' {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/** A template's own interface (spec 2026-09-27 §4.2): not sandboxed, style-isolated. */
export function TemplateUi({ slug, ui, version, deps, inputs, onFailure }: Props) {
  const element = useRef<HTMLDivElement>(null)
  const handle = useRef<HostHandle | null>(null)
  const latest = useLatest({ deps, onFailure })
  const slot = ui.slot ?? 'panel'

  useEffect(() => {
    const el = element.current
    if (!el) return
    const fail = (message: string) => latest.current.onFailure({ file: ui.module, message })
    if (!UI_API_SUPPORTED.includes(ui.api)) {
      fail(`written for host API ${ui.api}; this ScadBuddy supports ${UI_API_SUPPORTED.join(', ')}`)
      return
    }
    const root = el.shadowRoot ?? el.attachShadow({ mode: 'open' })
    adoptAppStyles(root)
    const created = createHost({
      slug,
      version,
      getSchema: () => latest.current.deps.getSchema(),
      getInputs: () => latest.current.deps.getInputs(),
      setInputs: (next) => latest.current.deps.setInputs(next),
      generate: () => latest.current.deps.generate(),
      openPrint: (id) => latest.current.deps.openPrint(id),
      presets: {
        list: () => latest.current.deps.presets.list(),
        save: (name) => latest.current.deps.presets.save(name),
        load: (id) => latest.current.deps.presets.load(id),
      },
      onDescribe: (fn) => latest.current.deps.onDescribe(fn),
    })
    handle.current = created
    let active = true
    let cleanup: (() => void) | void
    void (async () => {
      try {
        const module = await loadUiModule(api.uiFileUrl(slug, version, ui.module.replace(/^ui\//, '')))
        const mount = mountOf(module)
        if (!mount) throw new Error(`${ui.module} does not export a mount function`)
        if (!active) return
        const result = await mount(root, created.host, { slot, version: version ?? null, theme: theme(), api: ui.api })
        if (active) cleanup = result
        else if (typeof result === 'function') result()
      } catch (cause) {
        if (active) fail(cause instanceof Error ? cause.message : String(cause))
      }
    })()
    return () => {
      active = false
      created.dispose()
      handle.current = null
      try {
        if (typeof cleanup === 'function') cleanup()
      } catch (cause) {
        console.error(`${ui.module}: its cleanup threw`, cause)
      }
      root.replaceChildren()
    }
  }, [slug, version, ui.module, ui.api, slot, latest])

  useEffect(() => {
    handle.current?.notify(inputs)
  }, [inputs])

  return <div ref={element} data-testid="template-ui" className="h-full min-h-0 overflow-auto" />
}
```

- [ ] **Step 8: Run the tests and checks**

Run: `cd frontend && pnpm exec vitest run src/template-ui && pnpm lint && pnpm typecheck`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add frontend/src/template-ui
git commit -m "feat(customize): host API v1 and mounting a template module in a shadow root (#425)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Custom elements `<sb-param>`, `<sb-preview>`, `<sb-generate>`

The elements are thin registrations. `TemplateUi` renders the existing React widgets into each one with a portal, so every widget shares the page's one React tree (fonts, the preview's capture ref, agent highlighting). The template leaves an element's children to the host.

**Files:**
- Create: `frontend/src/template-ui/elements.ts`, `frontend/src/template-ui/HostElementContent.tsx`, `frontend/src/template-ui/elements.test.tsx`
- Modify: `frontend/src/template-ui/TemplateUi.tsx` (registry and portals)

**Interfaces:**
- Consumes: `TemplateUi` (Task 6); `ParamWidget` (`components/widgets/ParamWidget`); `allParams`, `extrudersOf` (`lib/params`); `getPath`, `setPath`, `splitInputs` (Task 5).
- Produces:
  ```ts
  // elements.ts
  export const ELEMENT_NAMES: readonly ['sb-param', 'sb-preview', 'sb-generate']
  export interface ElementRegistry { add(el: HostElement): void; remove(el: HostElement): void; changed(el: HostElement): void }
  export class HostElement extends HTMLElement
  export function provideRegistry(owner: HTMLElement, registry: ElementRegistry | undefined): void
  export function defineHostElements(): void
  // HostElementContent.tsx
  export interface ElementContext { schema: CustomizerSchema; slug: string; version?: string; fonts: FontFamily[]; inputs: JsonObject; onInputs(next: JsonObject): void; slot: UiSlot; preview: ReactNode; generate: ReactNode }
  export function HostElementContent(props: { element: HostElement; context: ElementContext }): ReactNode
  // TemplateUi props: + elementContext?: Omit<ElementContext, 'slot'>
  ```

- [ ] **Step 1: Write the failing test**

`frontend/src/template-ui/elements.test.tsx`:

```tsx
import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { keychainSchema } from '../mocks/fixtures'
import type { JsonObject } from '../lib/inputs'
import { setUiModuleLoader } from './loadModule'
import { TemplateUi } from './TemplateUi'

afterEach(() => setUiModuleLoader(null))

function page(inputs: JsonObject, onInputs: (next: JsonObject) => void, slot: 'panel' | 'page' = 'panel') {
  return (
    <TemplateUi
      slug="name-keychain"
      ui={{ module: 'ui/index.js', slot, api: 1 }}
      version={undefined}
      inputs={inputs}
      onFailure={vi.fn()}
      deps={{
        slug: 'name-keychain',
        version: undefined,
        getSchema: () => keychainSchema,
        getInputs: () => inputs,
        setInputs: onInputs,
        generate: vi.fn(),
        openPrint: vi.fn(),
        presets: { list: vi.fn(), save: vi.fn(), load: vi.fn() },
        onDescribe: vi.fn(),
      }}
      elementContext={{
        schema: keychainSchema,
        slug: 'name-keychain',
        fonts: [],
        inputs,
        onInputs,
        preview: <div data-testid="the-preview" />,
        generate: <button type="button">Generate</button>,
      }}
    />
  )
}

function shadowOf(container: HTMLElement): ShadowRoot {
  const root = container.querySelector('[data-testid="template-ui"]')?.shadowRoot
  if (!root) throw new Error('no shadow root')
  return root
}

describe('host custom elements', () => {
  it('renders a bound parameter widget and writes through its path', async () => {
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot) => {
        root.innerHTML = '<sb-param name="name"></sb-param><sb-param name="name" bind="style.label"></sb-param><sb-param name="nope"></sb-param>'
      },
    }))
    const onInputs = vi.fn()
    const { container } = render(page({ params: { name: 'Hi' }, style: { label: 'Yo' } }, onInputs))
    await waitFor(() => expect(shadowOf(container).querySelectorAll('input').length).toBeGreaterThanOrEqual(2))
    const [first, second] = Array.from(shadowOf(container).querySelectorAll('input'))
    expect((first as HTMLInputElement).value).toBe('Hi')
    expect((second as HTMLInputElement).value).toBe('Yo')
    fireEvent.change(second as HTMLInputElement, { target: { value: 'Ho' } })
    expect(onInputs).toHaveBeenLastCalledWith({ params: { name: 'Hi' }, style: { label: 'Ho' } })
    expect(shadowOf(container).textContent).toContain('model.scad has no parameter “nope”')
  })

  it('renders into an element inside a nested component shadow root', async () => {
    if (!customElements.get('x-nested-card')) {
      customElements.define(
        'x-nested-card',
        class extends HTMLElement {
          connectedCallback() {
            if (!this.shadowRoot) this.attachShadow({ mode: 'open' }).innerHTML = '<sb-param name="name"></sb-param>'
          }
        },
      )
    }
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot) => {
        root.innerHTML = '<x-nested-card></x-nested-card>'
      },
    }))
    const { container } = render(page({ params: { name: 'Hi' } }, vi.fn()))
    await waitFor(() =>
      expect(shadowOf(container).querySelector('x-nested-card')?.shadowRoot?.querySelector('input')?.value).toBe('Hi'),
    )
  })

  it('shows the preview only in the page slot, and Generate in both', async () => {
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot) => {
        root.innerHTML = '<sb-preview></sb-preview><sb-generate></sb-generate>'
      },
    }))
    const panel = render(page({ params: {} }, vi.fn(), 'panel'))
    await waitFor(() => expect(shadowOf(panel.container).textContent).toContain('Generate'))
    expect(shadowOf(panel.container).querySelector('[data-testid="the-preview"]')).toBeNull()
    panel.unmount()
    const full = render(page({ params: {} }, vi.fn(), 'page'))
    await waitFor(() => expect(shadowOf(full.container).querySelector('[data-testid="the-preview"]')).not.toBeNull())
  })
})
```

`keychainSchema`'s `name` parameter is a `string`, so `ParamWidget` renders an `<input>` for it (`mocks/fixtures.ts:70-76`).

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/template-ui/elements.test.tsx`
Expected: FAIL. `elementContext` is not a prop, and no input is rendered.

- [ ] **Step 3: Implement `elements.ts`**

```ts
export const ELEMENT_NAMES = ['sb-param', 'sb-preview', 'sb-generate'] as const

export interface ElementRegistry {
  add(el: HostElement): void
  remove(el: HostElement): void
  changed(el: HostElement): void
}

const REGISTRY = Symbol.for('scadbuddy.template-ui.registry')
type Owner = HTMLElement & { [REGISTRY]?: ElementRegistry }

/** Called by `TemplateUi` on the element whose shadow root the template mounts into. */
export function provideRegistry(owner: HTMLElement, registry: ElementRegistry | undefined): void {
  ;(owner as Owner)[REGISTRY] = registry
}

/** Outward through nested shadow roots, so an element a framework renders into its own
 * component's shadow root (spec §4.3, "under any framework") still finds the template's. */
function registryOf(el: Element): ElementRegistry | undefined {
  let root = el.getRootNode()
  while (root instanceof ShadowRoot) {
    const found = (root.host as Owner)[REGISTRY]
    if (found) return found
    root = root.host.getRootNode()
  }
  return undefined
}

/** A host widget placeholder (spec 2026-09-27 §4.3); the host renders into it. */
export class HostElement extends HTMLElement {
  static observedAttributes = ['name', 'file', 'bind']
  #registry: ElementRegistry | undefined

  connectedCallback(): void {
    this.#registry = registryOf(this)
    this.#registry?.add(this)
  }

  disconnectedCallback(): void {
    this.#registry?.remove(this)
    this.#registry = undefined
  }

  attributeChangedCallback(): void {
    this.#registry?.changed(this)
  }
}

export function defineHostElements(): void {
  for (const name of ELEMENT_NAMES) {
    if (!customElements.get(name)) customElements.define(name, class extends HostElement {})
  }
}
```

- [ ] **Step 4: Implement `HostElementContent.tsx`**

```tsx
import type { ReactNode } from 'react'
import type { CustomizerSchema, FontFamily, ParamValue } from '../api/types'
import { ParamWidget } from '../components/widgets/ParamWidget'
import { getPath, setPath, splitInputs, type JsonObject } from '../lib/inputs'
import { allParams, extrudersOf } from '../lib/params'
import type { HostElement } from './elements'
import type { UiSlot } from './types'

export interface ElementContext {
  schema: CustomizerSchema
  slug: string
  version?: string
  fonts: FontFamily[]
  inputs: JsonObject
  onInputs: (next: JsonObject) => void
  slot: UiSlot
  preview: ReactNode
  generate: ReactNode
}

function Problem({ children }: { children: ReactNode }) {
  return <p role="alert" className="px-3 py-2 text-[12px] text-warn">{children}</p>
}

function isParamValue(value: unknown): value is ParamValue {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
}

function BoundParam({ element, context }: { element: HostElement; context: ElementContext }) {
  const name = element.getAttribute('name') ?? ''
  const file = element.getAttribute('file') ?? 'model.scad'
  const bind = element.getAttribute('bind') ?? `params.${name}`
  if (file !== 'model.scad') return <Problem>Only model.scad has parameters in host API v1, not {file}.</Problem>
  const param = allParams(context.schema).find((candidate) => candidate.name === name)
  if (!param) return <Problem>model.scad has no parameter “{name}”.</Problem>
  const bound = getPath(context.inputs, bind)
  const value = isParamValue(bound) ? bound : (param.initial as ParamValue)
  const { params } = splitInputs(context.inputs)
  return (
    <ParamWidget
      param={param}
      value={value}
      slug={context.slug}
      version={context.version}
      fonts={context.fonts}
      extruder={extrudersOf(context.schema, params).get(name)}
      onChange={(next) => context.onInputs(setPath(context.inputs, bind, next))}
    />
  )
}

export function HostElementContent({ element, context }: { element: HostElement; context: ElementContext }) {
  switch (element.localName) {
    case 'sb-param':
      return <BoundParam element={element} context={context} />
    case 'sb-preview':
      // In the panel slot the host's own preview is beside the panel already.
      return context.slot === 'page' ? context.preview : null
    case 'sb-generate':
      return context.generate
    default:
      return null
  }
}
```

- [ ] **Step 5: Registry and portals in `TemplateUi`**

In `TemplateUi.tsx`: import `createPortal` from `react-dom`, `useState`, `defineHostElements`, `provideRegistry`, `HostElement`, `HostElementContent`, `ElementContext`. Add the prop `elementContext?: Omit<ElementContext, 'slot'>`. Then:

```tsx
  const [elements, setElements] = useState<readonly HostElement[]>([])
  const [, setRevision] = useState(0)
```

In the mount effect, before `const root = …`:

```tsx
    defineHostElements()
    provideRegistry(el, {
      add: (added) => setElements((current) => (current.includes(added) ? current : [...current, added])),
      remove: (removed) => setElements((current) => current.filter((candidate) => candidate !== removed)),
      changed: () => setRevision((n) => n + 1),
    })
```

and in its cleanup, after `root.replaceChildren()`: `provideRegistry(el, undefined)` and `setElements([])`.

Keys: a module-level `const keys = new WeakMap<HostElement, number>()` and `let nextKey = 0`, with `function keyOf(el: HostElement): string { let key = keys.get(el); if (key === undefined) { key = nextKey++; keys.set(el, key) } return String(key) }`.

Render:

```tsx
  return (
    <>
      <div ref={element} data-testid="template-ui" className="h-full min-h-0 overflow-auto" />
      {elementContext &&
        elements.map((el) =>
          createPortal(<HostElementContent element={el} context={{ ...elementContext, slot }} />, el, keyOf(el)),
        )}
    </>
  )
```

- [ ] **Step 6: Run the tests and checks**

Run: `cd frontend && pnpm exec vitest run src/template-ui && pnpm lint && pnpm typecheck`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add frontend/src/template-ui
git commit -m "feat(customize): sb-param, sb-preview and sb-generate custom elements (#425)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: The Customize page becomes a shell

**Files:**
- Modify: `frontend/src/pages/CustomizePage.tsx`, `frontend/src/components/ActionBar.tsx` (`ActionBarHandle`)
- Create: `frontend/src/mocks/templateUi.ts`, `frontend/src/pages/CustomizePage.ui.test.tsx`, `frontend/e2e/template-ui.spec.ts`
- Modify: `frontend/src/mocks/fixtures.ts` (`UI_DEMO_SLUG`, `UI_BROKEN_SLUG` models), `frontend/src/mocks/handlers.ts` (the two `ui/` routes), `frontend/vite.config.ts` (preview CSP)

**Interfaces:**
- Consumes: Tasks 5–7; `ModelSummary['ui']`, `ModelSummary['ui_error']`, `origin`, `origin_url`; `waitFor`, `committed` (`agent/highlight`).
- Produces:
  ```ts
  // ActionBar.tsx
  export interface ActionBarHandle { openPrint(outputId: string): void }
  // ActionBar Props: + ref?: Ref<ActionBarHandle>
  // mocks/templateUi.ts
  export const UI_MODULES: Record<string, Record<string, string>>   // slug -> ui path -> JS source
  // fixtures.ts
  export const UI_DEMO_SLUG = 'ui-demo'; export const UI_BROKEN_SLUG = 'ui-broken'
  // The agent's get_params gains: inputs: JsonObject, ui_summary: string | null
  ```

- [ ] **Step 1: Mocks: two templates with a UI and the module routes**

`frontend/src/mocks/templateUi.ts`:

```ts
/** Template UI modules the msw API serves (spec 2026-09-27 §4). */
export const UI_MODULES: Record<string, Record<string, string>> = {
  'ui-demo': {
    'index.js': `
import { GREETING } from './greeting.js'
export function mount(root, host) {
  const title = document.createElement('p')
  title.textContent = GREETING
  title.setAttribute('data-testid', 'ui-demo-greeting')
  const param = document.createElement('sb-param')
  param.setAttribute('name', 'name')
  const touch = document.createElement('button')
  touch.textContent = 'Remember me'
  touch.onclick = () => host.inputs.set({ demo: { touched: true } })
  root.append(title, param, touch)
  host.describe(() => 'the demo UI')
  return () => root.replaceChildren()
}
`,
    'greeting.js': `export const GREETING = 'Hello from the template'\n`,
  },
  'ui-broken': {
    'index.js': `export function mount() { throw new Error('the template UI is broken on purpose') }\n`,
  },
}
```

In `frontend/src/mocks/fixtures.ts`, export `UI_DEMO_SLUG = 'ui-demo'` and `UI_BROKEN_SLUG = 'ui-broken'`. Append two entries to `models`, each a copy of the `name-keychain` entry with its own `slug` and `name` (`'UI Demo'`, `'UI Broken'`), `origin: 'mine'`, and `ui: { module: 'ui/index.js', slot: 'panel', api: 1 }`. Add `schemas[UI_DEMO_SLUG] = keychainSchema` and `schemas[UI_BROKEN_SLUG] = keychainSchema`, in the same style as the other entries.

In `frontend/src/mocks/handlers.ts`, add before the catch-all model routes:

```ts
  // #425 — a template's own UI files, live or pinned; the module graph is plain JS.
  ...['/models/:slug/ui/*', '/models/:slug/versions/:commit/ui/*'].map((route) =>
    http.get(`${base}${route}`, ({ params, request }) => {
      const slug = decodeURIComponent(String(params['slug']))
      const path = decodeURIComponent(new URL(request.url).pathname.split('/ui/')[1] ?? '')
      const body = UI_MODULES[slug]?.[path]
      return body === undefined
        ? problem(404, 'Not Found', `no ui file '${path}'`)
        : new HttpResponse(body, { headers: { 'Content-Type': 'text/javascript; charset=utf-8' } })
    }),
  ),
```

- [ ] **Step 2: Write the failing page tests**

`frontend/src/pages/CustomizePage.ui.test.tsx`:

```tsx
import { screen, waitFor } from '@testing-library/react'
import { delay, http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { RENDER_DEBOUNCE_MS } from '../lib/useRenderJob'
import { keychainSchema, UI_BROKEN_SLUG, UI_DEMO_SLUG } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { setUiModuleLoader } from '../template-ui/loadModule'
import type { Host, Mount } from '../template-ui/types'
import { renderPage } from '../test/utils'
import { CustomizePage } from './CustomizePage'

const demo: Mount = (root, host) => {
  const p = document.createElement('p')
  p.textContent = `custom ${String((host.inputs.get()['params'] as Record<string, unknown>)['name'])}`
  root.append(p)
}

beforeEach(() => {
  setUiModuleLoader(async (url) => {
    if (url.includes(`/models/${UI_BROKEN_SLUG}/`)) return { mount: () => { throw new Error('broken on purpose') } }
    return { mount: demo }
  })
})
afterEach(() => setUiModuleLoader(null))

function open(slug: string) {
  return renderPage(<CustomizePage />, { route: `/m/${slug}`, path: '/m/:slug' })
}

function shadowText(): string {
  return document.querySelector('[data-testid="template-ui"]')?.shadowRoot?.textContent ?? ''
}

describe('CustomizePage with a template UI', () => {
  it('mounts the template UI in place of the generated form, and says where it came from', async () => {
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(shadowText()).toMatch(/^custom /))
    expect(screen.getByTestId('ui-origin')).toHaveTextContent('Custom interface · mine')
    expect(screen.queryByRole('tablist')).toBeNull()
  })

  it('falls back to the generated form with a banner naming the file and the error', async () => {
    open(UI_BROKEN_SLUG)
    const banner = await screen.findByRole('alert', { name: /template interface/i })
    expect(banner).toHaveTextContent('ui/index.js')
    expect(banner).toHaveTextContent('broken on purpose')
    expect(await screen.findByTestId('generate')).toBeInTheDocument()
    expect(document.querySelector('[data-param]')).not.toBeNull()
  })

  it('mounts only once a delayed schema is there, and host.schema() answers', async () => {
    server.use(
      http.get('/api/v1/models/:slug/schema', async () => {
        await delay(300)
        return HttpResponse.json(keychainSchema)
      }),
    )
    let seen: unknown
    setUiModuleLoader(async () => ({
      mount: async (_root: ShadowRoot, host: Host) => {
        seen = await host.schema()
      },
    }))
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(seen).toEqual(keychainSchema), { timeout: 3000 })
    expect(screen.queryByRole('alert', { name: /template interface/i })).toBeNull()
  })

  it('a UI-state-only set starts no new render', async () => {
    let host: Host | undefined
    setUiModuleLoader(async () => ({
      mount: (_root: ShadowRoot, given: Host) => {
        host = given
      },
    }))
    let renders = 0
    server.use(
      // Counts, then falls through to the regular mock handler (msw v2: no return value).
      http.post('/api/v1/models/:slug/render', () => {
        renders += 1
      }),
    )
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(renders).toBe(1))
    host?.inputs.set({ demo: { touched: true } })
    await new Promise((resolve) => setTimeout(resolve, RENDER_DEBOUNCE_MS * 2))
    expect(renders).toBe(1)
    expect(host?.inputs.get()['demo']).toEqual({ touched: true })
  })

  it('keeps a template without ui exactly on the generated form', async () => {
    open('name-keychain')
    await waitFor(() => expect(document.querySelector('[data-param]')).not.toBeNull())
    expect(document.querySelector('[data-testid="template-ui"]')).toBeNull()
    expect(screen.queryByTestId('ui-origin')).toBeNull()
  })
})
```

(`[data-param]` is the `<li data-param>` row `ParameterPanel` renders; its group tabs are a `tablist`, `ParameterPanel.tsx:74`.)

- [ ] **Step 3: Run to verify it fails**

Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/pages/CustomizePage.ui.test.tsx`
Expected: FAIL. The generated form renders for `ui-demo`, and no `template-ui` element exists.

- [ ] **Step 4: `ActionBarHandle`**

In `frontend/src/components/ActionBar.tsx`, import `useImperativeHandle` and `type Ref`, and add:

```ts
/** What a template UI's `host.openPrint` reaches (spec §4.3). */
export interface ActionBarHandle {
  openPrint(outputId: string): void
}
```

Add `ref?: Ref<ActionBarHandle>` to `Props` and to the destructured arguments, then:

```ts
  useImperativeHandle(
    ref,
    () => ({
      openPrint: (outputId) => {
        if (output?.id !== outputId) {
          throw new Error(`output ${outputId} is not the one on screen; call generate() first`)
        }
        setPrintOpen(true)
      },
    }),
    [output],
  )
```

- [ ] **Step 5: The shell**

In `frontend/src/pages/CustomizePage.tsx`, add imports: `TemplateUi` from `../template-ui/TemplateUi`, `type UiDeclaration` and `type TemplateUiFailure` from `../template-ui/types`, `type HostDeps` from `../template-ui/host`, `saveOutput`, `joinInputs`, `splitInputs`, `NO_EXTRA`, `JsonObject` from `../lib/inputs`, `sameValues` from `../lib/params`, `presetInputs`, `applyPreset` from `../lib/presets`, and `type ActionBarHandle`. Then add, after the `values`/`extra` lines from Task 5:

```tsx
  const record = modelState.data
  const declared = (record?.ui ?? null) as UiDeclaration | null
  const [uiFailure, setUiFailure] = useState<{ slug: string; failure: TemplateUiFailure } | null>(null)
  const failure =
    uiFailure?.slug === slug
      ? uiFailure.failure
      : record?.ui_error
        ? { file: 'model.json', message: record.ui_error }
        : null
  const customUi = declared && !failure ? declared : null
  // Wait for the record and the schema before choosing, so a template with a UI never
  // flashes the form, and a UI never mounts before `host.schema()` can answer.
  const choosing = (!record && !modelState.error) || !schema
  const [presetsRevision, setPresetsRevision] = useState(0)
  const inputs = useMemo(() => joinInputs(values, extra), [values, extra])
  const actions = useRef<ActionBarHandle>(null)
  const describeRef = useRef<(() => string) | null>(null)
  const uiVersion = version ?? record?.version ?? undefined
```

The host's dependencies read the page through `live` (the existing `useLatest` at the agent handlers). Extend that object with `inputs`, `extra`, `schema`, `ready: job?.status === 'done' && !rendering && settled && upToDate`, `job` and `output` (`values` is already there). Everything the memo's closures read comes through `live`, never through render-scoped values such as `requireSchema`, so a memo built before the schema arrived still sees it:

```tsx
  const schemaNow = useCallback(() => {
    const current = live.current.schema
    if (!current) throw new Error('the schema is still loading')
    return current
  }, [live])
  const hostDeps: HostDeps = useMemo(
    () => ({
      slug,
      version: uiVersion,
      getSchema: schemaNow,
      getInputs: () => live.current.inputs,
      setInputs: (next: JsonObject) => {
        const { params, extra: nextExtra } = splitInputs(next)
        setEdits((current) => {
          const shown = current.values ?? current.of ?? NOTHING
          // UI state alone must not re-render: keep the params object's identity, which
          // the debounce and useRenderJob key on, when the params did not change.
          return { of: current.of, values: sameValues(shown, params) ? shown : params, extra: nextExtra }
        })
      },
      generate: async () => {
        await waitFor(() => (live.current.ready ? true : undefined), {
          timeout: 120_000,
          what: 'the preview render of the current inputs',
        })
        const done = live.current.job
        if (!done) throw new Error('there is no render to keep')
        const created = await saveOutput({ slug, job: done, extra: live.current.extra, capture })
        setSaved({ jobId: done.id, output: created })
        outputsState.reload()
        await committed(() => live.current.output?.id === created.id, 'the saved output')
        return { jobId: done.id, outputId: created.id }
      },
      openPrint: (outputId: string) => actions.current?.openPrint(outputId),
      presets: {
        list: () => api.listPresets(slug),
        save: async (name: string) => {
          const created = await api.createPreset(slug, {
            name,
            inputs: presetInputs(schemaNow(), live.current.values, live.current.extra),
          })
          setPresetsRevision((n) => n + 1) // the picker keeps its own list; remount it
          return created
        },
        load: async (id: string) => {
          const preset = (await api.listPresets(slug)).find((candidate) => candidate.id === id)
          if (!preset) throw new Error(`no preset ${id}`)
          const applied = applyPreset(schemaNow(), preset)
          onApplyPreset(applied.values, applied.extra)
        },
      },
      onDescribe: (fn: (() => string) | null) => {
        describeRef.current = fn
      },
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- everything else is read through `live`
    [slug, uiVersion],
  )
```

(Place `schemaNow` and `hostDeps` after the existing `const live = useLatest(…)`. `capture`, `onApplyPreset`, `setSaved`, `outputsState` and `live` already exist on the page; `sameValues` is `lib/params`'s. Both `<PresetPicker>` elements in this task take `key={`${slug}:${presetsRevision}`}` so a preset saved by the UI shows up.)

The origin label beside a custom UI:

```tsx
  const originLabel =
    record?.origin === 'builtin'
      ? 'built-in'
      : record?.origin_url
        ? `imported from ${new URL(record.origin_url).host}`
        : 'mine'
```

The banner, rendered in the existing banner `<div>` below the header, after `modelState.error`:

```tsx
        {declared && failure && (
          <div
            role="alert"
            aria-label="Template interface failed"
            className="flex items-center gap-3 border-b border-warn/40 bg-warn/8 px-3 py-2 text-[12px] text-warn"
          >
            <span>
              This template&apos;s own interface ({failure.file}) could not start: {failure.message}. Showing the
              generated form instead.
            </span>
          </div>
        )}
```

The panel column. Replace the `<ParameterPanel …/>` element inside the flyout `<div>` with:

```tsx
          {choosing ? null : customUi ? (
            <div className="flex h-full min-h-0 flex-col">
              <div className="flex items-center gap-2 border-b border-line px-3 py-1.5">
                {full && <FlyoutHeader ref={flyoutClose} onClose={closeFlyout} />}
                <PresetPicker key={`${slug}:${presetsRevision}`} slug={slug} schema={schema} values={values} extra={extra} onApply={onApplyPreset} />
                <span data-testid="ui-origin" className="ml-auto shrink-0 text-[11px] text-faint">
                  Custom interface · {originLabel}
                </span>
              </div>
              <TemplateUi
                slug={slug}
                ui={customUi}
                version={uiVersion}
                deps={hostDeps}
                inputs={inputs}
                onFailure={(next) => setUiFailure({ slug, failure: next })}
                elementContext={elementContext}
              />
            </div>
          ) : (
            <ParameterPanel …the existing props, unchanged… />
          )}
```

with

built after the page's loading/error early returns, where `schema` is already narrowed to `CustomizerSchema`:

```tsx
  const elementContext = {
    schema,
    slug,
    version,
    fonts: fontsState.data ?? [],
    inputs,
    onInputs: hostDeps.setInputs,
    preview: previewElement,
    generate: (
      <Button
        onClick={() => void hostDeps.generate().catch(() => undefined)}
        disabled={rendering || !settled || job?.status !== 'done'}
      >
        {rendering || !settled ? (renderStage ? `Rendering: ${renderStage}` : 'Rendering…') : 'Generate'}
      </Button>
    ),
  }
```

where `previewElement` is the existing `<Suspense …><Preview …/></Suspense>` block, hoisted into a variable so that the workspace and `<sb-preview>` render the same element. Only one of them is ever mounted: `sb-preview` renders it only in the page slot, and in the page slot the workspace does not.

The page slot. When `customUi?.slot === 'page'`, render this in place of the whole `data-testid="workspace"` grid:

```tsx
        <div data-testid="workspace" className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)_auto]">
          <div className="flex items-center gap-2 border-b border-line px-3 py-1.5">
            <PresetPicker key={`${slug}:${presetsRevision}`} slug={slug} schema={schema} values={values} extra={extra} onApply={onApplyPreset} />
            <span data-testid="ui-origin" className="ml-auto shrink-0 text-[11px] text-faint">
              Custom interface · {originLabel}
            </span>
          </div>
          <TemplateUi slug={slug} ui={customUi} version={uiVersion} deps={hostDeps} inputs={inputs}
            onFailure={(next) => setUiFailure({ slug, failure: next })} elementContext={elementContext} />
          <ActionBar ref={actions} …the existing ActionBar props… />
        </div>
```

and pass `ref={actions}` to the existing `<ActionBar>` in the panel layout as well.

The agent's `get_params` (same file) adds `inputs: joinInputs(values, extra)` and `ui_summary: describeRef.current?.() ?? null` to its result. In `frontend/src/agent/catalog.ts`, `get_params`'s description gains: " With a template's own interface, also its full inputs and the interface's own summary (`ui_summary`)."

- [ ] **Step 6: Run the page tests and every existing frontend test**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test`
Expected: all pass. The no-ui test proves the generated form is untouched, and the existing `CustomizePage*.test.tsx` suites pass unchanged.

- [ ] **Step 7: The preview CSP and the e2e test**

`frontend/vite.config.ts`: add `import { readFileSync } from 'node:fs'` and give `preview` the page policy the backend sends:

```ts
  preview: {
    host: '127.0.0.1',
    port: 4173,
    // The backend's PAGE_CSP (backend/scadbuddy/api/static.py), so the mocked e2e run
    // meets the same policy a template UI does in production (spec §9).
    headers: { 'Content-Security-Policy': readFileSync(new URL('./page-csp.txt', import.meta.url), 'utf8').trim() },
  },
```

`frontend/e2e/template-ui.spec.ts`:

```ts
import { expect, test, type Page } from '@playwright/test'

async function trackCsp(page: Page) {
  await page.addInitScript(() => {
    const seen: string[] = []
    ;(window as unknown as { __csp: string[] }).__csp = seen
    document.addEventListener('securitypolicyviolation', (event) => {
      seen.push(`${event.violatedDirective} ${event.blockedURI}`)
    })
  })
}

async function violations(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { __csp: string[] }).__csp)
}

test('a template UI mounts, binds a parameter and records its own state', async ({ page }) => {
  await trackCsp(page)
  await page.goto('/m/ui-demo')
  await expect(page.getByTestId('ui-demo-greeting')).toHaveText('Hello from the template')
  await expect(page.getByTestId('ui-origin')).toContainText('Custom interface')
  // The POST that carries the filled value: "Remember me" alone starts no render.
  const render = page.waitForRequest(
    (request) =>
      request.method() === 'POST' &&
      request.url().endsWith('/models/ui-demo/render') &&
      (request.postDataJSON() as { inputs?: { params?: { name?: string } } }).inputs?.params?.name === 'Zed',
  )
  await page.getByRole('button', { name: 'Remember me' }).click()
  await page.getByTestId('template-ui').getByRole('textbox').first().fill('Zed')
  const body = (await render).postDataJSON() as { inputs: { params: { name: string }; demo?: unknown } }
  expect(body.inputs.params.name).toBe('Zed')
  expect(body.inputs.demo).toEqual({ touched: true })
  expect(await violations(page)).toEqual([])
})

test('a UI that throws leaves the template usable', async ({ page }) => {
  await page.goto('/m/ui-broken')
  await expect(page.getByRole('alert', { name: /template interface/i })).toContainText('broken on purpose')
  await expect(page.getByTestId('generate')).toBeVisible()
})

test('the page CSP breaks nothing the app does', async ({ page }) => {
  await trackCsp(page)
  await page.goto('/')
  await page.goto('/m/name-keychain')
  await expect(page.getByTestId('generate')).toBeVisible()
  // The font picker loads Google Fonts CSS and font files. Offline, the request fails, but
  // that is a network error, never a securitypolicyviolation.
  await page.locator('[data-param="font"]').getByRole('button').first().click()
  await page.waitForLoadState('networkidle')
  expect(await violations(page)).toEqual([])
  await page.getByRole('link', { name: /source/i }).click()
  await page.waitForLoadState('networkidle')
  expect(await violations(page)).toEqual([])
})
```

(Playwright locators pierce open shadow roots, and requests answered by the msw service worker still reach `waitForRequest`.)

- [ ] **Step 8: Run the e2e suite**

Run: `cd frontend && pnpm build && pnpm exec playwright test`
Expected: all specs pass, including the existing ones under the CSP. A violation reported by the third test is a real finding. Fix it by adding the directive the app needs to `PAGE_CSP` and `page-csp.txt` in the same commit, never by removing the check.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/pages/CustomizePage.tsx frontend/src/pages/CustomizePage.ui.test.tsx frontend/src/components/ActionBar.tsx frontend/src/agent/catalog.ts frontend/src/mocks/templateUi.ts frontend/src/mocks/fixtures.ts frontend/src/mocks/handlers.ts frontend/vite.config.ts frontend/e2e/template-ui.spec.ts
git commit -m "feat(customize): the page mounts a template UI in the panel or page slot, falling back to the form (#425)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: `maze-puzzle` shows the lid colour only with a lid

The lid-only parameter in `models/maze-puzzle/model.scad` is `lid_color` (lines 66–67: "Lid, ball_lid mode only"). `ball_d` applies in both modes, because corridors widen for the ball either way.

**Files:**
- Create: `models/maze-puzzle/ui/index.js`, `frontend/src/template-ui/maze.test.ts`
- Modify: `models/maze-puzzle/model.json` (`ui`), `models/maze-puzzle/README.md` (one paragraph)
- Create: `frontend/src/mocks/ui/maze-puzzle.js` (a byte-identical copy of `models/maze-puzzle/ui/index.js`, for the msw mocks), `frontend/src/mocks/ui/copies.test.ts`
- Modify: `frontend/eslint.config.js` (browser globals for the plain-JS copies)
- Modify: `frontend/src/mocks/fixtures.ts`, `frontend/src/mocks/templateUi.ts` (a `builtin:maze-puzzle` mock serving the copy), `frontend/e2e/template-ui.spec.ts`

**Interfaces:**
- Consumes: Host v1 (Task 6), `<sb-param>` (Task 7).
- Produces: `models/maze-puzzle/ui/index.js` exporting `async function mount(root, host)`; `LID_ONLY = ['lid_color']`.

- [ ] **Step 1: Write the failing test**

`frontend/src/template-ui/maze.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import type { CustomizerSchema } from '../api/types'
import type { JsonObject } from '../lib/inputs'
import type { Host, Mount } from './types'

const SCHEMA = {
  parameters: [
    { name: 'mode', type: 'select', group: 'Play', initial: 'open_tray', caption: '', options: [] },
    { name: 'ball_d', type: 'slider', group: 'Play', initial: 6, caption: '' },
    { name: 'wall_color', type: 'color', group: 'Colours', initial: '#006064', caption: '' },
    { name: 'lid_color', type: 'color', group: 'Colours', initial: '#FFFFFF', caption: '' },
  ],
} as unknown as CustomizerSchema

function fakeHost(initial: JsonObject) {
  let inputs = initial
  const listeners: ((i: JsonObject) => void)[] = []
  const host = {
    api: 1,
    inputs: {
      get: () => inputs,
      set: () => undefined,
      subscribe: (fn: (i: JsonObject) => void) => {
        listeners.push(fn)
        return () => undefined
      },
    },
    schema: async () => SCHEMA,
    describe: () => undefined,
  } as unknown as Host
  return {
    host,
    change(next: JsonObject) {
      inputs = next
      for (const fn of listeners) fn(next)
    },
  }
}

async function mountMaze(initial: JsonObject) {
  const url = new URL('../../../models/maze-puzzle/ui/index.js', import.meta.url).href
  const { mount } = (await import(/* @vite-ignore */ url)) as { mount: Mount }
  const root = document.createElement('div').attachShadow({ mode: 'open' })
  const fake = fakeHost(initial)
  const cleanup = await mount(root, fake.host, { slot: 'panel', version: null, theme: 'light', api: 1 })
  const lid = () => root.querySelector('sb-param[name="lid_color"]') as HTMLElement
  return { root, lid, change: fake.change, cleanup }
}

describe('maze-puzzle ui', () => {
  it('renders every parameter, grouped, and hides the lid colour without a lid', async () => {
    const { root, lid } = await mountMaze({ params: { mode: 'open_tray' } })
    expect(Array.from(root.querySelectorAll('sb-param')).map((el) => el.getAttribute('name'))).toEqual([
      'mode', 'ball_d', 'wall_color', 'lid_color',
    ])
    expect(Array.from(root.querySelectorAll('h3')).map((el) => el.textContent)).toEqual(['Play', 'Colours'])
    expect(lid().hidden).toBe(true)
  })

  it('shows it once the mode has a lid, and hides it again', async () => {
    const { lid, change } = await mountMaze({ params: { mode: 'open_tray' } })
    change({ params: { mode: 'ball_lid' } })
    expect(lid().hidden).toBe(false)
    change({ params: { mode: 'open_tray' } })
    expect(lid().hidden).toBe(true)
  })

  it('reads the default mode when the inputs leave it out', async () => {
    const { lid } = await mountMaze({ params: {} })
    expect(lid().hidden).toBe(true)
  })

  it('removes what it drew on cleanup', async () => {
    const { root, cleanup } = await mountMaze({ params: {} })
    if (typeof cleanup === 'function') cleanup()
    expect(root.childNodes.length).toBe(0)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/template-ui/maze.test.ts`
Expected: FAIL, failed to load `models/maze-puzzle/ui/index.js`

- [ ] **Step 3: The module**

`models/maze-puzzle/ui/index.js`:

```js
// ScadBuddy template UI, host API v1 (docs/superpowers/specs/2026-09-27-template-pipelines-design.md §4):
// the generated form, with the lid's colour shown only when there is a lid.

const LID_ONLY = new Set(['lid_color'])

function modeOf(inputs, schema) {
  const mode = inputs.params?.mode
  if (typeof mode === 'string') return mode
  return schema.parameters.find((param) => param.name === 'mode')?.initial
}

export async function mount(root, host) {
  const schema = await host.schema()
  const form = document.createElement('div')
  form.className = 'flex flex-col'
  let group = null
  for (const param of schema.parameters) {
    const name = param.group || 'Parameters'
    if (name !== group) {
      group = name
      const heading = document.createElement('h3')
      heading.className = 'px-3 pt-3 text-[11px] font-medium uppercase tracking-wide text-faint'
      heading.textContent = name
      form.append(heading)
    }
    const field = document.createElement('sb-param')
    field.setAttribute('name', param.name)
    form.append(field)
  }
  root.append(form)

  function draw(inputs) {
    const lid = modeOf(inputs, schema) === 'ball_lid'
    for (const field of form.querySelectorAll('sb-param')) {
      field.hidden = LID_ONLY.has(field.getAttribute('name')) && !lid
    }
  }
  draw(host.inputs.get())
  const unsubscribe = host.inputs.subscribe(draw)
  host.describe?.(() =>
    modeOf(host.inputs.get(), schema) === 'ball_lid'
      ? 'Ball maze with a snap-on lid; lid_color is shown.'
      : 'Open-tray ball maze; lid_color is hidden because there is no lid.',
  )
  return () => {
    unsubscribe()
    form.remove()
  }
}
```

`models/maze-puzzle/model.json`: add `"ui": {"module": "ui/index.js", "slot": "panel", "api": 1}` after `"source"`. `models/maze-puzzle/README.md`: add a paragraph that says the customizer hides the lid colour unless the mode is "Ball with snap-on lid", and that this is the template's own `ui/index.js`.

- [ ] **Step 4: Run it**

Run: `cd frontend && pnpm exec vitest run src/template-ui/maze.test.ts`
Expected: PASS
Run: `cd backend && uv run --frozen pytest tests/test_bundled_ui.py -q`
Expected: PASS, one case per bundled `ui` (`maze-puzzle` among them)

- [ ] **Step 5: The mocked e2e run serves the real module**

The Dockerfile's `frontend` stage copies only `frontend/`, so nothing the bundle can reach may import from `models/`. Copy the module: `cp models/maze-puzzle/ui/index.js frontend/src/mocks/ui/maze-puzzle.js`. In `frontend/src/mocks/templateUi.ts`, `import mazeUi from './ui/maze-puzzle.js?raw'` and add `'builtin:maze-puzzle': { 'index.js': mazeUi }` to `UI_MODULES`. `frontend/src/mocks/ui/copies.test.ts` keeps the copy honest (it runs in the CI `frontend` job, which has the whole checkout):

```ts
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('mock copies of bundled template UIs', () => {
  it('maze-puzzle.js is byte-identical to models/maze-puzzle/ui/index.js', () => {
    const read = (path: string) => readFileSync(new URL(path, import.meta.url))
    expect(read('./maze-puzzle.js').equals(read('../../../../models/maze-puzzle/ui/index.js'))).toBe(true)
  })
})
```

The copy is plain browser JS, which `eslint .` would reject with `no-undef` for `document`. In `frontend/eslint.config.js`, add after the `**/*.{ts,tsx}` block:

```js
  {
    // Copies of bundled template UIs (models/*/ui/*.js): plain browser modules.
    files: ['src/mocks/ui/**/*.js'],
    languageOptions: { ecmaVersion: 2022, sourceType: 'module', globals: globals.browser },
  },
```

Then, in `fixtures.ts`, add a `builtin:maze-puzzle` model (`origin: 'builtin'`, `ui` as above). Add its schema: the four parameters from the test's `SCHEMA`, where `mode` gets the options `open_tray:Open tray` and `ball_lid:Ball with snap-on lid`, shaped like `keychainSchema`'s select parameter. Append to `frontend/e2e/template-ui.spec.ts`:

```ts
test('maze-puzzle shows the lid colour only with a lid', async ({ page }) => {
  await page.goto('/m/builtin%3Amaze-puzzle')
  const ui = page.getByTestId('template-ui')
  await expect(ui.locator('sb-param[name="wall_color"]')).toBeVisible()
  await expect(ui.locator('sb-param[name="lid_color"]')).toBeHidden()
  await ui.getByRole('combobox').first().selectOption('ball_lid')
  await expect(ui.locator('sb-param[name="lid_color"]')).toBeVisible()
  await expect(page.getByTestId('ui-origin')).toHaveText('Custom interface · built-in')
})
```

(`SelectWidget` renders a native `<select>`, `SelectWidget.tsx:18`.)

- [ ] **Step 6: Checks**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test e2e/template-ui.spec.ts`
Expected: PASS. `models/maze-puzzle/verify.sh` is unaffected, since it renders `model.scad` only.
Run: `docker build --target frontend -t scadbuddy-frontend:check .`
Expected: the build succeeds (no `models/` import reaches the bundle). Its model directory changed, so the `models` CI job runs it on the PR.

- [ ] **Step 7: Commit**

```bash
git add models/maze-puzzle/ui/index.js models/maze-puzzle/model.json models/maze-puzzle/README.md frontend/src/template-ui/maze.test.ts frontend/src/mocks/ui frontend/eslint.config.js frontend/src/mocks/templateUi.ts frontend/src/mocks/fixtures.ts frontend/e2e/template-ui.spec.ts
git commit -m "feat(models): maze-puzzle's own panel hides the lid colour without a lid (#425)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: `dollhouse-kit` house designer (one piece per Generate)

The designer takes a box house (columns × rows of 150 mm modules, 1–3 storeys, and a number of windows per storey), lists every piece it needs with counts, and shows the chosen piece in the preview. Generate keeps that one piece. Rendering the whole house in one Generate is phase 4's pipeline. Its state lives in `inputs.house`. Everything it renders is `inputs.params`.

**Files:**
- Create: `models/dollhouse-kit/ui/pieces.js`, `models/dollhouse-kit/ui/index.js`, `frontend/src/template-ui/dollhouse.test.ts`
- Modify: `models/dollhouse-kit/model.json` (`ui`, slot `page`), `models/dollhouse-kit/README.md` (one paragraph)

**Interfaces:**
- Consumes: Host v1, `<sb-param>`, `<sb-preview>`, `<sb-generate>`.
- Produces (`ui/pieces.js`):
  ```js
  export const LIMITS = { cols: [1, 4], rows: [1, 3], storeys: [1, 3], windows: [0, 14] }
  export const DEFAULT_HOUSE = { cols: 2, rows: 1, storeys: 1, windows: 2 }
  export function clampHouse(house) -> { cols, rows, storeys, windows }
  export function housePieces(house) -> Array<{ id: string, piece: string, course: 'lower'|'upper'|null, count: number, label: string }>
  export function pieceParams(entry) -> { piece, course?, width_units: 1, depth_units: 1 }
  ```
  Counting rules, on a one-module grid (`width_units = depth_units = 1`), perimeter `P = 2 × (cols + rows)`:
  - ground storey: lower course 1 `wall_door_lower` + `P−1` `wall`; upper course 1 `wall_door_upper` + `min(windows, P−1)` `wall_window` + the rest `wall`;
  - each upper storey: lower course `P` `wall`; upper course `min(windows, P)` `wall_window` + the rest `wall`;
  - `corner_post`: 8 per storey (4 per course); `floor_tile`: `cols × rows` per storey; `roof_panel`: `cols × rows`;
  - `stairs_lower`, `stairs_upper`, `railing`: `storeys − 1` each; `door_leaf_lower`, `door_leaf_upper`: 1 each; `connectors`: 1 set;
  - entries with count 0 are left out.

- [ ] **Step 1: Write the failing test**

`frontend/src/template-ui/dollhouse.test.ts`:

```ts
import { describe, expect, it, vi } from 'vitest'
import type { CustomizerSchema } from '../api/types'
import type { JsonObject } from '../lib/inputs'
import type { Host, Mount } from './types'

const base = new URL('../../../models/dollhouse-kit/ui/', import.meta.url).href
type Entry = { id: string; piece: string; course: string | null; count: number }
const load = async () =>
  (await import(/* @vite-ignore */ `${base}pieces.js`)) as {
    housePieces: (h: object) => Entry[]
    clampHouse: (h: object) => object
    pieceParams: (e: Entry) => object
  }

describe('housePieces', () => {
  it('counts a one-room, one-storey house', async () => {
    const { housePieces } = await load()
    const counts = Object.fromEntries(
      housePieces({ cols: 1, rows: 1, storeys: 1, windows: 2 }).map((e) => [e.id, e.count]),
    )
    expect(counts).toEqual({
      'wall_door_lower': 1, 'wall:lower': 3, 'wall_door_upper': 1, 'wall_window': 2, 'wall:upper': 1,
      'corner_post': 8, 'floor_tile': 1, 'roof_panel': 1, 'door_leaf_lower': 1, 'door_leaf_upper': 1,
      'connectors': 1,
    })
  })

  it('counts a two-by-one, two-storey house with stairs', async () => {
    const { housePieces } = await load()
    const counts = Object.fromEntries(
      housePieces({ cols: 2, rows: 1, storeys: 2, windows: 3 }).map((e) => [e.id, e.count]),
    )
    // P = 6. Ground: 5 lower walls + door; upper: door + 3 windows + 2 walls.
    // First floor: 6 lower walls; upper: 3 windows + 3 walls.
    expect(counts['wall:lower']).toBe(11)
    expect(counts['wall_window']).toBe(6)
    expect(counts['wall:upper']).toBe(5)
    expect(counts['floor_tile']).toBe(4)
    expect(counts['roof_panel']).toBe(2)
    expect(counts['corner_post']).toBe(16)
    expect(counts['stairs_lower']).toBe(1)
    expect(counts['railing']).toBe(1)
  })

  it('clamps out-of-range and non-numeric house values', async () => {
    const { clampHouse } = await load()
    expect(clampHouse({ cols: 99, rows: -1, storeys: 'x', windows: 2.7 })).toEqual({ cols: 4, rows: 1, storeys: 1, windows: 2 })
  })

  it('renders one piece on the one-module grid', async () => {
    const { pieceParams } = await load()
    expect(pieceParams({ id: 'wall:upper', piece: 'wall', course: 'upper', count: 1 })).toEqual({
      piece: 'wall', course: 'upper', width_units: 1, depth_units: 1,
    })
  })
})

describe('the designer', () => {
  it('lists the pieces and shows the one picked', async () => {
    const { mount } = (await import(/* @vite-ignore */ `${base}index.js`)) as { mount: Mount }
    const schema = {
      parameters: [
        { name: 'piece', type: 'select', group: 'Piece', initial: 'wall_window', caption: '' },
        { name: 'course', type: 'select', group: 'Piece', initial: 'upper', caption: '' },
        { name: 'exterior', type: 'select', group: 'Exterior', initial: 'plain', caption: '' },
      ],
    } as unknown as CustomizerSchema
    let inputs: JsonObject = { params: {} }
    const set = vi.fn((patch: JsonObject) => {
      inputs = { ...inputs, ...patch }
    })
    const host = {
      api: 1,
      inputs: { get: () => inputs, set, subscribe: () => () => undefined },
      schema: async () => schema,
      describe: () => undefined,
    } as unknown as Host
    const root = document.createElement('div').attachShadow({ mode: 'open' })
    await mount(root, host, { slot: 'page', version: null, theme: 'light', api: 1 })
    expect(root.querySelector('sb-preview')).not.toBeNull()
    expect(root.querySelector('sb-generate')).not.toBeNull()
    expect(root.querySelector('sb-param[name="exterior"]')).not.toBeNull()
    expect(root.querySelector('sb-param[name="piece"]')).toBeNull()
    expect(set).toHaveBeenCalledWith({ house: { cols: 2, rows: 1, storeys: 1, windows: 2 } })
    const floor = root.querySelector('button[data-piece="floor_tile"]') as HTMLButtonElement
    floor.click()
    expect(set).toHaveBeenLastCalledWith({ params: { piece: 'floor_tile', width_units: 1, depth_units: 1 } })
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm exec vitest run src/template-ui/dollhouse.test.ts`
Expected: FAIL, failed to load `pieces.js`

- [ ] **Step 3: `ui/pieces.js`**

```js
// The dollhouse designer's bill of pieces (#425): a box house on a one-module grid.
// Pure, so it is tested on its own; ui/index.js draws it.

export const LIMITS = { cols: [1, 4], rows: [1, 3], storeys: [1, 3], windows: [0, 14] }
export const DEFAULT_HOUSE = { cols: 2, rows: 1, storeys: 1, windows: 2 }

const LABELS = {
  wall: 'Wall',
  wall_window: 'Wall with window',
  wall_door_lower: 'Door wall, lower course',
  wall_door_upper: 'Door wall, upper course',
  door_leaf_lower: 'Door leaf, lower half',
  door_leaf_upper: 'Door leaf, upper half',
  corner_post: 'Corner post',
  floor_tile: 'Floor tile',
  roof_panel: 'Roof panel',
  stairs_lower: 'Stairs, lower half',
  stairs_upper: 'Stairs, upper half',
  railing: 'Railing',
  connectors: 'Connectors (keys, pegs, hinge pins)',
}

export function clampHouse(house) {
  const out = {}
  for (const [key, [min, max]] of Object.entries(LIMITS)) {
    const value = Math.trunc(Number(house?.[key]))
    out[key] = Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : DEFAULT_HOUSE[key]
  }
  return out
}

export function housePieces(input) {
  const { cols, rows, storeys, windows } = clampHouse(input)
  const perimeter = 2 * (cols + rows)
  const counts = new Map()
  const add = (piece, course, count) => {
    if (count <= 0) return
    const id = piece === 'wall' ? `wall:${course}` : piece
    const entry = counts.get(id) ?? { id, piece, course: piece === 'wall' ? course : null, count: 0, label: '' }
    entry.count += count
    entry.label = piece === 'wall' ? `${LABELS.wall}, ${course} course` : LABELS[piece]
    counts.set(id, entry)
  }
  for (let storey = 0; storey < storeys; storey++) {
    const ground = storey === 0
    const openings = ground ? perimeter - 1 : perimeter
    const glazed = Math.min(windows, openings)
    if (ground) {
      add('wall_door_lower', null, 1)
      add('wall_door_upper', null, 1)
    }
    add('wall', 'lower', openings)
    add('wall_window', null, glazed)
    add('wall', 'upper', openings - glazed)
    add('corner_post', null, 8)
    add('floor_tile', null, cols * rows)
  }
  add('roof_panel', null, cols * rows)
  add('stairs_lower', null, storeys - 1)
  add('stairs_upper', null, storeys - 1)
  add('railing', null, storeys - 1)
  add('door_leaf_lower', null, 1)
  add('door_leaf_upper', null, 1)
  add('connectors', null, 1)
  return [...counts.values()]
}

export function pieceParams(entry) {
  return {
    piece: entry.piece,
    ...(entry.course ? { course: entry.course } : {}),
    width_units: 1,
    depth_units: 1,
  }
}
```

- [ ] **Step 4: `ui/index.js`**

```js
// ScadBuddy template UI, host API v1 (docs/superpowers/specs/2026-09-27-template-pipelines-design.md §4):
// a house designer. It lists the pieces a box house needs and previews one at a time;
// Generate keeps the piece on screen. The whole house in one Generate is phase 4's pipeline.

import { DEFAULT_HOUSE, LIMITS, clampHouse, housePieces, pieceParams } from './pieces.js'

const DRIVEN = new Set(['piece', 'course', 'width_units', 'depth_units'])
const HIDDEN_GROUPS = new Set(['Piece', 'Grid'])

function element(tag, props = {}, ...children) {
  const el = document.createElement(tag)
  Object.assign(el, props)
  el.append(...children)
  return el
}

export async function mount(root, host) {
  const schema = await host.schema()
  if (!host.inputs.get().house) host.inputs.set({ house: { ...DEFAULT_HOUSE } })

  const form = element('div', { className: 'flex flex-col gap-2 p-3' })
  const numbers = {}
  for (const [key, [min, max]] of Object.entries(LIMITS)) {
    const input = element('input', { type: 'number', min: String(min), max: String(max), step: '1' })
    input.addEventListener('change', () => {
      const house = clampHouse({ ...host.inputs.get().house, [key]: input.value })
      host.inputs.set({ house })
    })
    numbers[key] = input
    form.append(element('label', { className: 'flex items-center justify-between gap-2 text-[13px]' }, key, input))
  }

  const style = element('div', { className: 'flex flex-col' })
  for (const param of schema.parameters) {
    if (DRIVEN.has(param.name) || HIDDEN_GROUPS.has(param.group)) continue
    const field = element('sb-param')
    field.setAttribute('name', param.name)
    style.append(field)
  }

  const list = element('ul', { className: 'divide-y divide-line/60 text-[13px]' })
  const total = element('p', { className: 'px-3 py-2 text-[12px] text-muted' })

  root.append(
    element(
      'div',
      { className: 'grid h-full min-h-0 grid-cols-[minmax(260px,340px)_minmax(0,1fr)]' },
      element('div', { className: 'min-h-0 overflow-y-auto border-r border-line' }, form, style),
      element(
        'div',
        { className: 'grid min-h-0 grid-rows-[minmax(0,1fr)_auto]' },
        element('sb-preview'),
        element('div', { className: 'max-h-[40vh] overflow-y-auto border-t border-line' }, total, list, element('sb-generate')),
      ),
    ),
  )

  function draw(inputs) {
    const house = clampHouse(inputs.house ?? DEFAULT_HOUSE)
    for (const [key, input] of Object.entries(numbers)) input.value = String(house[key])
    const pieces = housePieces(house)
    total.textContent = `${pieces.reduce((sum, entry) => sum + entry.count, 0)} pieces; Generate keeps the one shown.`
    list.replaceChildren(
      ...pieces.map((entry) => {
        const current = inputs.params?.piece === entry.piece && (entry.course === null || inputs.params?.course === entry.course)
        const show = element('button', { type: 'button', textContent: current ? 'Showing' : 'Show' })
        show.dataset.piece = entry.piece
        show.disabled = current
        show.addEventListener('click', () => host.inputs.set({ params: pieceParams(entry) }))
        return element('li', { className: 'flex items-center justify-between gap-2 px-3 py-1.5' }, `${entry.count} × ${entry.label}`, show)
      }),
    )
  }

  draw(host.inputs.get())
  const unsubscribe = host.inputs.subscribe(draw)
  host.describe?.(() => {
    const inputs = host.inputs.get()
    const house = clampHouse(inputs.house ?? DEFAULT_HOUSE)
    const count = housePieces(house).reduce((sum, entry) => sum + entry.count, 0)
    return `${house.cols}×${house.rows}-module house, ${house.storeys} storey(s), ${count} pieces; showing ${inputs.params?.piece ?? 'the default piece'}.`
  })
  return () => {
    unsubscribe()
    root.replaceChildren()
  }
}
```

`models/dollhouse-kit/model.json`: add `"ui": {"module": "ui/index.js", "slot": "page", "api": 1}`. `README.md`: add a paragraph that describes the house designer (it counts the pieces for a box house and previews them one at a time, and Generate keeps the piece shown). Phase 4 will generate the whole house at once.

- [ ] **Step 5: Run it**

Run: `cd frontend && pnpm exec vitest run src/template-ui/dollhouse.test.ts`
Expected: PASS
Run: `cd backend && uv run --frozen pytest tests/test_bundled_ui.py -q`
Expected: PASS, one case per bundled `ui` (`dollhouse-kit` among them)

- [ ] **Step 6: Checks**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test`
Expected: PASS. `models/dollhouse-kit/verify.sh` is unchanged and runs in the `models` CI job because the directory changed.

- [ ] **Step 7: Commit**

```bash
git add models/dollhouse-kit/ui models/dollhouse-kit/model.json models/dollhouse-kit/README.md frontend/src/template-ui/dollhouse.test.ts
git commit -m "feat(models): dollhouse-kit house designer, one piece per Generate (#425)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: The agent writes inputs; authoring skill and docs

**Files:**
- Modify: `agent/src/tools/customizer.ts` (`render_model` takes `inputs`), `agent/test/tools.test.ts`
- Modify: `plugins/scadbuddy/skills/authoring/SKILL.md` (a "Template UI" section), `CLAUDE.md` (layout lines)

**Interfaces:**
- Consumes: `RenderRequest.inputs` (Task 1); `CreateOutputRequest.inputs` (Task 3); `ui` (Task 4).
- Produces: `render_model` input `inputs?: Record<string, unknown>`. When given, its `params` are validated and the body is `{ inputs, version }`. Otherwise the tool behaves as before.

- [ ] **Step 1: Write the failing test**

Append to the `describe('render_model', …)` block in `agent/test/tools.test.ts`:

```ts
  it('renders template inputs and validates their params', async () => {
    let body: unknown
    server.use(
      http.get(`${BACKEND}/api/v1/models/box/schema`, () => HttpResponse.json(SCHEMA)),
      http.post(`${BACKEND}/api/v1/models/box/render`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ job_id: 'j', status_url: '' }, { status: 202 })
      }),
      http.get(`${BACKEND}/api/v1/jobs/j`, () => HttpResponse.json({ id: 'j', slug: 'box', created_at: '', status: 'done' })),
    )
    const inputs = { params: { width: 5 }, house: { storeys: 2 } }
    await runTool(tool('render_model'), { slug: 'box', inputs }, ctx())
    expect(body).toEqual({ inputs, version: null })
    const refused = await runTool(tool('render_model'), { slug: 'box', inputs: { params: { width: 0 } } }, ctx())
    expect(refused.isError).toBe(true)
  })
```

(`width: 5` must be valid for the file's `SCHEMA`, and `width: 0` invalid, as the existing "refuses invalid parameters" case shows.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd agent && pnpm gen:api && pnpm exec vitest run test/tools.test.ts -t "template inputs"`
Expected: FAIL. The body is `{ params: {}, version: null }`.

- [ ] **Step 3: Implement**

In `agent/src/tools/customizer.ts`, `render_model`'s input gains

```ts
      inputs: z
        .record(z.string(), z.unknown())
        .optional()
        .describe(
          "Template inputs (a template with its own UI keeps state beside `params`); when given, `params` is ignored and inputs.params is what renders",
        ),
```

and the handler:

```ts
    handler: async ({ slug, params, inputs, version, save_output, output_name }, ctx) => {
      const rendered = (inputs?.['params'] ?? params) as typeof params
      const report = validateParams(await fetchSchema(ctx, slug, version), rendered)
      // …unchanged refusal…
      const accepted = await ok(
        ctx.backend.POST('/api/v1/models/{slug}/render', {
          params: { path: { slug } },
          body: inputs ? { inputs, version: version ?? null } : { params, version: version ?? null },
          signal: ctx.signal,
        }),
        `render ${slug}`,
      )
```

The description gains the sentence: "For a template with its own UI (`ui` in get_model), pass `inputs` to keep its state; the output records them."

- [ ] **Step 4: Authoring skill**

In `plugins/scadbuddy/skills/authoring/SKILL.md`, add a row to the files table:

```markdown
| `ui/index.js` | Optional. The template's own customizer (`"ui": {"module": "ui/index.js", "slot": "panel" \| "page", "api": 1}` in `model.json`). Plain ES module, no build step, exporting `mount(root, host, ctx)`; see "Template UI" below (template-pipelines spec `docs/superpowers/specs/2026-09-27-template-pipelines-design.md` §4.1). |
```

and a section:

```markdown
## Template UI

Source: `docs/superpowers/specs/2026-09-27-template-pipelines-design.md` §4 and §8.1; worked examples `models/maze-puzzle/ui/index.js` (panel) and `models/dollhouse-kit/ui/index.js` (page).

- `mount(root, host, ctx)` receives an open `ShadowRoot`, the `Host` v1 object and `{slot, version, theme, api}`. It may be `async`. Return a cleanup function. If `mount` throws or rejects, or `api` is a major the host does not support, the page shows the generated form with a banner.
- State is `host.inputs`: `inputs.params` is what renders (`model.scad`'s parameters). Every other key is the UI's own state and is saved with presets and outputs. `host.inputs.set(patch)` is a JSON merge patch (`null` deletes a key). A parameter `model.scad` does not have throws.
- Widgets: `<sb-param name="lid_color">` (optionally `bind="style.exterior"`), `<sb-preview>` (page slot only), `<sb-generate>`. Leave their children empty; the host renders into them.
- Only files under `ui/` are served, and only `.js .mjs .css .json .svg .png .jpg .jpeg .webp .woff2`. Import siblings relatively (`./pieces.js`). The page's Content-Security-Policy (`backend/scadbuddy/api/static.py` `PAGE_CSP`) loads script only from ScadBuddy and keeps fetch/XHR and subresource requests there, apart from Google Fonts style and font files. So: no CDN imports.
- Template code is not sandboxed (§9). It runs in the page with the user's session and can call every ScadBuddy API. The CSP does not stop navigation, `window.open` or WebRTC. Review a template's `ui/` as you would any code you run.
```

- [ ] **Step 5: CLAUDE.md**

Under "Layout", after the `frontend/src/` line, add:

```markdown
- `frontend/src/template-ui/` — template-owned UIs (#425): `host.ts` (Host API v1 over the page's
  inputs), `TemplateUi.tsx` (loads `ui/<module>` with `import()`, mounts into a shadow root, falls
  back to the generated form), `elements.ts` (`sb-param`/`sb-preview`/`sb-generate`, rendered by
  portal). Inputs are `{params, v, …ui state}` (`backend/scadbuddy/render/inputs.py`);
  `backend/scadbuddy/api/template_ui.py` serves `ui/**` live and at `/versions/{commit}/`;
  `api/static.py` `PAGE_CSP` (mirrored in `frontend/page-csp.txt`) is the page's policy.
```

- [ ] **Step 6: Checks**

Run: `cd agent && pnpm lint && pnpm typecheck && pnpm test && pnpm build`
Expected: PASS
Run: `.github/scripts/lint-plugin.sh`
Expected: exit 0 (the section cites the spec with `§`).

- [ ] **Step 7: Commit**

```bash
git add agent/src/tools/customizer.ts agent/test/tools.test.ts plugins/scadbuddy/skills/authoring/SKILL.md CLAUDE.md
git commit -m "feat(agent): render_model takes template inputs; authoring skill documents ui (#425)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Disagreements between the spec and the code (resolved here)

1. **UI file URL.** §4.1/§10 say `GET /models/{slug}/ui/{path}?version=<revision>`. The code's convention for revision-pinned reads is a path segment (`/models/{slug}/versions/{commit}/schema`, `api/versions.py`). A query string also drops off a module's relative imports (`./pieces.js` resolves without it), so a pinned module would import live siblings. The plan uses `/models/{slug}/ui/{path}` (live, `no-cache` + ETag) and `/models/{slug}/versions/{commit}/ui/{path}` (immutable at an exported revision, live at the current one).
2. **`<sb-param>` default binding.** §4.3 says it binds to `inputs[name]`, but the same section and §10 put parameter values under `inputs.params`, and phase 1 stores `{"params": …}`. The default bind is `params.<name>`.
3. **`host.generate()`.** §4.3 gives `Promise<{ jobId }>`, assuming that Generate starts the render. In the code the preview render is automatic, and Generate saves the finished render as an output (`ActionBar` → `api.createOutput`). `generate()` waits for the render of the current inputs, saves it, and resolves `{ jobId, outputId }`: `openPrint` needs the output id.
4. **Custom-element rendering.** §4.4 says "`react-dom/client` roots per element". The plan uses portals from the page's one React tree: the preview's capture ref, the fonts list and agent highlighting live in page context, and a second `<Preview>` root would be a second WebGL context.
5. **`require_valid_params`.** §4.4 moves it into `RenderPiece`. Phase 2 has only the default pipeline, so the API still validates `inputs.params` against `model.scad`. The move belongs to phase 4 (#427).
6. **Preset storage.** §10 speaks of preset "files". Saved presets are Postgres rows (`saved_presets`, #332), and a template's own presets are a list in `model.json`. The plan adds an `inputs` column with a backfill and keeps writing `params`. It also adds an optional `inputs` to `model.json` presets, and a params-only entry reads as `v: 0`.
7. **maze-puzzle.** §1 says "`parts` and `lid_color` are meaningless unless `mode = ball_lid`". `models/maze-puzzle/model.scad` has no `parts` parameter, so `lid_color` is the only lid-only option.
8. **URL-import confirmation (§9).** `library/url_import.py` imports a single `.scad`, and upload takes source, `model.json`, thumbnail and README, so no import path can bring `ui/` files in phase 2. A `ui` declared in an uploaded `model.json` without its module falls back with the banner. The confirmation belongs to whichever change makes imports multi-file.
9. **`host.schema(file)` and `GET /models/{slug}/files` (§4.3, §10).** No phase 2 consumer needs another file's schema or a file listing. v1's `schema()` accepts only `model.scad`. `?file=` and `/files` land with phase 4's multi-file pipelines as minor, non-breaking additions (§8.1).
10. **Page slot.** §4.1 says the page slot "gets the whole content area under the header". v1 has no element for Download/Send/Print, so the host's `ActionBar` stays as the page slot's footer.
11. **Job status.** §10 says `GET /jobs/{id}` is "unchanged shape" plus `steps`. The plan also adds `inputs`, so a client can see what a job was submitted with.
12. **CSP.** §9 decides that template code is not sandboxed and names no CSP. The brief asks for one, so the plan adds only two things. (a) The page policy stops cross-origin script loading and fetch/XHR/subresource beacons, except Google Fonts style and font files, which the font picker needs. (b) `default-src 'none'; sandbox` on served `ui/` files means none can run as a document. Neither stops same-origin API calls with the user's session (settings, printing, `/api/v1/ai/credentials`), reading the DOM through `root.host`, or exfiltration by navigation, `window.open` or WebRTC. There is no `frame-ancestors`, because Bambuddy's framing origin is not known.
13. **Phase 1 addendum's "phase 2 migration that drops `params`".** Phase 2 does not drop `render_jobs.params`. `JobStatus.inputs` falls back to `{"params": job.params}` for rows the legacy queue inserted with `inputs = '{}'`, so no second backfill is needed.
14. **The agent writes inputs (§4.3).** The service tool `render_model` writes full inputs (Task 11). The in-page browser agent reads them (`get_params.inputs`, Task 8) but writes only `params` through its existing tools. An in-page tool that writes UI state is left out, because no phase 2 template needs one.

15. **`createOutput` gains a trailing `index?` in phase 4.** Phase 4's pipelines can write several outputs per job; its Task 8 appends `index?: number` to this signature (sent in the body only when defined), so every phase 2 call stays as written here.

## Self-review notes

- **Spec coverage.** §4.1 declaration → Task 4 (`UiDeclaration`, served files). §4.2 mounting, shadow root, fallback banner, origin → Tasks 6 and 8. §4.3 Host v1 → Task 6 (all members), `describe` → Task 8 (agent `get_params`), custom elements → Task 7. Inputs as the one state: renders → Task 1, presets → Task 2, outputs and reopen → Tasks 3 and 5, the agent → Tasks 8 (browser tools) and 11 (service tool). §4.4 frontend shell → Tasks 5 and 8; `RenderRequest.inputs` → Task 1. §8.1 majors → `UI_API_SUPPORTED`, tested in Task 6. §9 containment as it concerns UI modules → Task 4 (serving, page CSP) and Task 8 (the e2e CSP check); URL-import confirmation deferred with its reason (Disagreement 8). §10 → Tasks 1–4; `/files`, `?file=`, `/outputs/arrange`, Settings deferred with reasons. §11 item 2 first users → Tasks 9 and 10. §12: #325 presets first-class → Task 2; #252 agent → Task 11 (the authoring skill documents `ui`).
- **Placeholder scan.** No TBD/TODO. Where a step edits code the plan could not quote whole (the output-create route body, `PresetPicker` internals, the existing SQL tails), it names the exact line to change and gives the replacement.
- **Type consistency.** `normalize_inputs`/`legacy_inputs`/`InputsError` (Task 1) are used with the same signatures in Tasks 2 and 3. `JsonObject`, `InputsExtra`, `NO_EXTRA`, `splitInputs`, `joinInputs` (Task 5) are used in Tasks 6–8. `HostDeps` fields in Task 6 match the object Task 8 builds. `TemplateUi` props: Task 6 defines `slug, ui, version, deps, inputs, onFailure`, and Task 7 adds `elementContext`, which Task 8 passes. `ElementContext` keys match `HostElementContent`. `ActionBarHandle.openPrint` (Task 8) is called through `HostDeps.openPrint`. `api.uiFileUrl(slug, version, path)` has the same argument order in Tasks 5, 6 and the tests.
- **Declined (review M10).** An uploaded `model.json` with a malformed `ui` is written back without it (`Catalogue.create` dumps `ModelMeta`). Upload cannot carry `ui/` files in phase 2, so there is no module to lose, and the next upload that can carry `ui/` is where keeping the raw key belongs.
- **Review Focus.** All five pinned: (1) Task 4 `test_ui_paths_never_leave_ui`; (2) Task 4 `test_a_patch_keeps_ui`, `test_a_malformed_ui_costs_only_the_ui`; (3) Task 6 `ignores writes after dispose`, `refuses a parameter the schema lacks`; (4) Task 3 `test_an_output_from_before_inputs_reads_as_params_v0`, Task 5 `reopens an output with its UI state`; (5) Task 3 `test_an_output_records_the_inputs_it_was_saved_with`, `test_an_output_refuses_inputs_the_job_did_not_render`.
