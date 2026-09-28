# Print Workflow Foundations (#316, #317, #322) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A project's Bambuddy folder keeps every file it used, including the editable 3MF placed there on Generate. Every runtime setting becomes editable in the UI, and the page shows where each value came from.

**Architecture:**
- The single `library_file_id` slot on an output becomes a list of copies keyed by (folder, target). Only copies in the inbox folder are ever deleted, and nothing is ever moved.
- A small route files the Generate output into the active project.
- Settings: every runtime field joins the env-seeded store pattern. The API reports each field's source and accepts a per-field reset.

**Tech Stack:** FastAPI/pydantic, httpx + respx, React 19, msw, vitest, Playwright.

**Spec:** issues #316, #317 and #322 (bodies and comments); the print-flow spec `docs/superpowers/specs/2026-09-24-print-flow-design.md` §7.

## Global Constraints

- The CI commands from CLAUDE.md are green locally before every push. Regenerate `openapi.json`, then `schema.d.ts`, then msw, in that order.
- Bambuddy stays the source of truth. ScadBuddy stores ids, never copies of Bambuddy data.
- Old records keep working: an `outputs/<id>/meta.json` or `settings.json` written before this change must load and behave sensibly.
- The API key and the Google Fonts key are write-only: never returned, only "set" or "not set".
- The PR title is a conventional commit, the body says `Fixes #N`, and it carries the session attribution. No force-push, no amend.

## Review Focus

- **A legacy output** (`library_file_id` + `library_file_plate`, no folder known) sent to a project → the legacy copy is not moved. Its folder is read once from Bambuddy (`GET /library/files/{id}`) and recorded, and then the normal rules apply. Test in #316.
- **A cached copy deleted in Bambuddy (404)** → dropped from the list and re-uploaded. This must not fail the send. Test in #316.
- **The same project chosen twice on Generate** → no second upload. The route is idempotent per (folder, target). Test in #317.
- **An env var added to the deployment after the user saved other settings** → still honoured for fields the user never set, i.e. the existing `ENV_SEEDED` semantics extended to every field. Test in #322.
- **"Reset to deployment value" on a field the user had cleared** → removed from `cleared`, and the env value is back. Test in #322.

---

## Shared contracts

### #316: library copies (`backend/scadbuddy/bambuddy/uploads.py`, Postgres since #455)

```python
class SlicedCopy(BaseModel):
    id: int                       # Bambuddy library file id of the sliced 3MF
    preset_key: str | None = None # f"{pipeline_id}" or the preset triple, whichever the route used

class LibraryCopy(BaseModel):
    id: int                       # Bambuddy library file id of the unsliced 3MF
    folder_id: int | None         # None = library root
    target_key: str               # Target.key (plate[@nozzle])
    sliced: list[SlicedCopy] = Field(default_factory=list)

# Stored by BambuddyUploadStore in output_bambuddy_uploads / output_bambuddy_slices
# (render/pg_store.py migration 3), not in meta.json. The API's OutputDetail fills
# library_files from it. No data migration (#455): library_file_id /
# library_file_plate / library_files in an old meta.json are ignored, so that output's
# next send uploads afresh.
```

In `backend/scadbuddy/bambuddy/send.py`:

```python
async def ensure_uploaded(client, store, uploads, meta, settings, *, target=None, folder_id=None) -> int
# folder_id None means the inbox (settings.library_folder_id).
# Rule: reuse the copy with (folder, target) equal; else upload a new copy there.
# Delete only superseded copies whose folder is the inbox. Never call move_library_files.
def is_inbox(folder_id: int | None, settings: StoredSettings) -> bool
def record_sliced(store, output_id, library_file_id, sliced: SlicedCopy) -> OutputMeta
```

- Response models that report `library_file_id` keep the field: it is the copy used by this send.
- The output delete route lists the copies. The UI offers "also delete the inbox copies in Bambuddy" and leaves project copies alone.

### #317: project file (depends on #316)

- The route `POST /api/v1/outputs/{id}/project-file`, body `{"project_id": int}`, returns `{"project_id", "folder_id", "library_file_id", "created": bool}`.
- It resolves the folder via `folder_for(client, project_id)`, creating the project folder the way `ensure_project` does if it is missing. The target comes from `StoredSettings.project_pipelines[str(project_id)]`, then `pipeline_for(slug)`, then `default_plate`, then the fallback. It calls `ensure_uploaded(..., folder_id=)` and then `attach_edit_link`.
- A new `StoredSettings.project_pipelines: dict[str, int]` is written by `run_for_output` whenever a print runs with a project.
- New in `backend/scadbuddy/bambuddy/projects.py`: `async def media_folder_for(client, project_id: int) -> int`. It returns the id of the `Media` subfolder (`parent_id` = the project folder, `project_id` set), creating it if missing. It is not used by #317's UI; it exists for #309.
- Frontend:
  - the Customize page gets a `ProjectPicker` (reuse the component; `No project` option; default `last_project_id`);
  - after Generate succeeds with a project, call the route and show "Saved to <project>" with an "Open in Bambuddy" link;
  - the choice updates `last_project_id` through the existing settings route.

### #322: settings (lands after #274, which adds `media_upload_max_bytes`)

In `backend/scadbuddy/core/settings.py`:

```python
BOOTSTRAP_FIELDS: frozenset[str] = frozenset({"data_dir", "seed_models_dir", "frontend_dir",
    "openscad", "openscad_lsp", "revision", "version"})   # plus the database URL if #241 adds one
```

In `backend/scadbuddy/library/settings_store.py`:
- `ENV_SEEDED` becomes every `Settings` field not in `BOOTSTRAP_FIELDS`.
- `_from_env` copies them all.
- `StoredSettings` and `SettingsPatch` gain the fields, with bounds validation.

```python
SettingSource = Literal["stored", "env", "default", "cleared"]
def sources(self) -> dict[str, SettingSource]
# SettingsPatch.reset: list[str] | None — drop the stored value and remove it from `cleared`.
```

- The API: `GET /settings` gains `sources: dict[str, SettingSource]`, `restart_required: list[str]` (fields whose saved value differs from the running value and cannot apply live) and `bootstrap: dict[str, str]` (read-only values for About).
- Consumers must read runtime values from the store at use time, not from the boot `Config`, for the fields marked live in #322. The render queue concurrency resizes live if feasible; otherwise it goes in `restart_required`.
- The CI guard in `backend/tests/test_settings_coverage.py` asserts `set(Settings.model_fields) == set(ENV_SEEDED) | BOOTSTRAP_FIELDS`.
- Frontend (`SettingsPage.tsx`):
  - sections with sticky nav and per-section save or discard, with an in-app unsaved guard;
  - a source badge per field, plus reset;
  - a Remembered choices table using the existing one-key routes, with a new `DELETE` per map entry where there is no route yet;
  - an About section;
  - Uploads, Rendering, Fonts and Diagnostics sections;
  - the connection test lists each scope.
- Bambuddy's finish-photo status is read-only. If the Bambuddy settings route needs a scope the key lacks, show "unknown" rather than failing.

## Tasks

| Task | Issue | Branch | Starts when |
|---|---|---|---|
| 1 | #316 | `feat/316-library-copies` | now |
| 2 | #317 | `feat/317-project-file` | #316 merged |
| 3 | #322 | `feat/322-settings-ux` | #274 merged |

Each task is TDD from the issue's "Done when" list plus the Review Focus lines assigned to it. Each ends with the full CI set green, a push, and a PR.
