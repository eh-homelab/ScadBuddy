# Library parity 6: print options and remembered choices by subject (#1754)

> For agentic workers: implemented inline (superpowers:executing-plans), test-first.

**Goal:** a library-file print remembers its options and choices exactly as an output's
does, in the same store, and a reprint of it applies them. Closes #1749 rows B6, C1, F3, H6.

**Architecture:** one *options scope key* names what a print's per-subject layer is
remembered under: an output's model slug (outputs of one model share it, as today), or a
library file's subject key `library:<file id>` (`PrintSubject.options_scope`). Model slugs
never contain `:` (`MODEL_ID_PATTERN`), so the two never collide. Both the per-subject
print options (`model_print_options`, the `model` scope of `PUT /settings/print-options`)
and the remembered choices (`model_print_choices`) are keyed by it. `library_print_choices`
is copied into `model_print_choices` by a new migration and no longer read or written.

**Tech stack:** FastAPI, psycopg, Postgres migrations; React 19 + vitest + msw.

## Global constraints

- Backend schema changes are new files `backend/scadbuddy/migrations/<UTC>_<slug>.sql`.
- The migration is idempotent (re-running it changes nothing) and loses no row: every
  `library_print_choices` row lands as `model_print_choices('library:<id>')`; on a key
  already present the newer `updated_at` wins.
- `library_print_choices` is kept (not dropped) so a rolled-back image still starts; a
  later migration may drop it.
- No new `/api/v1` operation; routes keep their paths and shapes.

## Review focus

1. A remembered-choices row for a library file in Settings: labelled as a library file
   and forgotten through `PUT /print/library/{id}/choices`, never the slug route (which
   422s on `library:12`).
2. `PUT /settings/print-options` with `scope=model` and a key that is neither a slug nor
   `library:<digits>` is a 422, not stored under a key nothing reads.
3. A reprint operation checked before the upgrade carries `slug` only; its run still
   resolves options from it.
4. A library row the current `ModelPrintChoices` cannot read must not stop the settings
   loading (the old per-file read tolerated it): the migration copies rows verbatim, so
   it is the same risk as a model row, no worse.
5. The dialog's "This file" scope survives switching from an output to a library file
   (the disclosure re-reads when the scope key changes).

---

### Task 1: Migration and store (C1)

**Files:** new `backend/scadbuddy/migrations/20261009T…Z_print_choices_by_subject.sql`;
`library/settings_store.py` (`library_choices`/`set_library_choices` go through
`model_print_choices` under `library:<id>`; `forget_remembered` no longer touches the old
table); `bambuddy/subject.py` (`options_scope(subject, slug)` helper,
`PrintSubject.options_scope`); tests in `tests/test_settings_store.py` (or the store's
pg test module) and a migration test.

- [ ] Failing tests: migration copies every row, twice-applied is identical, a newer
      existing row is kept; `set_library_choices(7, …)` shows in
      `load().model_print_choices["library:7"]`.
- [ ] Implement; run focused tests against pg on :55466.
- [ ] Commit.

### Task 2: Options scope through the run, check, reprint (B6, H6)

**Files:** `bambuddy/print_source.py` (`options_slug` -> `options_scope`; LibrarySource
returns `library:<id>`), `bambuddy/print_run.py`, `bambuddy/send.py`
(`resolve_print_options(settings, scope_key, …)`), `bambuddy/operations.py`
(`reprint_check` returns `options_scope`, run reads it or legacy `slug`),
`api/settings.py` (`PrintOptionsUpdate` validates a model key), `api/library_print.py`,
`api/analyzers.py`, `bambuddy/choices.py`.

- [ ] Failing tests: a library run resolves `model_print_options["library:<id>"]`; a
      reprint of a library archive applies them; legacy checked `{"slug": …}` still works;
      bad model key 422.
- [ ] Implement, run, commit.

### Task 3: Frontend (F3) and agent docs

**Files:** `frontend/src/lib/printSource.ts` (`optionsScope(source)`),
`components/PrintOptionsDisclosure.tsx` (`scope={{key,label}}` replaces `slug`; "This
file" for a library file), `components/PrintPicker.tsx`, `lib/printOptions.ts` labels,
`pages/settings/RememberedChoicesPanel.tsx` (library rows), `mocks/handlers.ts` (library
choices in `modelChoices` under `library:<id>`); agent `tools/settings.ts` description and
`sessions/touched.ts` (`model` null for a library key).

- [ ] Failing vitest: the library dialog offers "This file" and saves under
      `library:<id>`; Settings lists and forgets a library row.
- [ ] Implement; lint, typecheck, focused vitest; agent lint/typecheck/focused test.
- [ ] Commit; PR.
