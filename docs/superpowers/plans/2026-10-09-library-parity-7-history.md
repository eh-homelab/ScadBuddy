# Library parity 7: history parity for library prints — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A library-file print's history behaves as an output's: Bambuddy reprints of its slice are linked by hash (H3), the history filters to one library file (H4), a library print is labelled by its file's name (F9), and the print span says what kind of thing was printed without recording anything of Bambuddy's (F5, #1231).

**Architecture:** `linking.link_by_hash` is generalised to a `PrintSubject` (`link_subject_by_hash`); a library print's slices are already recorded under `library:<id>` in `output_bambuddy_slices` since #1882, so the same scan works. It runs for a library file when one of its queue items is gone (as an output's does) and when the history is filtered to that file, throttled per subject. `PrintLinkStore.page` takes subjects instead of output ids. The list gains `library_file_id`, and every summary `library_file_name` (read through `ArchiveCache`, 30 s).

**Tech Stack:** FastAPI, psycopg, respx; React 19, vitest; the agent's zod tools.

**Spec:** epic #1749 (owner rule and inventory), `docs/superpowers/specs/2026-10-01-distributed-tracing-design.md` §6.

## Global Constraints

- A library-file print differs from any other print only in how its 3MF is obtained (#1749).
- Tracing: never record a parameter value or anything Bambuddy returns (spec §6).
- Every `/api/v1` operation needs an agent tool or a `coverage.ts` entry.
- Generated API files are never committed.

## Review Focus

- `slug` and `library_file_id` both given: 422 rather than an empty page that looks like "no prints".
- A library file deleted in Bambuddy: its prints still list, labelled generically (name `None`), never a 500.
- A Bambuddy read of the file that fails other than 404 must not fail the list.
- A hash scan for a library file with no recorded slices reads no archives.
- Back/forward and "Clear filters" drop the file filter like any other (URL `file=`).

---

### Task 1: hash-link library slices (H3)

**Files:** `backend/scadbuddy/bambuddy/linking.py`, `backend/tests/bambuddy/test_print_linking.py`

- [ ] Test: a library subject whose recorded slice hash matches an archive in the window gets a `content_hash` link under `library:<id>`; no slices, no archive read.
- [ ] Test: `link_library_prints` on a 404 item scans that file by hash (throttled once per `HASH_SCAN_INTERVAL`).
- [ ] Implement `link_subject_by_hash(client, uploads, links, subject, *, made_at=None)`; `link_by_hash(meta)` wraps it; `scan_library_by_hash(...)` with a per-subject throttle.
- [ ] Commit `feat(print): hash-link a library file's reprints made inside Bambuddy (#1755)`.

### Task 2: per-file filter and file name (H4, F9)

**Files:** `backend/scadbuddy/bambuddy/print_links.py` (`page(subjects=)`), `backend/scadbuddy/bambuddy/archive_cache.py` (`library_file`), `backend/scadbuddy/api/print_history.py`, `backend/tests/api/test_print_history.py`, `agent/src/tools/prints.ts`

- [ ] Tests: `?library_file_id=89` lists only that file's prints and scans it by hash on the first page; with `slug` too, 422; summaries carry `library_file_name` (None for an output's and for a deleted file); `q` matches the file name.
- [ ] Implement; agent `list_prints` gains `library_file_id`.
- [ ] Commit `feat(print): filter the history by library file; name a library print by its file (#1755)`.

### Task 3: frontend filter, label and span attribute (H4, F9, F5)

**Files:** `frontend/src/lib/printsQuery.ts`, `components/prints/PrintFilterBar.tsx`, `PrintHistory.tsx`, `PrintItem.tsx`, `src/mocks/...`, `lib/useRunPrint.ts`, tests; spec §6.

- [ ] Tests: `file=` parses/serialises and becomes `library_file_id`; a library print's label is its file name and links to `/prints?file=<id>`; the filter shows as a removable chip; the print span records `scadbuddy.print_source` (`output`|`library`) and no library id.
- [ ] Implement; amend §6 to allow the source kind and `all_plates` (ScadBuddy's own values).
- [ ] Commit `feat(prints): per-file history filter and file-name label; print span names its source kind (#1755, #1231)`.
