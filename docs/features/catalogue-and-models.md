# Catalogue and models

## What it does

The Models page lists the bundled templates and your own. Add a model by uploading a
`.scad` file, pasting source or importing from a URL; duplicate one (a fork that can take
its upstream's later changes); edit its name, description, tags, README and pictures;
delete it (the source stays in git). User guide: [Catalogue](../user-guide.md#catalogue),
[Writing a model](../user-guide.md#writing-a-model),
[Deleting a model](../user-guide.md#deleting-a-model).

## How it works

Templates are folders in a git repository on the API's data volume. Bundled models
(`models/`) are copied into the image and mirrored under `_builtin/` as one commit at
every start; each catalogue write is one commit. The API process alone reads and writes
the repository. Longer writes (duplicate, mesh-model creation) run as `library`
operations ([workflows.md](../architecture/workflows.md#operations-one-shape-for-writes)).

Each template also has a derived row of `templates` in Postgres (#7317). Once a copy run
has finished, the list, a template's record, its id list and the other readers (the pin
index, which is the libraries the templates pin, the library associations, the print
settings, the name check) read the rows; before that, they read the files. The list of a
library's users (`GET /api/v1/libraries/{name}/users`) and the check before a library
checkout's removal always read the files (spec §7.4). The catalogue thumbnail
(`GET /api/v1/models/{slug}/thumbnail`) is named from the row too: its `ETag` is the
template's own cover's sha256 (the same value as before the copy), `"output-<id>"` for an
output's plate cover or `"preview-<id>"` for the default-render preview, so a matching
`If-None-Match` answers 304 with no file read (except for a row that lags its media, which
is read from the files); a 200 carries the tag of the image it read.
Every catalogue write refreshes its templates' rows under the models write
lock: a commit writes them in the transaction that records the models HEAD, and a write
that commits nothing writes them in a transaction of its own. A failed refresh never fails
the write; it is logged and counted, and the copy repairs it. The copy runs at each API
start and in each housekeeping sweep: it adds, refreshes and drops rows to match the
folders, a few templates per hold of the write lock. `GET /api/v1/model-records/copy-status`
answers its last run. The details are in
[data.md](../architecture/data.md#template-records).

```mermaid
sequenceDiagram
    participant UI as Models page
    participant API
    participant Git as models git repo
    participant PG as Postgres templates
    UI->>API: GET /api/v1/models?q=…
    API->>PG: read the rows (after a copy run; before it, model.json from Git)
    UI->>API: POST /api/v1/models (upload / paste)
    API->>API: openscad parse check
    API->>Git: commit
```

## Code

- Page: `frontend/src/pages/CataloguePage.tsx` (Paste source, Import from URL, Add model at
  `:192-201`).
- Routes: `backend/scadbuddy/api/models.py` (search and filters from `:313`, duplicate at
  `:1395`), `backend/scadbuddy/api/model_files.py`, `backend/scadbuddy/api/media.py`,
  `backend/scadbuddy/api/readme_images.py`, `backend/scadbuddy/api/upstream.py:82`
  (upstream status, merge, dismiss, detach).
- Catalogue and history: `backend/scadbuddy/library/catalogue.py` (`duplicate` at `:1262`,
  built-in sync at `:2940-2956`), `backend/scadbuddy/library/history.py:1-13`,
  `backend/scadbuddy/library/upstream.py:1-11` (three-way merge with `git merge-file`).
- Template rows (#7317): `backend/scadbuddy/library/template_rows.py` (the rows and the
  copy runs), `template_refresh.py` (the writers' refresh), `template_copy.py` (the copy),
  the route `template_copy_status` in `backend/scadbuddy/api/models.py`.
- Bundled models: `models/<slug>/` with `model.scad`, `model.json`, `README.md`,
  `thumbnail.png` and `verify.sh` ([`models/CLAUDE.md`](../../models/CLAUDE.md)).

## Data

- The models git repository under `SCADBUDDY_DATA_DIR`; a model's metadata is its
  `model.json` (`backend/scadbuddy/core/paths.py:11`).
- Tables: `template_media`, `model_previews`, `models_repo_head`, `templates`,
  `template_copy_runs` ([data.md](../architecture/data.md#ownership-by-domain)).
- Blob keys: `src-<slug>-<revision>` snapshots for render workers, `asset-<id>` uploads.

## Tests

- Backend: `backend/tests/test_history.py`, `backend/tests/api/test_duplicate.py`,
  `backend/tests/api/test_upstream.py`; the template rows in `backend/tests/`: `test_template_rows.py`,
  `test_template_refresh.py` (one case per writer), `test_template_copy.py` and
  `tests/api/test_template_copy_status.py`.
- e2e: `frontend/e2e/catalogue-cards.spec.ts`, `catalogue-filters.spec.ts`,
  `catalogue-list.spec.ts`, `import-dialog.spec.ts`, `paste-source.spec.ts`,
  `model-details.spec.ts`, `duplicate-template.spec.ts`, `upstream-update.spec.ts`,
  `template-gallery.spec.ts`, `media-manager.spec.ts`.
- Bundled models: each `verify.sh`, run by the `models` CI job.

## Limits and known issues

- A duplicate always tracks its upstream; a clone without that link is
  [#3187](https://github.com/eh-homelab/ScadBuddy/issues/3187).
- The Models page's search folds case and accents in process; there is no database index
  (`backend/scadbuddy/api/models.py:313-367`).
