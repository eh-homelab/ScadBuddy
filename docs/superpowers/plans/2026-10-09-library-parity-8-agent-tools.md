# Library parity 8: agent tools for library prints (#1756, #1967)

Epic #1749, rows A1-A3. Owner rule: a library-file print is an ordinary print; only the
3MF source differs. So the agent gets no second set of print tools: every print tool
that names an output also takes a library file.

## Shape

- `agent/src/tools/print.ts`: a `source` pair, `output_id` | `library_file_id`, exactly
  one (a zod refine on the input object, so a call naming neither or both is refused
  at parse time, before an outward call reaches the approval gate). The route is the
  only per-kind difference:
  - `get_print_choices`, `get_print_filaments`, `get_print_progress`, `print_output`
    (keeps `print_sequence`, #907), `file_output_under_project`: `/print/outputs/{id}/…`
    or `/print/library/{file_id}/…`.
  - `remember_model_print_choices`: `slug` | `library_file_id`
    (`PUT /print/library/{file_id}/choices`).
  - New read tool `list_library` (`GET /print/library`, `folder_id`, `all`).
- `agent/src/tools/outputs.ts`: `get_output_plates`, `get_output_preview` (with
  `plate`) and `download_3mf` take `library_file_id` too (`/print/library/{id}/plates`,
  `/preview.glb`, `/file`).
- `agent/src/tools/coverage.ts`: `LIBRARY_PRINT_LATER` goes; the routes above leave
  `NOT_A_TOOL`. The checks, thumbnails and objects stay, with reasons that no longer
  say "later".
- `agent/src/resources/catalog.ts` + `events.ts` (#1967): a resource
  `scadbuddy://print/library/{file_id}/progress` (backed by `get_print_progress`), and
  a `print.*` event whose `output_id` is `library:<id>` announces that URI, never
  `scadbuddy://print/outputs/library%3A<id>/progress`.
- `agent/src/sessions/touched.ts`: one-line change so a library print records
  `library:<id>` as what was printed (minimal; that directory is another agent's).
- `plugins/scadbuddy/skills/print/SKILL.md`: drop "no agent tool yet".
- `backend/scadbuddy/api/agent_actor.py`: `PUT /print/library/{file_id}/choices` joins
  `AGENT_ALLOWED_WRITES` (a write-tier route now, as the model one is;
  `test/agentActor.test.ts` derives the list from the registry).
- The three output read tools that now reach Bambuddy for a library file declare
  `Manage Library` (`test/projections.test.ts`).

#1967's frontend item (hash-linked Waiting row) already landed in #1966 (d6c4b13a0,
`PrintsPage.test.tsx` "takes a print found by its hash…"); nothing to do there.

## Order (test-first)

1. `test/libraryPrint.test.ts`: each tool hits the library route for
   `library_file_id`; neither/both is refused; `print_output` fills choices from the
   library choices read and sends `print_sequence`; `list_library` query.
2. `test/resources.test.ts`: library event → library URI; resource resolves.
3. Implement; `pnpm lint && pnpm typecheck && pnpm test && pnpm build`.
