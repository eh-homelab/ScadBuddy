# Send Bar Library-Only Implementation Plan (#312)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the send bar (ActionBar → SendDialog → `POST /outputs/{id}/send`) a plain library upload, and remove every Bambuddy slicer-pipeline and raw-preset default from ScadBuddy, so the Print dialog is the only way to print.

**Architecture:** The send route keeps its path but only uploads, lays the 3MF out for the Settings printer, and attaches the edit link. Queue mode, copies, print options, pipeline runs, eligibility handling, the pipeline progress route, the Settings pipeline selector, the raw-preset settings fields and the Bambuddy client's pipeline surface are all deleted. Each removal is split so every commit is green: the **frontend stops reading a field first** (valid against both the old and new schema), and then the **backend removes it and regenerates** `openapi.json` plus both `schema.d.ts` files.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, httpx + respx, pytest, mypy strict, ruff (uv); React 19 + TypeScript + Vite, Vitest + msw, Playwright; the `agent/` package (pnpm); plugin lint script.

**Spec:** `/tmp/claude-1000/-home-elan-repos-eh-homelab-clusters/438289e2-cd70-4e64-bf80-829e85ea4f34/scratchpad/312-design.md` (approved 2026-09-28). Background: `docs/superpowers/specs/2026-09-27-spool-first-print-design.md` §0.

**Base:** branch from `main` after PR #335 (`feat/spool-first-print`) merges. Every path below is relative to the ScadBuddy repo root. Line numbers were read from the `spool-first-print` tree and are approximate after earlier tasks land; search for the quoted text.

## Global Constraints

- The send dialog has no queue mode, copies, print options, or pipeline/eligibility handling. It uploads, attaches the edit link, and links to the Bambuddy library file. The Print button stays the only way to print.
- No production code references pipelines: `grep -rni pipeline backend/scadbuddy frontend/src` finds nothing live (comments that explain history, recordings, generated files and the `pipelines.py` module name excepted; see Conflict 5 in the controller notes).
- The Settings page has no pipeline or raw-preset fields. An old `settings.json` still loads.
- An old output record (`meta.json`) that says `print_route: "pipeline"` still loads.
- Generated files are regenerated in the same commit as the backend change that alters them: `openapi.json`, `frontend/src/api/schema.d.ts`, `agent/src/api/schema.d.ts`.
- Keep the print-option scopes global → per-printer → per-model → per-request: the Print run (`run_for_output` in `backend/scadbuddy/bambuddy/pipelines.py`) uses all four. Remove only what is send-only.
- US spelling in new text ("color"); existing identifiers such as `filament_colours` keep their names.
- New commits only, never amend. `git add` with explicit paths only, never `-A` or `.`.
- Every commit message ends with:

  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_0159GufZGG4bJoXvBkEJ2S16
  ```

**Gates** (a task is done only when every gate its change can affect is green):

```bash
# backend
cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest
# regenerate (every backend task that touches a route, model or route docstring)
cd backend && uv run --frozen python -m scadbuddy.tools.export_openapi && cd ../frontend && pnpm gen:api && cd ../agent && pnpm gen:api
# frontend
cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test
# agent
cd agent && pnpm lint && pnpm typecheck && pnpm test
# plugin
.github/scripts/lint-plugin.sh
```

`respx.mock` used bare in these tests has `assert_all_mocked=True` and `assert_all_called=False`: an unmocked Bambuddy call fails the test, and an unused mock does not. So "no pipeline route was called" is enforced by simply not mocking one.

## Review Focus

1. **A stale browser tab or script posts `{"mode": "queue", "copies": 2}` to `/send`.** Expected: a 422, and nothing uploaded, not a silent library upload reported as success. Test: Task 2, `test_queue_mode_is_refused_and_nothing_is_uploaded`.
2. **An output whose last print was a pipeline run** (`meta.json` has `print_route: "pipeline"`, `pipeline_run_id`, and a leftover `queue_item_id` from an older print). Expected: the output still lists and opens, History shows it as in the library, and progress is `null`, not the older print's queue item. Tests: Task 4, `test_an_output_last_printed_by_a_pipeline_run_still_opens` and the three `progress_for` legacy tests.
3. **A `settings.json` written before #312** (holds `pipeline_id`, the four raw-preset keys and `model_pipelines`), and an old client that PUTs `pipeline_id`. Expected: it loads, GET /settings answers without those keys, the next write drops them, and the PUT is a 200, not a 422. Tests: Task 6, `test_a_settings_file_from_before_312_still_loads_and_sheds_the_old_keys` and `test_an_old_client_sending_a_pipeline_is_not_refused`.
4. **A configured `pipeline_id` survives in settings when Task 2 lands** (Task 6 has not run yet). Expected: the send never reads `/slicer-pipelines/`, and lays the file out for the Settings printer's plate. Test: Task 2, `test_a_stored_pipeline_is_never_read_by_the_send`.
5. **Bambuddy answers 409 on a call ScadBuddy still makes** (queue, slice, folders, projects). Expected: it stays a 409 with Bambuddy's body passed through, not a 502 "unavailable". Test: Task 7, `test_a_409_passes_bambuddys_body_through`.

---

### Task 1: Send dialog uploads to the library only (frontend first)

**Order:** frontend first. No schema change: `{ mode: 'library' }` is a valid `SendRequest` before and after Task 2.

**Files:**
- Modify: `frontend/src/components/SendDialog.tsx` (whole file)
- Modify: `frontend/src/pages/CustomizePage.test.tsx:193-468` (the send-dialog tests)
- Modify: `frontend/src/mocks/handlers.ts:1501-1535` (the `/outputs/:id/send` handler)
- Modify: `frontend/src/api/types.ts:151` (drop `SendMode`)
- Modify: `frontend/src/lib/problems.ts:3-36`, `frontend/src/lib/problems.test.ts:2-30` (drop the eligibility helpers)
- Modify: `frontend/src/lib/printOptions.ts:129-133` (drop `quantityBounds`, the send bar's only helper)

**Interfaces:**
- Consumes: `api.sendOutput(id: string, body: SendRequest): Promise<SendResult>` (unchanged signature).
- Produces: `SendDialog` props unchanged (`open`, `output`, `onClose`, `onSent(result: SendResult)`). It never reads `result.mode`, `pipeline_run_id`, `queue_item_id` or `options`, so Task 2 can delete them.

- [ ] **Step 1: Write the failing tests**

In `frontend/src/pages/CustomizePage.test.tsx`, replace the test `'sends a generated output and links to the Bambuddy queue'` with:

```tsx
  it('sends a generated output to the library and links to it', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('/api/v1/outputs/:id/send', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({
          library_file_id: 41,
          filename: 'name-keychain-reagan.3mf',
          bambuddy_url: 'https://bambuddy.test/library',
          edit_url: 'https://scad.test/edit/x',
        })
      }),
    )
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Send to Bambuddy' }))
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    // #312: the send bar only uploads. Queueing is the Print dialog's job.
    expect(within(dialog).queryByRole('radio')).not.toBeInTheDocument()
    expect(within(dialog).queryByLabelText('Copies')).not.toBeInTheDocument()
    expect(within(dialog).queryByText('Options')).not.toBeInTheDocument()
    expect(within(dialog).getByText(/use Print/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(within(dialog).getByText(/Added to the library/)).toBeInTheDocument())
    expect(bodies).toEqual([{ mode: 'library' }])
    expect(within(dialog).getByRole('button', { name: 'Open in library' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Open in queue' })).not.toBeInTheDocument()
    expect(within(dialog).getByText(/Bambuddy has the link back/)).toBeInTheDocument()
  })

  it('shows the refusal the server sent, and stays open to retry', async () => {
    server.use(
      http.post('/api/v1/outputs/:id/send', () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/plate-does-not-fit',
            title: 'Conflict',
            status: 409,
            detail: 'the model is 200 mm across and does not fit the A1 mini plate',
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Send to Bambuddy' }))
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(within(dialog).getByRole('alert')).toHaveTextContent('A1 mini'))
    expect(within(dialog).getByRole('button', { name: 'Send' })).toBeEnabled()
  })
```

In the same file:
- In `'says when no link back to the parameters was attached'`, replace the mocked body with `{ library_file_id: 41, filename: 'name-keychain-reagan.3mf', bambuddy_url: 'https://bambuddy.test/library', edit_url: null }`.
- In `'says nothing about the link when no public URL is configured'`, replace the mocked body with the same four fields.
- Delete these five tests outright (they test the queue mode, the copies box and the options disclosure, all removed): `'sends the per-send print options the Options disclosure collected (#88)'`, `'does not let an untouched Copies box beat a remembered quantity (#88)'`, `'keeps the Copies box and the Options row on one value (#88)'`, `'reports the quantity the server resolved, not one guessed locally (#88)'`, `'does not carry a per-send option into the next send (#88)'`.
- Drop `fireEvent` from the `@testing-library/react` import and `printOptions` from the `../mocks/fixtures` import (their only uses were in the deleted tests).

In `frontend/src/lib/problems.test.ts`, delete the whole `describe('eligibilityIssues', …)` block and change the import to:

```ts
import { refusedCheck, trackingDuplicates } from './problems'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/pages/CustomizePage.test.tsx -t "library"`
Expected: FAIL. `'sends a generated output to the library and links to it'` finds a radio (the queue/library mode fieldset).

- [ ] **Step 3: Rewrite `SendDialog.tsx`**

Replace the whole file with:

```tsx
import { useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { Output, SendResult } from '../api/types'
import { openExternal } from '../lib/embed'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  open: boolean
  output: Output | undefined
  onClose: () => void
  onSent: (result: SendResult) => void
}

/**
 * The send bar: upload this output's 3MF to the Bambuddy library, nothing more (#312).
 * Slicing and queueing is the Print dialog's job, so there is no mode, copies box or
 * print options here.
 */
export function SendDialog({ open, output, onClose, onSent }: Props) {
  // Only to tell "no link was configured" apart from "Bambuddy refused the note":
  // the send result reports an absent link the same way for both. Read each time the
  // dialog opens, not once per mount — the dialog outlives every send on the page,
  // and the setting can change between them.
  const settings = useAsync(async () => (open ? await api.getSettings() : null), [open])
  const publicUrl = settings.data?.public_url ?? null
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<SendResult | null>(null)

  function close() {
    setError(null)
    setResult(null)
    setSending(false)
    onClose()
  }

  async function send() {
    if (!output) return
    setSending(true)
    setError(null)
    try {
      const sent = await api.sendOutput(output.id, { mode: 'library' })
      setResult(sent)
      onSent(sent)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Send failed. Check the connection.')
    } finally {
      setSending(false)
    }
  }

  return (
    <Dialog
      open={open}
      title="Send to Bambuddy"
      description={result ? undefined : 'The file is uploaded from ScadBuddy, not your browser.'}
      onClose={close}
      footer={
        result ? (
          <>
            <Button onClick={close}>Done</Button>
            {result.bambuddy_url && (
              <Button variant="primary" onClick={() => openExternal(result.bambuddy_url as string)}>
                Open in library
              </Button>
            )}
          </>
        ) : (
          <>
            <Button onClick={close} disabled={sending}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void send()}
              disabled={sending || !output}
              {...USER_ONLY}
            >
              {sending && <Spinner />}
              {sending ? 'Sending' : 'Send'}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <>
          <p className="text-[13px] text-ink">
            Added to the library as <span className="sb-num">{result.filename}</span> (#
            {result.library_file_id}).
          </p>
          {/* The note is best-effort, so say which way it went rather than implying
              the link is on the file when Bambuddy refused it. Silence when no public
              URL is set: there was no link to attach, which is not a failure. */}
          {result.edit_url ? (
            <p className="mt-1.5 text-[12px] text-muted">
              Bambuddy has the link back to these parameters.
            </p>
          ) : publicUrl ? (
            <p className="mt-1.5 text-[12px] text-muted">
              Bambuddy would not take the link back to these parameters.
            </p>
          ) : null}
        </>
      ) : (
        <>
          <p className="text-[13px] text-ink">
            Adds the 3MF to the Bambuddy library, laid out for the printer set in Settings.
          </p>
          <p className="mt-1.5 text-[12px] text-muted">
            To slice and queue it, use Print instead.
          </p>
          {error && (
            <p role="alert" className="mt-3 text-[13px] text-warn">
              {error}
            </p>
          )}
        </>
      )}
    </Dialog>
  )
}
```

Note the result text: the old test matched `/Added to the library/` and `(#41)`; keep `Added to the library as` verbatim.

- [ ] **Step 4: Remove the now-dead helpers**

`frontend/src/lib/problems.ts`: delete `EligibilityIssue`, `describeIssue` and `eligibilityIssues` (lines 3-36). Keep `refusedCheck` and `trackingDuplicates`.

`frontend/src/lib/printOptions.ts`: delete `quantityBounds` and its doc comment (lines 129-133). Its only caller was `SendDialog`.

`frontend/src/api/types.ts`: delete `export type SendMode = SendResult['mode']`.

`frontend/src/mocks/handlers.ts`: replace the `/outputs/:id/send` handler with:

```ts
  http.post(`${base}/outputs/:id/send`, async ({ params }) => {
    const id = String(params['id'])
    const output = state.outputs.find((o) => o.id === id)
    if (!output) return problem(404, 'Output not found')
    if (!state.settings.has_api_key) {
      return problem(409, 'Bambuddy is not connected', 'Add an API key on the settings page.')
    }
    await delay(250)
    // #312: the send bar only uploads; nothing is queued, so no queue or run id is set.
    const libraryFileId = output.library_file_id ?? nextNumber()
    state.outputs = state.outputs.map((o) =>
      o.id === id ? { ...o, library_file_id: libraryFileId } : o,
    )
    const result: SendResult = {
      mode: 'library',
      library_file_id: libraryFileId,
      filename: `${output.slug}-${output.name ?? output.id}.3mf`,
      bambuddy_url: `${state.settings.bambuddy_url}/library`,
      edit_url: state.settings.public_url
        ? `${state.settings.public_url.replace(/\/$/, '')}${editPath(id)}`
        : null,
    }
    return HttpResponse.json(result)
  }),
```

(`mode: 'library'` stays until Task 2 regenerates `SendResult` without it. Today it is required.)

- [ ] **Step 5: Run the frontend gates**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test`
Expected: PASS. `e2e/agent-bridge.spec.ts` ("opens the send dialog, but cannot send") still passes, because the dialog title and the `Send` button are unchanged.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/SendDialog.tsx frontend/src/pages/CustomizePage.test.tsx \
  frontend/src/mocks/handlers.ts frontend/src/api/types.ts frontend/src/lib/problems.ts \
  frontend/src/lib/problems.test.ts frontend/src/lib/printOptions.ts
git commit -F - <<'EOF'
feat(send)!: the send dialog only uploads to the library (#312)

The mode radio, the Copies box, the print-options disclosure and the
eligibility-issue list are gone. Send uploads the 3MF, attaches the edit link
and offers "Open in library"; printing is the Print dialog's job.

Deleted tests (queue mode, copies and per-send options no longer exist):
- sends the per-send print options the Options disclosure collected (#88)
- does not let an untouched Copies box beat a remembered quantity (#88)
- keeps the Copies box and the Options row on one value (#88)
- reports the quantity the server resolved, not one guessed locally (#88)
- does not carry a per-send option into the next send (#88)
- eligibilityIssues (lib/problems.test.ts)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0159GufZGG4bJoXvBkEJ2S16
EOF
```

---

### Task 2: The send route only uploads (backend, then regenerate)

**Order:** backend first for the schema. It depends on Task 1 (the frontend no longer reads the removed `SendResult` fields). The only frontend edit is the one-line fallout in the typed mock.

**Files:**
- Modify: `backend/scadbuddy/bambuddy/send.py` (whole file)
- Modify: `backend/scadbuddy/api/outputs.py:17,25,335-361` (route, imports)
- Modify: `backend/scadbuddy/bambuddy/pipelines.py:1-8,97-100,216-223` (docstrings, `target_for` call)
- Modify: `backend/scadbuddy/bambuddy/options.py:26-28,47-49,73-80` (drop `beyond_pipeline`, `PIPELINE_CARRIED`)
- Modify: `backend/scadbuddy/render/plate.py:226-233` (drop `nozzle_diameter_of`)
- Modify: `backend/scadbuddy/render/bambu3mf.py:216-219` (comment)
- Modify: `backend/tests/api/test_send.py` (see Step 1)
- Delete: `backend/tests/api/test_send_options.py`
- Modify: `backend/tests/api/test_print_options_picker.py:17-18` (take `remember`, `queue_route` in-file)
- Modify: `backend/tests/api/test_print_plates.py:28`
- Modify: `backend/tests/api/test_print.py:1-82` (drop `pipelines_route`, `run_body`)
- Modify: `backend/tests/api/test_print_filaments.py:17,88,113,141,169`
- Modify: `backend/tests/api/test_print_progress.py:9,28-111` (delete the two send-queue tests)
- Modify: `backend/tests/bambuddy/test_options.py:57,95-98`
- Modify: `backend/tests/test_plate.py:19,32-50`
- Regenerate: `backend/openapi.json`, `frontend/src/api/schema.d.ts`, `agent/src/api/schema.d.ts`
- Modify: `frontend/src/mocks/handlers.ts` (drop `mode: 'library',` from the send result)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `class SendRequest(BaseModel): mode: Literal["library"] = "library"`
  - `class SendResult(BaseModel): library_file_id: int; filename: str; bambuddy_url: str | None = None; edit_url: str | None = None`
  - `async def send_output(client: BambuddyClient, store: OutputStore, meta: OutputMeta, settings: StoredSettings) -> SendResult`
  - `async def target_for(client: BambuddyClient, settings: StoredSettings, *, printer_id: int | None = None, nozzle_diameter: str | None = None) -> Target` (no `slug`, no `pipeline_id`)
  - `upload_output`, `ensure_uploaded`, `attach_edit_link`, `request_scope`, `resolve_print_options`, `register_sidebar`, `SidebarLink`, `Target`: signatures unchanged.
  - After this task nothing in `scadbuddy/` calls `settings.pipeline_for`, `client.pipelines()`, `client.pipeline()` from `send.py`, or `client.run_pipeline`. `settings.printer_preset` / `process_preset` / `filament_presets` / `bed_type` have no reader.

- [ ] **Step 1: Write the failing tests and retarget `test_send.py`**

In `backend/tests/api/test_send.py`:

Delete the `PRESETS` dict, `presets_route`, `A1_02_NOZZLE` and `pipeline_run_route`. Replace `plate_routes` with:

```python
def plate_routes(*, printer_id: int = 1, model: str = "H2C") -> None:
    """Mock what the send reads to learn which printer's plate to lay out for (#105):
    the printer list, for the model of the printer set in Settings."""
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": printer_id,
                    "name": "3DP-31B-598",
                    "model": model,
                    "is_active": True,
                    "nozzle_count": 2,
                }
            ],
        )
    )
```

Delete these tests (the queue mode, the pipeline nozzle and the slice-and-queue branch no longer exist):
`test_queue_mode_runs_the_configured_pipeline`, `test_an_ineligible_pipeline_surfaces_bambuddys_report_verbatim`, `test_queue_mode_without_a_pipeline_slices_then_enqueues`, `test_a_failed_slice_is_reported_rather_than_queued`, `test_queue_mode_with_neither_a_pipeline_nor_presets_says_so`, `test_queue_mode_with_no_printer_and_no_pipeline_says_so`, `test_more_colours_than_filament_slots_is_refused_before_slicing`, `test_copies_is_bounded`, `test_the_upload_states_the_pipelines_nozzle_diameter`, `test_a_printer_preset_the_catalogue_cannot_name_keeps_the_placeholder`, `test_an_unreadable_preset_catalogue_does_not_fail_the_send`, `test_a_malformed_preset_catalogue_does_not_fail_the_send`, `test_the_slice_and_queue_branch_annotates_both_files_last`, `test_a_failed_annotation_still_returns_the_queued_item`.

In `test_library_mode_uploads_to_the_configured_folder_and_records_the_id`, replace the three result assertions (`mode`, `library_file_id`, `queue_item_id`) with:

```python
    assert set(body) == {"library_file_id", "filename", "bambuddy_url", "edit_url"}
    assert body["library_file_id"] == 41
```

(keep the `bambuddy_url` assertion).

Replace `test_the_upload_is_laid_out_for_the_target_printers_plate` with:

```python
@respx.mock
def test_the_upload_is_laid_out_for_the_settings_printers_plate(
    client: TestClient, model: str
) -> None:
    """An H2C reaches x 25..325, so its centre is 175,160 — not the 256-plate's 128,128."""
    configure(client, printer_id=1)
    plate_routes(printer_id=1, model="H2C")
    output_id = make_output(client, model)
    upload = upload_route()

    assert client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}).is_success

    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
    item = root.find(".//{*}item")
    assert item is not None
    transform = [float(value) for value in (item.get("transform") or "").split()]
    assert transform[9:11] == [175.0, 160.0]
```

Rename `test_without_a_pipeline_the_upload_keeps_the_placeholder_nozzle` to `test_the_send_bar_upload_keeps_the_placeholder_nozzle`, give it this docstring, and drop the `presets = presets_route()` line and the `assert not presets.called` line (an unmocked `/slicer/presets` call now fails the test by itself):

```python
    """The send bar chooses no nozzle, so the 3MF keeps the placeholder and the preset
    catalogue is never read (#126 now applies to the print run only)."""
```

In `test_a_refused_re_send_leaves_the_previous_file_in_place`, delete the `respx.get(f"{API}/slicer-pipelines/")` mock.

Replace `test_a_failed_annotation_still_queues_the_print` with:

```python
@respx.mock
def test_a_failed_annotation_still_returns_the_upload(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The note is cosmetic; the upload is the point of the request."""
    configure(client, public_url="https://scad.test")
    output_id = make_output(client, model)
    upload = upload_route()
    annotate_route()  # the read succeeds; the write is what fails
    annotate = respx.put(f"{API}/library/files/41").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    assert response.status_code == 200
    body = response.json()
    assert body["library_file_id"] == 41
    # Nothing was attached, so the result does not claim a link.
    assert body["edit_url"] is None
    assert annotate.called
    assert upload.called
    meta = json.loads(
        (paths.output_dir(model, output_id) / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["library_file_id"] == 41
```

Replace `test_the_annotation_runs_after_the_work_that_matters` with:

```python
@respx.mock
def test_the_annotation_runs_after_the_upload(client: TestClient, model: str) -> None:
    """A slow or broken annotate must not sit in front of the upload."""
    configure(client, public_url="https://scad.test")
    output_id = make_output(client, model)
    upload_route()
    annotate_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    order = [(call.request.method, call.request.url.path) for call in respx.calls]
    assert order.index(("POST", "/api/v1/library/files")) < order.index(
        ("PUT", "/api/v1/library/files/41")
    )
```

Add, after `test_sending_an_unknown_output_is_a_404`:

```python
# --- #312 the send bar only uploads --------------------------------------------------


@respx.mock
def test_queue_mode_is_refused_and_nothing_is_uploaded(client: TestClient, model: str) -> None:
    """A stale client still asking to queue gets a 422, not a silent library upload."""
    configure(client)
    output_id = make_output(client, model)
    upload = upload_route()

    response = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "queue", "copies": 2}
    )

    assert response.status_code == 422
    assert not upload.called


@respx.mock
def test_an_old_clients_extra_fields_are_ignored(client: TestClient, model: str) -> None:
    """The dialog used to send ``options`` with every library send; that still uploads."""
    configure(client)
    output_id = make_output(client, model)
    upload_route()

    response = client.post(
        f"/api/v1/outputs/{output_id}/send",
        json={"mode": "library", "options": {"timelapse": False}},
    )

    assert response.status_code == 200


@respx.mock
def test_a_stored_pipeline_is_never_read_by_the_send(client: TestClient, model: str) -> None:
    """Until Task 6 removes the field, a stored ``pipeline_id`` must change nothing: the
    plate is the Settings printer's, and no ``/slicer-pipelines/`` route is mocked, so
    reading one would fail this test."""
    configure(client, pipeline_id=4, printer_id=1)
    plate_routes(printer_id=1, model="H2C")
    output_id = make_output(client, model)
    upload_route()

    assert client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}).is_success
    assert all("slicer-pipelines" not in str(call.request.url) for call in respx.calls)


@respx.mock
def test_a_send_starts_no_print(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    assert client.get(f"/api/v1/print/outputs/{output_id}/progress").json() is None
    record = client.get(f"/api/v1/outputs/{output_id}").json()
    assert record["queue_item_id"] is None
    assert record["print_route"] is None
```

(`test_a_stored_pipeline_is_never_read_by_the_send` still passes after Task 6, where `pipeline_id` in the PUT is ignored.)

Delete `backend/tests/api/test_send_options.py`. Its two helpers used elsewhere move into `backend/tests/api/test_print_options_picker.py`. Replace its line `from tests.api.test_send_options import queue_route, remember` with these definitions placed after the imports (add `from typing import Any`, `import httpx` and `from tests.bambuddy.conftest import recording`, and import `API` from `tests.api.test_send`):

```python
OPTIONS_ROUTE = "/api/v1/settings/print-options"


def remember(
    client: TestClient, scope: str, options: dict[str, Any], key: str | None = None
) -> None:
    body: dict[str, Any] = {"scope": scope, "options": options}
    if key is not None:
        body["key"] = key
    assert client.put(OPTIONS_ROUTE, json=body).status_code == 200


def queue_route() -> respx.Route:
    return respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue-item.json"))
    )
```

In `backend/tests/api/test_print_plates.py`, change `from tests.api.test_send_options import queue_route` to `from tests.api.test_print_options_picker import queue_route`.

In `backend/tests/api/test_print.py`, delete `pipelines_route` and `run_body`, change the import to `from tests.api.test_send import BASE`, and replace the last three sentences of the module docstring (from "The helpers below" on) with: `The helpers below (``printers_route``, ``presets_routes``) are imported by the other print test modules.`

In `backend/tests/api/test_print_filaments.py`, change the import to `from tests.api.test_print import presets_routes` and delete the four `pipelines_route()` calls. Keep `pipeline = respx.get(f"{API}/slicer-pipelines/1")` and `assert not pipeline.called` in `test_the_filament_step_shows_the_mounted_nozzles` as a guard.

In `backend/tests/api/test_print_progress.py`, delete `test_a_pipeline_run_is_followed_to_its_queue_entries` and `test_a_run_whose_slice_failed_reports_bambuddys_words_and_the_fix` (both started their print through the send bar's queue mode), and delete the `from tests.api.test_print import …` line.

In `backend/tests/bambuddy/test_options.py`, delete `test_quantity_alone_can_ride_a_pipeline_run_but_nothing_else_can`, and in the docstring near line 57 change ``SendRequest.copies`` to ``PrintRunRequest.copies``.

In `backend/tests/test_plate.py`, delete `class TestNozzleDiameter` and `nozzle_diameter_of` from the import.

- [ ] **Step 2: Run the tests to verify the new ones fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_send.py -q`
Expected: FAIL. `test_queue_mode_is_refused_and_nothing_is_uploaded` gets 409 or 200 instead of 422, `test_library_mode_…` finds `mode` in the body, and `test_a_stored_pipeline_is_never_read_by_the_send` fails on an unmocked `GET /slicer-pipelines/`.

- [ ] **Step 3: Rewrite `send.py`**

Replace `backend/scadbuddy/bambuddy/send.py` with the following. Functions not shown in full here (`_read_3mf`, `_plate_for_model`, `_laid_out_for`, `register_sidebar`, `SidebarLink`, the `SIDEBAR_*` constants) are copied verbatim from the current file.

```python
"""The send bar's library upload, the upload the print run shares, and the sidebar link.

Kept out of the route module so they can be tested against respx recordings without
a FastAPI app, and so the route stays a thin adapter. The send bar only uploads
(#312); slicing and queueing is the print dialog's run, in
``scadbuddy.bambuddy.pipelines``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import status
from pydantic import BaseModel

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import NOT_FOUND_PROBLEM, PLATE_FIT_PROBLEM, not_configured
from scadbuddy.bambuddy.models import ExternalLink
from scadbuddy.bambuddy.options import PrintOptions, resolve
from scadbuddy.core.problems import ApiError
from scadbuddy.library.deeplink import edit_url, merge_edit_note
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore, download_filename
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.bambu3mf import replate_3mf
from scadbuddy.render.plate import DEFAULT_PLATE as FALLBACK_PLATE
from scadbuddy.render.plate import PlateFitError, PlateGeometry, plate_for

logger = logging.getLogger(__name__)

SIDEBAR_NAME = "ScadBuddy"
# Earlier builds registered the link as "Customize"; adopt and rename it rather than
# leaving a second entry in Bambuddy's sidebar.
LEGACY_SIDEBAR_NAMES = frozenset({"Customize"})
SIDEBAR_ICON = "shapes"

LIBRARY_PATH = "/library"


class SendRequest(BaseModel):
    """The send bar's body. It only uploads to the library (#312).

    ``mode`` stays so that a client still asking for the removed ``"queue"`` mode is
    refused with a 422 rather than silently getting an upload it did not ask for. Any
    other field an older client sends (``copies``, ``options``) is ignored.
    """

    mode: Literal["library"] = "library"


class SendResult(BaseModel):
    library_file_id: int
    filename: str
    #: Bambuddy's library page, where the upload landed.
    bambuddy_url: str | None = None
    #: The "Edit in ScadBuddy" link attached to the library file, when one is known.
    edit_url: str | None = None


# SidebarLink: copy verbatim.
# _read_3mf: copy verbatim.


@dataclass(frozen=True)
class Target:
    """What the 3MF is laid out for: the target's plate and, when known, its nozzle."""

    plate: PlateGeometry
    #: The nozzle the print run chose (#126). ``None`` — the send bar, which chooses
    #: none — keeps the placeholder.
    nozzle_diameter: str | None = None

    @property
    def key(self) -> str:
        # copy the current body and docstring verbatim
        ...


async def target_for(
    client: BambuddyClient,
    settings: StoredSettings,
    *,
    printer_id: int | None = None,
    nozzle_diameter: str | None = None,
) -> Target:
    """The plate and nozzle the 3MF is laid out for.

    The plate is ``printer_id``'s, else the printer set in Settings, else the fallback
    plate; a printer Bambuddy reports without a model falls back the same way. The
    nozzle is stated only when the caller chose one: the print run does (spec
    2026-09-27 §4), the send bar does not.
    """
    printer_id = printer_id if printer_id is not None else settings.printer_id
    if printer_id is None:
        # Nothing to resolve against, so do not spend a round trip finding out.
        return Target(_plate_for_model(None), nozzle_diameter)
    printer = next((row for row in await client.printers() if row.id == printer_id), None)
    model = printer.model if printer is not None else None
    return Target(_plate_for_model(model), nozzle_diameter)


# _plate_for_model: copy verbatim.
# _laid_out_for: copy verbatim.
```

Then `upload_output` and `ensure_uploaded` verbatim, except:
- in both, the fallback line becomes `target = target if target is not None else await target_for(client, settings)`;
- in `ensure_uploaded`'s docstring, replace "The print picker (#86) leans on that: opening it checks eligibility, which needs a file in Bambuddy, and must not re-upload on every open." with "The print dialog leans on that: its filament step reads the plate's slots out of a library file, and must not re-upload on every open."

`attach_edit_link` verbatim, except the docstring sentence "The note is cosmetic — the file is already uploaded and the print already queued — so" becomes "The note is cosmetic — the file is already uploaded, and on the print run already queued — so".

`request_scope` and `resolve_print_options` verbatim, except their docstrings: "Shared by the send bar and the print picker (#78) for the same reason :func:`resolve_print_options` is." becomes "The print run's per-request overlay (#78)."; and "Shared by the send bar and the print picker (#124), so the two can never disagree about which remembered option wins." becomes "The print run's merge (#124). The send bar no longer queues (#312), so it resolves none."

Then:

```python
async def send_output(
    client: BambuddyClient,
    store: OutputStore,
    meta: OutputMeta,
    settings: StoredSettings,
) -> SendResult:
    """Upload the 3MF to the library and note the edit link on it. Nothing is queued."""
    meta, filename = await upload_output(client, store, meta, settings)
    if meta.library_file_id is None:  # pragma: no cover - upload_output always records one
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "the upload did not return a library file id")
    library_file_id = meta.library_file_id
    return SendResult(
        library_file_id=library_file_id,
        filename=filename,
        bambuddy_url=client.config.web_url(LIBRARY_PATH),
        edit_url=await attach_edit_link(client, library_file_id, meta, settings),
    )
```

and `register_sidebar` verbatim.

Gone from the file: `SendMode`, `QUEUE_PATH`, `DEFAULT_PLATE` (and the two-plates comment that explained it), `_target_model_and_preset`, `_nozzle_diameter`, `_check_colours`, `_settings_slice_request`, `pipeline_slice_request`, `_resolve_options`, `scope_printer`, `_queue_send`.

- [ ] **Step 4: Update the callers and the leftovers**

`backend/scadbuddy/api/outputs.py`: remove `PrintProgressDep` from the `scadbuddy.api.deps` import, and replace the send route with:

```python
@router.post(
    "/outputs/{output_id}/send",
    response_model=SendResult,
    summary="Upload the 3MF to the Bambuddy library",
)
async def send_output_to_bambuddy(
    output_id: OutputIdPath,
    body: SendRequest,
    outputs: OutputsDep,
    store: SettingsStoreDep,
) -> SendResult:
    """Upload ``model.3mf`` to the configured library folder, laid out for the printer
    set in Settings, and note the "Edit in ScadBuddy" link on it.

    Nothing is sliced or queued (#312): printing is ``POST /print/outputs/{id}/run``.
    ``mode`` accepts only ``"library"``.

    The file is read from the PVC and pushed by the server, so the API key never
    reaches the browser. A re-send replaces the file Bambuddy already holds rather
    than adding a second copy.
    """
    del body  # validated for its ``mode`` alone
    meta = require_output(outputs, output_id)
    settings = store.load()
    async with client_for(settings) as client:
        return await send_output(client, outputs, meta, settings)
```

`backend/scadbuddy/bambuddy/pipelines.py`:
- module docstring: replace the last sentence ("Bambuddy's own slicer pipelines are still what the send bar runs (``scadbuddy.bambuddy.send``), which is unaffected by this module.") with "The send bar only uploads (#312); this is the only path that prints."
- `PrintRunRequest.options` comment: "scope, as on ``SendRequest``." becomes "scope.".
- the `target_for` call in `run_for_output` becomes:

```python
    target = await target_for(
        client,
        settings,
        printer_id=printer_id,
        nozzle_diameter=choices.nozzles[0].size,
    )
```

`backend/scadbuddy/bambuddy/options.py`: delete `PIPELINE_CARRIED` (and its comment) and `PrintOptions.beyond_pipeline`. In the `quantity` comment, "mirroring ``SendRequest.copies``" becomes "mirroring ``PrintRunRequest.copies``".

`backend/scadbuddy/render/plate.py`: delete `nozzle_diameter_of` (its last caller was `send._nozzle_diameter`). Keep `_NOZZLE_SUFFIX`, which `plate_for` still uses.

`backend/scadbuddy/render/bambu3mf.py:216-219`: the comment becomes:

```python
#: Only the arity-free presence of this option matters; 1 and 3 entries were both
#: measured to slice identically on a two-extruder H2C. The print run replaces it
#: with the nozzle it chose (#126): slicing never reads it, but a person deciding
#: whether to start a print does.
```

- [ ] **Step 5: Run the backend gates**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest`
Expected: PASS. If ruff reports an import unused by the test edits (for example `pytest`, `trimesh` or `recording` in `test_send.py`, or `recording` in `test_print_progress.py`), delete that import and rerun.

- [ ] **Step 6: Regenerate and fix the typed mock**

Run the regenerate command from Global Constraints. Then in `frontend/src/mocks/handlers.ts` delete the line `      mode: 'library',` from the send handler's `SendResult`.

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test` and `cd agent && pnpm lint && pnpm typecheck && pnpm test`
Expected: PASS. `git diff --stat` shows `openapi.json` and both `schema.d.ts` changed: `SendResult` loses `mode`, `pipeline_run_id`, `queue_item_id`, `options`; `SendRequest` loses `copies`, `options`, and `mode` becomes `"library"`.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/bambuddy/send.py backend/scadbuddy/api/outputs.py \
  backend/scadbuddy/bambuddy/pipelines.py backend/scadbuddy/bambuddy/options.py \
  backend/scadbuddy/render/plate.py backend/scadbuddy/render/bambu3mf.py \
  backend/tests/api/test_send.py backend/tests/api/test_send_options.py \
  backend/tests/api/test_print_options_picker.py backend/tests/api/test_print_plates.py \
  backend/tests/api/test_print.py backend/tests/api/test_print_filaments.py \
  backend/tests/api/test_print_progress.py backend/tests/bambuddy/test_options.py \
  backend/tests/test_plate.py backend/openapi.json frontend/src/api/schema.d.ts \
  agent/src/api/schema.d.ts frontend/src/mocks/handlers.ts
git commit -F - <<'EOF'
feat(send)!: POST /outputs/{id}/send only uploads to the library (#312)

Queue mode is gone: no pipeline run, no slice-with-settings-presets, no
slice-with-pipeline-presets, no copies or per-send print options. mode accepts
only "library", so a stale client asking to queue gets a 422 instead of a
silent upload. The 3MF is laid out for the Settings printer's plate and keeps
the placeholder nozzle; the nozzle is stated by the print run only.
target_for loses slug/pipeline_id; nozzle_diameter_of, beyond_pipeline and
PIPELINE_CARRIED lose their last callers and go.

Deleted tests:
- test_send.py: test_queue_mode_runs_the_configured_pipeline,
  test_an_ineligible_pipeline_surfaces_bambuddys_report_verbatim,
  test_queue_mode_without_a_pipeline_slices_then_enqueues,
  test_a_failed_slice_is_reported_rather_than_queued,
  test_queue_mode_with_neither_a_pipeline_nor_presets_says_so,
  test_queue_mode_with_no_printer_and_no_pipeline_says_so,
  test_more_colours_than_filament_slots_is_refused_before_slicing,
  test_copies_is_bounded, test_the_upload_states_the_pipelines_nozzle_diameter,
  test_a_printer_preset_the_catalogue_cannot_name_keeps_the_placeholder,
  test_an_unreadable_preset_catalogue_does_not_fail_the_send,
  test_a_malformed_preset_catalogue_does_not_fail_the_send,
  test_the_slice_and_queue_branch_annotates_both_files_last,
  test_a_failed_annotation_still_returns_the_queued_item
- test_send_options.py (all 17; the picker's options are covered by
  test_print_options_picker.py, which now owns remember/queue_route)
- test_print_progress.py: test_a_pipeline_run_is_followed_to_its_queue_entries,
  test_a_run_whose_slice_failed_reports_bambuddys_words_and_the_fix
- test_options.py: test_quantity_alone_can_ride_a_pipeline_run_but_nothing_else_can
- test_plate.py: TestNozzleDiameter

Retargeted to library mode: the H2C plate layout (now via the Settings
printer), the placeholder nozzle, the failed-annotation and annotation-order
tests.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0159GufZGG4bJoXvBkEJ2S16
EOF
```

---

### Task 3: Progress and History stop reading the pipeline route (frontend first)

**Order:** frontend first. Every edit is valid against today's schema (`route` still admits `'pipeline'`, `pipeline_run_id` is still optional), and it is what lets Task 4 narrow them.

**Files:**
- Modify: `frontend/src/components/PrintProgressPanel.tsx:4-6,45-56`
- Modify: `frontend/src/components/PrintProgressPanel.test.tsx`
- Modify: `frontend/src/lib/usePrintProgress.test.ts:46-107`
- Modify: `frontend/src/mocks/fixtures.ts:645-712` (`pipelineProgress`, `failedRunProgress`)
- Modify: `frontend/src/mocks/handlers.ts:1640-1650,1735-1755`
- Modify: `frontend/src/pages/HistoryPage.tsx:110-115,187-196`
- Modify: `frontend/src/components/PrintPicker.test.tsx:27,570`
- Modify: `frontend/src/api/client.ts:507-509` (comment)

**Interfaces:**
- Consumes: `PrintProgress`, `Output` from `frontend/src/api/types.ts`.
- Produces: fixture `failedSliceProgress: PrintProgress` (route `'slice_queue'`) replacing `failedRunProgress`; `pipelineProgress` deleted. No component reads `route === 'pipeline'` or `pipeline_run_id` after this task.

- [ ] **Step 1: Write the failing tests**

In `frontend/src/components/PrintProgressPanel.test.tsx`:
- Delete `'heads a pipeline run with how many copies have reached the queue'`.
- Replace `'names the printer per copy, or says Bambuddy has still to pick one'` with:

```tsx
  it('names the printer, or says Bambuddy has still to pick one', () => {
    const entry = fixtures.queuedSliceProgress.copies_detail![0]!
    render(
      <PrintProgressPanel
        progress={{
          ...fixtures.queuedSliceProgress,
          copies_detail: [{ ...entry, printer_name: null }],
        }}
        polling
      />,
    )

    expect(screen.getByTestId('print-progress-copy-0')).toHaveTextContent(
      'Copy on a printer Bambuddy picks',
    )
  })
```

- In `'deep-links each queue entry from the URL the backend sent'`, render `fixtures.queuedSliceProgress` and look for the link named `'#4471'` with `href` `'https://bambuddy.internal.nullreference.io/queue/4471'`.
- Replace `'shows the recorded failed run verbatim, with the fix the backend chose'` with:

```tsx
  it('shows a failed slice verbatim, with the fix the backend chose', () => {
    const progress = fixtures.failedSliceProgress
    render(<PrintProgressPanel progress={progress} polling={false} />)

    expect(screen.getByTestId('print-progress')).toHaveTextContent('Slice — failed')
    const error = screen.getByTestId('print-progress-error')
    expect(error).toHaveTextContent(
      'Slice failed: The selected printer is not compatible with the process preset in the 3mf.',
    )
    expect(error).toHaveClass('text-warn')
    expect(screen.getByTestId('print-progress-fix')).toHaveTextContent(
      'Bambuddy could not slice this plate. Change the plate or print settings, or fix the model, and print again.',
    )
  })
```

- In `'spins only while the caller is still polling'`, use `fixtures.queuedSliceProgress` for the first render and `fixtures.failedSliceProgress` for the rerender.

In `frontend/src/lib/usePrintProgress.test.ts`, replace every `fixtures.failedRunProgress` with `fixtures.failedSliceProgress` and every `fixtures.pipelineProgress` with `fixtures.queuedSliceProgress`. In the first test, change the comment to `// A failed slice is settled, so only \`settled\` can end the poll.`

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/components/PrintProgressPanel.test.tsx src/lib/usePrintProgress.test.ts`
Expected: FAIL. `fixtures.failedSliceProgress` is undefined.

- [ ] **Step 3: Replace the fixtures**

In `frontend/src/mocks/fixtures.ts`, delete `pipelineProgress` and `failedRunProgress` (and their doc comments) and add:

```ts
/**
 * A slice that failed, so no queue item was ever created. `fix` is chosen by the
 * backend from *where* it failed (the slice), not from the wording of the message.
 */
export const failedSliceProgress: PrintProgress = {
  route: 'slice_queue',
  stage: 'failed',
  settled: true,
  slice_job_id: 7,
  queue_item_id: null,
  copies: 1,
  copies_completed: 0,
  copies_failed: 1,
  copies_cancelled: 0,
  copies_in_progress: 0,
  error_message:
    'Slice failed: The selected printer is not compatible with the process preset in the 3mf.',
  fix:
    'Bambuddy could not slice this plate. Change the plate or print settings, or fix the ' +
    'model, and print again.',
  copies_detail: [],
  bambuddy_url: QUEUE_URL,
}
```

- [ ] **Step 4: Stop reading the pipeline route**

`frontend/src/components/PrintProgressPanel.tsx`: delete the `if (progress.route === 'pipeline') { … }` block at the top of `headline`, and change the component doc's first line to `Where a print got to (#89): the slice, then the queue entries it produced.`

`frontend/src/mocks/handlers.ts`:
- in the `/print/outputs/:id/run` handler, the output update becomes `{ ...o, library_file_id: libraryFileId, queue_item_id: queueItemIds[0] }` (drop `pipeline_run_id: null`);
- in the `/print/outputs/:id/progress` handler, delete the `if (output.pipeline_run_id) { … }` branch.

`frontend/src/pages/HistoryPage.tsx`:
- delete the `{!output.queue_item_id && output.pipeline_run_id && ( … )}` block;
- the library block's condition becomes `{!output.queue_item_id && output.library_file_id && (`;
- in the `BambuddyId` doc comment, replace "Bambuddy has no page per pipeline run — its copies land in the queue — so a run links to the queue itself rather than to an invented path." with "A queue item links to its own page, and a library file to the library."

`frontend/src/components/PrintPicker.test.tsx`: in both `{ ...output, library_file_id: undefined, pipeline_run_id: undefined }` (lines ~27 and ~570) drop `pipeline_run_id: undefined`.

`frontend/src/api/client.ts`: the `attachToProject` comment becomes:

```ts
  /** Filed after the run, never during it: a plate's queue item only exists once it has
   * sliced, and an archive only once a print has finished, so the ids come from the
   * progress read (#89). */
```

- [ ] **Step 5: Run the frontend gates**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test`
Expected: PASS. `grep -rn "pipeline" frontend/src --include=*.ts --include=*.tsx | grep -v schema.d.ts` still lists Settings code (Task 5), comments in `PrintPicker.tsx`, and the `PrintPicker.test.tsx` assertions that no pipeline shows.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/PrintProgressPanel.tsx \
  frontend/src/components/PrintProgressPanel.test.tsx frontend/src/lib/usePrintProgress.test.ts \
  frontend/src/mocks/fixtures.ts frontend/src/mocks/handlers.ts frontend/src/pages/HistoryPage.tsx \
  frontend/src/components/PrintPicker.test.tsx frontend/src/api/client.ts
git commit -F - <<'EOF'
refactor(progress): the UI no longer reads the pipeline route (#312)

Nothing creates a pipeline run any more, so the progress panel's run headline,
History's "pipeline run #N" row and the pipelineProgress/failedRunProgress
fixtures go. failedSliceProgress (slice_queue) replaces the failed-run fixture.

Deleted tests:
- PrintProgressPanel: heads a pipeline run with how many copies have reached
  the queue (the per-copy printer and deep-link tests now use the queue route)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0159GufZGG4bJoXvBkEJ2S16
EOF
```

---

### Task 4: Progress drops the pipeline route; old records still load (backend, then regenerate)

**Order:** backend first for the schema. Depends on Tasks 2 and 3.

**Files:**
- Modify: `backend/scadbuddy/library/outputs.py:39-40,53-97,211-245`
- Modify: `backend/scadbuddy/bambuddy/progress.py:1-20,32,70-76,94-100,113-128,131-195,340-365`
- Modify: `backend/scadbuddy/api/printing.py:1-7,62-68,210-218,264-270` (docstrings; two are route descriptions in `openapi.json`)
- Modify: `backend/scadbuddy/bambuddy/projects.py:184-189` (docstring)
- Modify: `backend/tests/bambuddy/test_progress.py`
- Modify: `backend/tests/api/test_print_progress.py` (add the legacy-record test)
- Modify: `backend/tests/api/test_outputs.py:41`
- Modify: `backend/tests/test_event_bus.py:393`
- Regenerate: `backend/openapi.json`, `frontend/src/api/schema.d.ts`, `agent/src/api/schema.d.ts`

**Interfaces:**
- Consumes: nothing from Task 3 except that the frontend no longer reads the pipeline route.
- Produces:
  - `PrintRoute = Literal["slice_queue"]`
  - `OutputMeta` without `pipeline_run_id`. A before-validator drops `print_route`, `queue_item_id`, `slice_job_id`, `plates` and `pipeline_run_id` from a record whose last print was a pipeline run.
  - `OutputStore.record_send(...)` without `pipeline_run_id`.
  - `PrintProgress` without `pipeline_run_id`; `from_run`, `NEVER_QUEUED_FIX` and `RUN_FIX` deleted; `SLICE_FIX` reworded.
  - After this task, `client.pipeline_run` has no caller (Task 7 deletes it).

- [ ] **Step 1: Write the failing tests**

In `backend/tests/bambuddy/test_progress.py`:
- Change the module docstring to: `"""Issue #89 — normalising the slice-and-queue route into one progress view."""`
- Delete the `run()` helper, `PipelineRun` from the models import, and `NEVER_QUEUED_FIX` and `from_run` from the progress import.
- Delete every test that calls `from_run`: `test_a_failed_run_still_reporting_in_progress_is_settled_and_failed`, `test_a_run_whose_slice_never_produced_a_file_points_at_the_slicer`, `test_a_copy_that_never_reached_the_queue_points_at_eligibility`, `test_a_copy_that_reached_the_queue_and_then_failed_points_at_the_mapping`, `test_a_run_still_going_is_not_settled`, `test_every_copy_accounted_for_settles_even_without_a_completion_time`.
- In `test_the_route_is_taken_from_the_record_not_guessed`, drop `pipeline_run_id=1,` from the `meta(...)` call and change the docstring to `"""A recorded route is followed as it stands."""`.
- Replace `test_a_record_written_before_this_issue_still_follows_its_run` with:

```python
@respx.mock
async def test_a_record_whose_last_print_was_a_pipeline_run_has_no_progress(
    bambuddy: BambuddyClient,
) -> None:
    """#312: the pipeline route is gone. The record reads as never printed, and the queue
    item an *earlier* print left beside it is not mistaken for the print now running."""
    record = meta(print_route="pipeline", pipeline_run_id=1, queue_item_id=51, slice_job_id=9)
    assert record.print_route is None
    assert record.queue_item_id is None
    assert await progress_for(bambuddy, record) is None


@respx.mock
async def test_a_record_from_before_routes_with_a_run_id_has_no_progress(
    bambuddy: BambuddyClient,
) -> None:
    """Before #89 a run id meant the pipeline route even beside a queue item id."""
    record = meta(pipeline_run_id=1, queue_item_id=51)
    assert record.queue_item_id is None
    assert await progress_for(bambuddy, record) is None


@respx.mock
async def test_a_record_from_before_routes_with_only_a_queue_item_follows_it(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(200, json={"id": 51, "status": "completed"})
    )
    progress = await progress_for(bambuddy, meta(queue_item_id=51))
    assert progress is not None
    assert progress.route == "slice_queue"
    assert progress.queue_item_id == 51
```

- Replace the body of `test_a_bambuddy_that_refuses_the_read_is_not_swallowed` with:

```python
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(500, json={"detail": "the database is locked"})
    )
    with pytest.raises(ApiError) as raised:
        await progress_for(
            bambuddy, meta(slice_job_id=9, queue_item_id=51, print_route="slice_queue")
        )
    assert "the database is locked" in raised.value.detail
```

In `backend/tests/api/test_print_progress.py` add (with `import json` and `from scadbuddy.core.paths import DataPaths`):

```python
@respx.mock
def test_an_output_last_printed_by_a_pipeline_run_still_opens(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#312: a record written by the old send bar must not become unreadable."""
    configure(client)
    output_id = make_output(client, model)
    path = paths.output_dir(model, output_id) / "meta.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(print_route="pipeline", pipeline_run_id=12, queue_item_id=7, library_file_id=41)
    path.write_text(json.dumps(record), encoding="utf-8")

    detail = client.get(f"/api/v1/outputs/{output_id}")
    assert detail.status_code == 200
    assert "pipeline_run_id" not in detail.json()
    assert detail.json()["library_file_id"] == 41
    assert detail.json()["queue_item_id"] is None
    assert client.get(f"/api/v1/models/{model}/outputs").status_code == 200
    assert client.get(f"/api/v1/print/outputs/{output_id}/progress").json() is None
```

In `backend/tests/api/test_outputs.py:41`, change `assert body["pipeline_run_id"] is None` to `assert "pipeline_run_id" not in body`.

In `backend/tests/test_event_bus.py:393`, change `route="pipeline"` to `route="slice_queue"` (keep the `type: ignore[arg-type]`, which is for `stage`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_progress.py tests/api/test_print_progress.py tests/api/test_outputs.py -q`
Expected: FAIL. The pipeline-record tests find `queue_item_id == 51` still set, and `test_outputs` finds `pipeline_run_id` in the body.

- [ ] **Step 3: Narrow the record**

In `backend/scadbuddy/library/outputs.py`:

```python
#: Which Bambuddy route produced the ids below; see ``bambuddy/dispatch.py``. The
#: ``"pipeline"`` route went with the send bar's queue mode (#312).
PrintRoute = Literal["slice_queue"]

#: What the last print left on a record. A record whose last print was a pipeline run
#: may still carry an *older* slice-and-queue print's ids here, so they go with it.
_LAST_PRINT_FIELDS = frozenset(
    {"print_route", "pipeline_run_id", "queue_item_id", "slice_job_id", "plates"}
)
```

In `OutputMeta`, delete `pipeline_run_id: int | None = None`, change the `plates` comment's last sentence to "Empty on records written before multi-plate prints.", and add (import `Any` from `typing` and `model_validator` from `pydantic`):

```python
    @model_validator(mode="before")
    @classmethod
    def _forget_a_pipeline_run(cls, data: Any) -> Any:
        """Records from before #312 can say their last print was a pipeline run: either
        ``print_route: "pipeline"``, or (before #89) no route and a run id. That route is
        gone, so the record reads as never printed rather than failing to load or
        reporting an older print's queue item as the current one."""
        if not isinstance(data, dict):
            return data
        route = data.get("print_route")
        if route == "pipeline" or (route is None and data.get("pipeline_run_id") is not None):
            return {key: value for key, value in data.items() if key not in _LAST_PRINT_FIELDS}
        return data
```

In `record_send`, delete the `pipeline_run_id` parameter and its `("pipeline_run_id", pipeline_run_id),` entry.

- [ ] **Step 4: Drop the route from progress**

In `backend/scadbuddy/bambuddy/progress.py`:
- Module docstring, first 15 lines, becomes:

```python
"""Following a print to its queue entries (#89).

A print leaves ScadBuddy by one route, slice then queue: a slice job finishes, and a
queue item per plate carries ``quantity``. The send bar's old pipeline route (#312)
is gone; a record left by it reads as never printed (``OutputMeta``).
```

  and keep the "**The fix that applies is derived from where it failed…**" paragraph.
- Import line: `from scadbuddy.bambuddy.models import QueueItem, SliceJob`.
- `CopyProgress` docstring: "A pipeline run reports one of these per copy; the queue route has exactly one, because …" becomes "The queue route has one per plate, because …" (keep the rest of the sentence).
- `PrintProgress`: delete `pipeline_run_id: int | None = None`.
- Delete `NEVER_QUEUED_FIX`, `RUN_FIX` and `from_run`. `SLICE_FIX` becomes:

```python
SLICE_FIX = (
    "Bambuddy could not slice this plate. Change the plate or print settings, or fix the "
    "model, and print again."
)
```

- Replace the route selection at the top of `progress_for` (from `route = meta.print_route` down to the end of the `if route == "pipeline":` block) with:

```python
    route = meta.print_route
    if route is None and meta.queue_item_id is not None:
        # Records written before #89 carry no route; a queue item is this route's.
        route = "slice_queue"
    if route != "slice_queue":
        return None
    url = client.config.web_url(QUEUE_PATH)
```

  then dedent the old `if route == "slice_queue":` body one level (keep the multi-plate `TaskGroup` block and the final `return await _queued_progress(...)` unchanged), and delete the trailing `return None`. In the docstring delete "The route is taken from the record rather than guessed from which ids are set, because an output printed both ways carries both."

In `backend/scadbuddy/api/printing.py`:
- module docstring's last sentence ("``POST /outputs/{id}/send`` stays where it was: … unrelated to the dialog's own run.") becomes "``POST /outputs/{id}/send`` stays in ``outputs.py``: it only uploads (#312).";
- `ProjectAttach` docstring's second sentence becomes "The ids come from the progress read (#89): a plate's queue item only exists once it has sliced, so the caller learns them by polling.";
- the progress route's docstring: replace the paragraph "The poll is needed rather than optional on the pipeline route: … while a copy is still being dispatched." with "``settled`` is what says the polling can stop.", and the first line "Follow whichever of Bambuddy's two routes this output last took (#89)." with "Follow this output's last print, slice then queue (#89).";
- the attach route's docstring: "a pipeline run's queue entries are created by a background task" becomes "a plate's queue item only exists once it has sliced".

In `backend/scadbuddy/bambuddy/projects.py:184-186`, "``jobs[].queue_entry_id`` is null when a pipeline run answers 202, and an archive only exists once a print has finished — so attaching at run time would attach nothing on one route and only half on the other." becomes "A plate's queue item only exists once it has sliced, and an archive only once a print has finished — so attaching at run time would attach only part of it."

- [ ] **Step 5: Run the backend gates**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest`
Expected: PASS.

- [ ] **Step 6: Regenerate and run the frontend and agent gates**

Run the regenerate command, then the frontend and agent gates.
Expected: PASS with no frontend edit (Task 3 removed every reader). `PrintProgress.route` is now `"slice_queue"` and `pipeline_run_id` is gone from `PrintProgress` and `OutputDetail`.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/library/outputs.py backend/scadbuddy/bambuddy/progress.py \
  backend/scadbuddy/api/printing.py backend/scadbuddy/bambuddy/projects.py \
  backend/tests/bambuddy/test_progress.py backend/tests/api/test_print_progress.py \
  backend/tests/api/test_outputs.py backend/tests/test_event_bus.py backend/openapi.json \
  frontend/src/api/schema.d.ts agent/src/api/schema.d.ts
git commit -F - <<'EOF'
refactor(progress)!: drop the pipeline route; old records still load (#312)

PrintRoute is slice_queue only, and OutputMeta/PrintProgress lose
pipeline_run_id. A record whose last print was a pipeline run (route
"pipeline", or no route and a run id) loads with its last-print ids dropped,
so it reads as never printed instead of failing validation or reporting an
older print's queue item. from_run, RUN_FIX and NEVER_QUEUED_FIX go.

Deleted tests (test_progress.py, all exercised from_run):
- test_a_failed_run_still_reporting_in_progress_is_settled_and_failed
- test_a_run_whose_slice_never_produced_a_file_points_at_the_slicer
- test_a_copy_that_never_reached_the_queue_points_at_eligibility
- test_a_copy_that_reached_the_queue_and_then_failed_points_at_the_mapping
- test_a_run_still_going_is_not_settled
- test_every_copy_accounted_for_settles_even_without_a_completion_time
- test_a_record_written_before_this_issue_still_follows_its_run (replaced by
  the three legacy-record tests)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0159GufZGG4bJoXvBkEJ2S16
EOF
```

---

### Task 5: Settings page drops the pipeline selector (frontend first)

**Order:** frontend first. Every removed field is optional in today's `SettingsView`, `SettingsPatch` and `BambuddyTargets`, so the fixtures and form can drop them before Task 6.

**Files:**
- Modify: `frontend/src/pages/SettingsPage.tsx:48,80,91,108,116,129,150,346-368`
- Modify: `frontend/src/pages/SettingsPage.test.tsx:64-75`
- Modify: `frontend/src/pages/agentTools.test.tsx:130-157`
- Modify: `frontend/src/agent/catalog.ts:29-37`
- Modify: `frontend/src/mocks/fixtures.ts:522-535,605-630` (`settings`, `targets.pipelines`)
- Modify: `frontend/src/mocks/handlers.ts:103,1864` (comment, PUT body cast)
- Modify: `frontend/src/api/types.ts:83` (drop `BambuddyPipeline`)
- Modify: `frontend/src/components/PrintOptionsDisclosure.tsx:26-30,89-91` (comments)

**Interfaces:**
- Consumes: `api.getSettings`, `api.putSettings`, `api.getBambuddyTargets` (unchanged).
- Produces: `SETTINGS_FIELDS = ['bambuddy_url', 'public_url', 'library_folder_id', 'printer_id', 'default_plate', 'display_unit'] as const`. The Settings form never sends `pipeline_id`.

- [ ] **Step 1: Write the failing tests**

In `frontend/src/pages/SettingsPage.test.tsx`, replace `'offers the folders and pipelines Bambuddy reports'` with:

```tsx
  it('offers the folders and printers Bambuddy reports, and no slicer pipeline', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    expect(await screen.findByRole('option', { name: 'ScadBuddy' })).toBeInTheDocument()
    // Bambuddy's ids are integers, so the <select> values are their decimal strings.
    expect(screen.getByLabelText('Library folder')).toHaveValue('2')
    expect(screen.getByLabelText('Printer')).toHaveValue('1')
    // #312: printing is the Print dialog's, which derives its own presets.
    expect(screen.queryByLabelText('Slicer pipeline')).not.toBeInTheDocument()
  })

  it('never sends a pipeline', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).not.toHaveProperty('pipeline_id')
    put.mockRestore()
  })
```

In `frontend/src/pages/agentTools.test.tsx`, at the end of `'reads the form without the key, and sets a field without saving'`, add:

```tsx
    // #312: the pipeline is not a field any more, and the form does not report one.
    const pipeline = await bridge.call('set_field', { field: 'pipeline_id', value: '1' })
    expect(!pipeline.ok && pipeline.error.code).toBe('invalid_args')
    expect(form.ok && Object.keys(form.result.values)).not.toContain('pipeline_id')
```

If `form.result` is not typed as having `values`, use `expect(JSON.stringify(form)).not.toContain('pipeline_id')` instead.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/pages/SettingsPage.test.tsx src/pages/agentTools.test.tsx`
Expected: FAIL. The page still has a `Slicer pipeline` select and sends `pipeline_id`, and `set_field` accepts `pipeline_id`.

- [ ] **Step 3: Remove the selector**

`frontend/src/pages/SettingsPage.tsx`: delete the `pipelineId` state, `setPipelineId(idValue(settings.pipeline_id))`, `pipeline_id: asId(pipelineId),` in `draft()`, the `pipeline_id` entries in `form`, `choices` and the `get_form` `choices`, the `pipelineId !== idValue(settings.pipeline_id) ||` line in `dirty`, and the whole `<div>` holding the `slicer-pipeline` label, select and help text (lines ~346-368).

`frontend/src/agent/catalog.ts`: delete `'pipeline_id',` from `SETTINGS_FIELDS`.

`frontend/src/mocks/fixtures.ts`: in `settings`, delete `pipeline_id`, `printer_preset`, `process_preset`, `filament_presets` and `bed_type`; in `targets`, delete the `pipelines` array and the comment above it.

`frontend/src/mocks/handlers.ts`: in the `PUT /settings` body cast, delete `pipeline_id?: number | null`; at line ~103 the comment "No global fallback, unlike the pipeline default." becomes "No global fallback.".

`frontend/src/api/types.ts`: delete `export type BambuddyPipeline = Schemas['Pipeline']`.

`frontend/src/components/PrintOptionsDisclosure.tsx`:
- the `printerId` prop comment becomes:

```tsx
  /**
   * The printer the print will reach, when the caller already knows it. Left undefined,
   * the server says which one the per-printer scope keys on: the printer set in Settings.
   */
```

- the comment at ~89 becomes `// The per-printer scope only applies once the printer is known; with none chosen and` / `// none in Settings, saving there would go nowhere — hence the disabled option and the note below.`

- [ ] **Step 4: Run the frontend gates**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/SettingsPage.tsx frontend/src/pages/SettingsPage.test.tsx \
  frontend/src/pages/agentTools.test.tsx frontend/src/agent/catalog.ts \
  frontend/src/mocks/fixtures.ts frontend/src/mocks/handlers.ts frontend/src/api/types.ts \
  frontend/src/components/PrintOptionsDisclosure.tsx
git commit -F - <<'EOF'
feat(settings)!: remove the slicer pipeline selector (#312)

Settings' "Where files go" keeps the library folder and printer. The agent's
set_field no longer accepts pipeline_id, and the form never sends one.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0159GufZGG4bJoXvBkEJ2S16
EOF
```

---

### Task 6: Settings lose the pipeline and raw-preset fields (backend, then regenerate)

**Order:** backend first for the schema. Depends on Tasks 2 and 5.

**Files:**
- Modify: `backend/scadbuddy/library/settings_store.py:14,49-59,62-73,100-131,148-163,224-239,297-299`
- Modify: `backend/scadbuddy/api/settings.py:3-19,22-37,55-66,95-117,139-177,210-221`
- Modify: `backend/scadbuddy/core/events.py:160-167`
- Modify: `backend/tests/api/test_settings.py:19-33,65,78` (+2 tests)
- Modify: `backend/tests/api/test_print_options.py:1-16,134-257`
- Modify: `backend/tests/api/test_sidebar.py:155-172`
- Modify: `backend/tests/test_event_bus.py:363-377`
- Regenerate: `backend/openapi.json`, `frontend/src/api/schema.d.ts`, `agent/src/api/schema.d.ts`

**Interfaces:**
- Consumes: nothing reads `pipeline_for`, `pipeline_id` or the raw presets after Task 2.
- Produces:
  - `BambuddyIds`: `library_folder_id`, `printer_id` only.
  - `StoredSettings` without `printer_preset`, `process_preset`, `filament_presets`, `bed_type`, `model_pipelines`, `pipeline_for`.
  - `SettingsPatch`, `SettingsView` without `pipeline_id`, `printer_preset`, `process_preset`, `filament_presets`, `bed_type`.
  - `SettingsStore.set_model_pipeline` deleted; `SettingsSection` without `"model_pipeline"`.
  - `BambuddyTargets`: `folders`, `printers` only.
  - `GET /settings/print-options` takes no `pipeline_id`, never calls Bambuddy, and reports `printer_id = settings.printer_id`.
  - After this task, `client.pipelines()` and `client.pipeline()` have no caller (Task 7 deletes them).

- [ ] **Step 1: Write the failing tests**

`backend/tests/api/test_settings.py`: the expected dict in `test_defaults_are_empty_and_the_key_is_absent` becomes:

```python
    assert client.get("/api/v1/settings").json() == {
        "bambuddy_url": None,
        "has_api_key": False,
        "public_url": None,
        "library_folder_id": None,
        "printer_id": None,
        "default_plate": None,
        "display_unit": "mm",
    }
```

At lines ~65 and ~78, change `json={"pipeline_id": 3}` to `json={"printer_id": 3}`. Add:

```python
def test_a_settings_file_from_before_312_still_loads_and_sheds_the_old_keys(
    client: TestClient, data_dir: Path
) -> None:
    legacy = {
        "bambuddy_url": "https://bambuddy.test",
        "pipeline_id": 4,
        "printer_id": 1,
        "printer_preset": {"source": "cloud", "id": "GM041"},
        "process_preset": {"source": "cloud", "id": "GP252"},
        "filament_presets": [{"source": "cloud", "id": "GFSA05_22"}],
        "bed_type": "Textured PEI Plate",
        "model_pipelines": {"demo": 9},
    }
    (data_dir / "settings.json").write_text(json.dumps(legacy), encoding="utf-8")

    body = client.get("/api/v1/settings").json()
    assert body["bambuddy_url"] == "https://bambuddy.test"
    assert body["printer_id"] == 1
    assert "pipeline_id" not in body
    assert "printer_preset" not in body

    assert client.put("/api/v1/settings", json={"public_url": "https://scad.test"}).is_success
    stored = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    for key in (
        "pipeline_id",
        "printer_preset",
        "process_preset",
        "filament_presets",
        "bed_type",
        "model_pipelines",
    ):
        assert key not in stored
    assert stored["printer_id"] == 1


def test_an_old_client_sending_a_pipeline_is_not_refused(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"pipeline_id": 3, "printer_id": 2})
    assert response.status_code == 200
    assert response.json()["printer_id"] == 2
    assert "pipeline_id" not in response.json()
```

`backend/tests/api/test_print_options.py`: delete `test_the_printer_the_scope_keys_on_comes_from_the_pipeline_when_no_printer_is_set`, `test_a_models_stored_pipeline_no_longer_decides_the_printer_the_scope_keys_on`, `test_a_pipeline_that_no_longer_exists_is_not_fatal_either` and `test_the_pipeline_about_to_run_decides_the_printer_the_scope_keys_on`. Replace `test_remembering_an_option_never_needs_a_reachable_bambuddy` and `test_an_unreachable_bambuddy_still_serves_what_needs_no_bambuddy` with:

```python
@respx.mock
def test_the_options_never_touch_bambuddy(client: TestClient) -> None:
    """Both halves are settings.json alone (#312: no pipeline to resolve a printer from).
    No Bambuddy route is mocked, so any call would fail this test."""
    client.put("/api/v1/settings", json={"bambuddy_url": "https://bambuddy.test"})

    saved = client.put(ROUTE, json={"scope": "global", "options": {"timelapse": False}})
    read = client.get(ROUTE)

    assert saved.status_code == 200
    body = read.json()
    assert body["printer_id"] is None
    assert body["global_options"]["timelapse"] is False
    assert body["defaults"]["bed_levelling"] == "auto"
```

Remove the imports this leaves unused (`httpx`, `DataPaths`, `Settings`, `SETTINGS_NAME`, `SettingsStore`, `recording`; ruff names any others).

`backend/tests/api/test_sidebar.py`, `test_targets_feed_the_settings_pickers`: delete the `slicer-pipelines/` mock and replace `assert body["pipelines"] == []` with `assert set(body) == {"folders", "printers"}`.

`backend/tests/test_event_bus.py`, `test_every_settings_write_is_announced_with_its_section`: replace `store.set_model_pipeline("demo", 3)` with `store.set_printer_bed_type(1, "Cool Plate")` and `"model_pipeline",` with `"printer_bed_type",`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_settings.py tests/api/test_print_options.py tests/api/test_sidebar.py tests/test_event_bus.py -q`
Expected: FAIL. The defaults dict still has the pipeline and preset keys, the legacy keys survive the write, and `targets` still has `pipelines`.

- [ ] **Step 3: Remove the fields**

`backend/scadbuddy/library/settings_store.py`:
- import: `from scadbuddy.bambuddy.models import NozzleChoice, SlotChoice, Tier` (drop `PresetRef`);
- `BambuddyIds`: delete `pipeline_id`; its docstring becomes "Integers, because that is what Bambuddy's own OpenAPI declares for ``folder_id`` and ``printer_id`` — an earlier draft of this file typed them as strings.";
- `ModelPrintChoices` docstring's first two sentences become "What the print dialog last chose for one model (#78). ``printer_id`` is the printer it printed on. ``filament_plan`` is only …" (keep the rest);
- `StoredSettings`: delete the `# Used by "Slice and queue" …` block (four fields), `model_pipelines` with its comment, and `pipeline_for`. Change the `model_print_choices` comment's "set one model at a time for the same reason" to "set one model at a time";
- add to the `StoredSettings` docstring: "A ``settings.json`` from before #312 may still hold ``pipeline_id``, the four raw-preset keys or ``model_pipelines``; pydantic ignores unknown keys, so it loads, and the next write drops them.";
- `SettingsPatch`: delete `pipeline_id`, `printer_preset`, `process_preset`, `filament_presets`, `bed_type`. Add to its docstring: "Keys it does not declare, such as an older client's ``pipeline_id``, are ignored.";
- delete `set_model_pipeline`;
- in `save_print_options`' docstring, ":meth:`set_model_pipeline` is not" becomes ":meth:`set_model_choices` is not".

`backend/scadbuddy/core/events.py`: delete `"model_pipeline",` from `SettingsSection`.

`backend/scadbuddy/api/settings.py`:
- imports: `from scadbuddy.bambuddy.models import Folder, Printer`; delete `import logging` and `logger = logging.getLogger(__name__)` (the only user was `get_print_options`); `ApiError` stays (`test_settings` uses it).
- `SettingsView`: delete `pipeline_id`, `printer_preset`, `process_preset`, `filament_presets`, `bed_type`. `_view`: delete the same five keyword arguments.
- `PrintOptionsState` docstring becomes: `"""The view plus the printer the per-printer scope keys on: the printer set in Settings, or none. Neither half needs Bambuddy."""`
- `BambuddyTargets`: delete `pipelines`.
- `get_print_options` becomes:

```python
@router.get(
    "/settings/print-options",
    response_model=PrintOptionsState,
    summary="Remembered print options",
)
def get_print_options(
    store: SettingsStoreDep,
    slug: Annotated[
        str | None,
        Query(description="The model about to be printed"),
    ] = None,
) -> PrintOptionsState:
    settings = store.load()
    return PrintOptionsState(**_options_view(settings).model_dump(), printer_id=settings.printer_id)
```

- `get_targets`: summary `"Folders and printers to choose from"`, and delete `pipelines=await client.pipelines(),`.

- [ ] **Step 4: Run the backend gates**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest`
Expected: PASS.

- [ ] **Step 5: Regenerate and run the frontend and agent gates**

Run the regenerate command, then the frontend and agent gates.
Expected: PASS with no frontend edit. `Pipeline` disappears from `components.schemas`, and `SettingsView`, `SettingsPatch` and `BambuddyTargets` lose the fields.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/library/settings_store.py backend/scadbuddy/api/settings.py \
  backend/scadbuddy/core/events.py backend/tests/api/test_settings.py \
  backend/tests/api/test_print_options.py backend/tests/api/test_sidebar.py \
  backend/tests/test_event_bus.py backend/openapi.json frontend/src/api/schema.d.ts \
  agent/src/api/schema.d.ts
git commit -F - <<'EOF'
feat(settings)!: remove pipeline_id and the raw slicer-preset settings (#312)

pipeline_id, printer_preset, process_preset, filament_presets, bed_type,
model_pipelines, pipeline_for, set_model_pipeline and the "model_pipeline"
settings section go. An older settings.json still loads (unknown keys are
ignored) and sheds them on the next write; an old client's PUT with
pipeline_id is a 200. GET /settings/print-options no longer takes pipeline_id
or calls Bambuddy: the per-printer scope keys on the Settings printer. The
targets route lists folders and printers only.

Deleted tests (test_print_options.py; there is no pipeline to resolve):
- test_the_printer_the_scope_keys_on_comes_from_the_pipeline_when_no_printer_is_set
- test_a_models_stored_pipeline_no_longer_decides_the_printer_the_scope_keys_on
- test_a_pipeline_that_no_longer_exists_is_not_fatal_either
- test_the_pipeline_about_to_run_decides_the_printer_the_scope_keys_on
- test_remembering_an_option_never_needs_a_reachable_bambuddy and
  test_an_unreachable_bambuddy_still_serves_what_needs_no_bambuddy (merged
  into test_the_options_never_touch_bambuddy)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0159GufZGG4bJoXvBkEJ2S16
EOF
```

---

### Task 7: Remove the Bambuddy client's pipeline surface (backend only)

**Order:** backend only, after Tasks 2, 4 and 6 removed every caller. No schema change is expected; Step 5 regenerates to prove it.

**Files:**
- Modify: `backend/scadbuddy/bambuddy/client.py:1-15,34-65,224-246,490-568`
- Modify: `backend/scadbuddy/bambuddy/models.py:20-21,280-314,380-487`
- Modify: `backend/scadbuddy/bambuddy/errors.py:19-45,100-109`
- Modify: `backend/tests/bambuddy/test_client.py:10-24,65-81,208-240,393-411`
- Modify: `backend/tests/bambuddy/test_client_print_workflow.py:19-28,135-345,558-651`
- Delete: `backend/tests/bambuddy/recordings/slicer-pipeline.json`, `slicer-pipelines.json`, `slicer-pipelines-configured.json`, `pipeline-run.json`, `pipeline-runs.json`
- Modify: `backend/tests/bambuddy/recordings/README.md` (the entries for those five files and the pipeline-only notes)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `BambuddyClient` without `pipelines`, `pipeline`, `create_pipeline`, `check_eligibility`, `run_pipeline`, `pipeline_run`, `pipeline_runs`.
  - `models.py` without `TargetKind`, `FanoutStrategy`, `Pipeline`, `PipelineCreate`, `PipelineList`, `PipelineJob`, `PipelineRun`, `PipelineRunList`, `PipelineRunRequest`, `EligibilityRequest`, `EligibilityIssue`, `PerPrinterReport`, `EligibilityReport`.
  - `errors.py`: `ELIGIBILITY_PROBLEM` replaced by `CONFLICT_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-conflict"`. A Bambuddy 409 is still a 409 with `bambuddy_body` (see Conflict 1 in the controller notes).

- [ ] **Step 1: Write the failing test**

In `backend/tests/bambuddy/test_client.py`, replace `test_a_409_carries_the_eligibility_report_verbatim` with:

```python
@respx.mock
async def test_a_409_passes_bambuddys_body_through(bambuddy: BambuddyClient) -> None:
    """Bambuddy documents no 409, but a live call that answers one is a conflict to
    report as such, not a 502 "unavailable"."""
    body = {"detail": "printer 1 is busy"}
    respx.post(f"{API}/queue/").mock(return_value=httpx.Response(409, json=body))

    with pytest.raises(ApiError) as caught:
        await bambuddy.enqueue(QueueItemCreate(printer_id=1, library_file_id=2))

    assert caught.value.status == 409
    assert caught.value.type == CONFLICT_PROBLEM
    assert caught.value.extensions["bambuddy_body"] == body
```

and in its imports replace `ELIGIBILITY_PROBLEM` with `CONFLICT_PROBLEM` and drop `PipelineRunRequest`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_client.py -q`
Expected: FAIL at collection with `ImportError: cannot import name 'CONFLICT_PROBLEM'`.

- [ ] **Step 3: Rename the 409 mapping**

In `backend/scadbuddy/bambuddy/errors.py`:
- replace `ELIGIBILITY_PROBLEM = "https://scadbuddy.dev/problems/pipeline-ineligible"` with `CONFLICT_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-conflict"`;
- the 409 branch becomes:

```python
    if code == status.HTTP_409_CONFLICT:
        # Bambuddy's own reason lives in the body; pass it through verbatim rather than
        # paraphrase it.
        return ApiError(
            status.HTTP_409_CONFLICT,
            f"Bambuddy reported a conflict when asked to {what}{suffix}",
            type_=CONFLICT_PROBLEM,
            bambuddy_status=code,
            bambuddy_body=upstream_body(response),
        )
```

- in the `Scope` docstring, delete the two sentences from "Which flag guards ``/slicer-pipelines/`` could **not** be verified" to "this is the line to correct." (ScadBuddy no longer calls that route).

- [ ] **Step 4: Delete the pipeline surface**

`backend/scadbuddy/bambuddy/client.py`: delete the seven methods listed under **Produces**, the `# --- pipelines and queue ---` heading (keep `queue_item` and `enqueue` under a `# --- queue ---` heading), and `EligibilityReport`, `EligibilityRequest`, `Pipeline`, `PipelineCreate`, `PipelineList`, `PipelineRun`, `PipelineRunList`, `PipelineRunRequest` from the models import. In the module docstring, drop the clause "while ``/api/v1/slicer-pipelines/`` wraps its rows in ``{"pipelines": [...]}``" and end the sentence after "lists".

`backend/scadbuddy/bambuddy/models.py`: delete the thirteen names listed under **Produces**.

`backend/tests/bambuddy/test_client.py`:
- `test_folders_and_links_are_bare_lists_and_pipelines_are_wrapped` becomes `test_folders_and_links_are_bare_lists`, without the `slicer-pipelines/` mock and the `pipelines()` assertion;
- delete `test_run_pipeline_passes_copies_and_force`.

`backend/tests/bambuddy/test_client_print_workflow.py`:
- delete `test_a_configured_pipeline_carries_its_target_and_fanout`, `test_creating_a_pipeline_sends_only_the_create_schemas_fields`, `test_check_eligibility_returns_the_report_rather_than_raising`, `test_a_class_targeted_report_keeps_the_per_printer_detail`, `test_an_unknown_issue_kind_still_parses`, `test_a_run_reports_the_queue_entry_and_printer_of_every_copy`, `test_the_runs_list_is_wrapped_and_carries_a_total`, `test_reading_one_pipeline_returns_its_presets_and_target`, and any section heading left empty;
- in the `test_every_new_call_names_the_scope_its_refusal_needs` parameter list, delete the four entries for `c.pipeline(1)`, `c.check_eligibility(...)`, `c.pipeline_runs(1)` and `c.create_pipeline(...)`;
- drop `EligibilityRequest`, `PipelineCreate` and `PipelineRunRequest` from the imports (and `PresetRef` if ruff reports it unused).

Delete the five recording files:

```bash
git rm backend/tests/bambuddy/recordings/slicer-pipeline.json \
  backend/tests/bambuddy/recordings/slicer-pipelines.json \
  backend/tests/bambuddy/recordings/slicer-pipelines-configured.json \
  backend/tests/bambuddy/recordings/pipeline-run.json \
  backend/tests/bambuddy/recordings/pipeline-runs.json
```

First confirm nothing still reads them: `grep -rn "slicer-pipeline\|pipeline-run" backend/tests --include=*.py` must print nothing.

In `backend/tests/bambuddy/recordings/README.md`, delete every entry that names one of the five files, and the two notes on `SlicerPipelineCreate` and `check-eligibility` (around lines 115-119). Leave `openapi/scadbuddy-routes.json` and `openapi/routes.txt` untouched: they record Bambuddy's schema, and `test_options.py` reads the former.

- [ ] **Step 5: Run the backend gates and prove the schema did not move**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest`
Expected: PASS.

Run: `cd backend && uv run --frozen python -m scadbuddy.tools.export_openapi && git diff --exit-code openapi.json`
Expected: exit 0 (no schema change).

Run: `grep -rni pipeline backend/scadbuddy --include=*.py`
Expected: only the `bambuddy/pipelines.py` module name in imports, history comments (`catalogue.py`, `dispatch.py`, `filaments.py`, `resolver.py`, `hardware.py`, `choices.py`, `models.py:530`, `settings_store.py` docstring on legacy keys, `outputs.py` legacy validator, `progress.py` docstring), and `render/jobs.py`'s "render pipeline". Nothing live.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/bambuddy/client.py backend/scadbuddy/bambuddy/models.py \
  backend/scadbuddy/bambuddy/errors.py backend/tests/bambuddy/test_client.py \
  backend/tests/bambuddy/test_client_print_workflow.py \
  backend/tests/bambuddy/recordings/README.md
git commit -F - <<'EOF'
refactor(bambuddy): remove the slicer-pipeline client surface (#312)

pipelines, pipeline, create_pipeline, check_eligibility, run_pipeline,
pipeline_run and pipeline_runs lost their last callers with the send bar's
queue mode, the pipeline progress route and the Settings pipeline, and go
with their models. A Bambuddy 409 is still passed through as a 409 with its
body, now typed bambuddy-conflict rather than pipeline-ineligible, since
Bambuddy documents no 409 and a live call answering one is still a conflict.

Deleted recordings: slicer-pipeline.json, slicer-pipelines.json,
slicer-pipelines-configured.json, pipeline-run.json, pipeline-runs.json.

Deleted tests:
- test_client.py: test_run_pipeline_passes_copies_and_force,
  test_a_409_carries_the_eligibility_report_verbatim (replaced by
  test_a_409_passes_bambuddys_body_through)
- test_client_print_workflow.py: test_a_configured_pipeline_carries_its_target_and_fanout,
  test_creating_a_pipeline_sends_only_the_create_schemas_fields,
  test_check_eligibility_returns_the_report_rather_than_raising,
  test_a_class_targeted_report_keeps_the_per_printer_detail,
  test_an_unknown_issue_kind_still_parses,
  test_a_run_reports_the_queue_entry_and_printer_of_every_copy,
  test_the_runs_list_is_wrapped_and_carries_a_total,
  test_reading_one_pipeline_returns_its_presets_and_target, and four scope
  cases for the removed calls

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0159GufZGG4bJoXvBkEJ2S16
EOF
```

---

### Task 8: Docs: user guide, spec supersede note, plugin skill, README

**Files:**
- Modify: `docs/user-guide.md:170-186,226-229,236-239,252`
- Modify: `docs/superpowers/specs/2026-09-27-spool-first-print-design.md:28-36`
- Modify: `plugins/scadbuddy/skills/print/SKILL.md:127-130,157-159,168-169`
- Modify: `README.md:34`

**Interfaces:** none.

- [ ] **Step 1: User guide**

`docs/user-guide.md`:
- scope table row: `| **Manage Queue** | queueing prints and running slicer pipelines |` becomes `| **Manage Queue** | queueing prints from the print picker |`;
- delete the paragraph under the table that starts "Which scope Bambuddy checks for `/slicer-pipelines/` hasn't been confirmed" and ends "…names the scope that call asked for.";
- step 3 becomes: `3. Under **Where files go**, choose the library folder and the printer. **Send to Bambuddy** lays its upload out for that printer's plate, and the print picker opens on it.`
- the **Send to Bambuddy** bullet under "Customizing" becomes:

```markdown
- **Send to Bambuddy** uploads the output to the library, laid out for the printer set
  in Settings. It doesn't slice or queue; use **Print** for that. If ScadBuddy's own URL
  is set, the library file gets an "Edit in ScadBuddy" link that opens these parameters
  again.
```

- the print picker intro's last sentence ("There is no slicer pipeline to pick or maintain here — pipelines still exist in Bambuddy, and the one-click send bar and Settings' default pipeline still use one, until #312.") becomes "There is no slicer pipeline to pick or maintain. Pipelines still exist in Bambuddy, but ScadBuddy doesn't use them, and **Print** is the only way it prints."
- the **Filament** bullet's "the same spool-inventory picker the send bar uses" becomes "a spool-inventory picker".

- [ ] **Step 2: Spec supersede note**

In `docs/superpowers/specs/2026-09-27-spool-first-print-design.md` §0, after the paragraph ending "tracked as its own follow-up, eh-homelab/ScadBuddy#312.", add:

```markdown
**Superseded by #312 (2026-09-28): the send bar no longer queues.** `POST
/outputs/{id}/send` only uploads the 3MF to the library, laid out for the Settings
printer, and attaches the edit link. Its queue mode, copies and print options are gone,
and so are Settings' default pipeline, the raw slicer-preset settings, the pipeline
progress route and ScadBuddy's Bambuddy pipeline client calls. The print dialog's run
(§4) is the only path that prints. The amendment 2 paragraph above describes the state
before #312.
```

- [ ] **Step 3: Plugin skill and README**

`plugins/scadbuddy/skills/print/SKILL.md`:
- replace the paragraph at lines ~127-130 with:

```markdown
The simpler **send** path, `POST /api/v1/outputs/{output_id}/send` with
`{mode: "library"}`, only uploads the 3MF to the library folder and attaches the
edit link. It never slices or queues; `mode: "queue"` is refused with a 422
(`backend/openapi.json`, `SendRequest`; spool-first spec §0, the #312 note). To
print, use the run above. Sending is outward too.
```

- in §6, delete the sentence "For a send-bar pipeline run, **`status` and the copy counters don't move** on a failure, so don't read them as "still printing". The run's `completed_at` is what settles it (print-flow spec §6)." Keep "`settled` says when to stop." and the all-plates sentence.
- in §7, "Sending needs **Manage Library** and **Manage Queue**, and listing printers needs **Read Status** (main spec §7)." becomes "Sending needs **Manage Library**, printing also needs **Manage Queue**, and listing printers needs **Read Status** (main spec §7)."

`README.md:34`: `- **Send to Bambuddy**: upload to a library folder, or slice and queue it.` becomes `- **Send to Bambuddy**: upload to a library folder; printing is the print picker's job.`

- [ ] **Step 4: Verify**

Run: `.github/scripts/lint-plugin.sh`
Expected: exit 0 (the new send paragraph still cites `backend/openapi.json` and a spec `§`).

Run: `grep -n -i "slice and queue\|slices and queues it\|running slicer pipelines\|until #312\|send bar uses" docs/user-guide.md README.md plugins/scadbuddy/skills/print/SKILL.md`
Expected: no line describes the send bar queueing. ("slices and queues through Bambuddy" in the print picker text is correct and stays.)

If the repo's pre-commit or CI lints Markdown, run `pre-commit run --files docs/user-guide.md docs/superpowers/specs/2026-09-27-spool-first-print-design.md plugins/scadbuddy/skills/print/SKILL.md README.md`.

- [ ] **Step 5: Commit**

```bash
git add docs/user-guide.md docs/superpowers/specs/2026-09-27-spool-first-print-design.md \
  plugins/scadbuddy/skills/print/SKILL.md README.md
git commit -F - <<'EOF'
docs: the send bar only uploads; printing is the print picker's (#312)

User guide, README and the print skill describe the library-only send and drop
the Settings pipeline and the pipeline-run progress notes. The spool-first spec
gets a supersede note: the send bar no longer queues.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0159GufZGG4bJoXvBkEJ2S16
EOF
```

---

## After the last task

- Run every gate once more on the branch head (backend, regenerate + `git diff --exit-code` on the three generated files, frontend including Playwright, agent, plugin lint).
- Run the Done-when grep: `grep -rni pipeline backend/scadbuddy frontend/src --include=*.py --include=*.ts --include=*.tsx | grep -v schema.d.ts`. Every hit should be a history comment, the `pipelines.py` module name, `render pipeline`, or a test asserting no pipeline shows.
- The PR body says `Fixes #312`.

---

## Controller rulings (2026-09-28), which bind every task
- R1 (409): keep the 409 branch, rename the problem type to `bambuddy-conflict`, and pass the body through, as planned.
- R2 (old records): as planned, a before-validator on OutputMeta drops stale `print_route: "pipeline"` last-print ids. A test must load such a record.
- R3 (`SendRequest.mode`): `Literal["library"]`, and a stale `"queue"` gets a 422, as planned.
- R4 (#126 nozzle): accepted. A send-bar upload uses the placeholder nozzle. Delete `nozzle_diameter_of`.
- R5 (module name): ADD a mechanical task after Task 7 that renames `backend/scadbuddy/bambuddy/pipelines.py` to `backend/scadbuddy/bambuddy/print_run.py` and updates every import. No behavior change, gates green. After it, grepping `pipeline` in backend/scadbuddy and frontend/src must find only comments that explain history.
- R6 (recordings): delete the pipeline recordings no test reads. Git history keeps them.
- R8 (screenshot): Task 8 regenerates `docs/images/settings.png` from the msw-mocked preview with a Playwright screenshot at the same viewport as the existing image. If that proves impractical, note it in the report; do not leave the old image showing the pipeline selector without saying so.
