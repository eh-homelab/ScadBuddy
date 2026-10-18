import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { BatchChoice, PrintBatch, PrintRunRequest } from '../api/types'
import { choicesView } from '../mocks/choices'
import { libraryFiles } from '../mocks/features/library'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { BatchPrintDialog } from './BatchPrintDialog'

vi.mock('./print/PlateScene', () => ({ PlateScene: () => <div data-testid="plate-scene" /> }))

const name = (id: number) => libraryFiles.find((file) => file.id === id)?.filename ?? ''

async function newBatch(...ids: number[]): Promise<PrintBatch> {
  return api.createPrintBatch(ids.map((id) => ({ library_file_id: id, filename: name(id), sliced: id === 104 })))
}

/** Every batch-item save, in order. */
function watchItems() {
  const saved: BatchChoice[] = []
  server.events.on('request:start', async ({ request }) => {
    if (request.method === 'PUT' && /\/print\/batches\/[^/]+\/items\/\d+$/.test(new URL(request.url).pathname))
      saved.push((await request.clone().json()) as BatchChoice)
  })
  return saved
}

async function step(n: number, of: number) {
  return screen.findByRole('dialog', { name: new RegExp(`^File ${n} of ${of} — `) })
}

async function loaded() {
  await screen.findByTestId('filament-slot-1')
}

/** Slot 1's checked spool, and one that is not. */
/** Slot 1's spools, its list opened: shadcn radios are buttons, checked by `aria-checked`. */
function slot1() {
  if (screen.getByTestId('change-slot-1').getAttribute('aria-expanded') === 'false') {
    fireEvent.click(screen.getByTestId('change-slot-1'))
  }
  const radios = within(screen.getByTestId('filament-slot-1')).getAllByRole('radio') as HTMLButtonElement[]
  const isChecked = (radio: HTMLElement) => radio.getAttribute('aria-checked') === 'true'
  return { checked: radios.find(isChecked)!, other: radios.find((radio) => !isChecked(radio))! }
}

beforeEach(() => resetMockState())
afterEach(() => {
  server.events.removeAllListeners()
  vi.restoreAllMocks()
})

describe('BatchPrintDialog (#2672)', () => {
  it("carries the last file's spools to the next one when its slots are the same", async () => {
    const saved = watchItems()
    const batch = await newBatch(89, 91)
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await step(1, 2)
    await loaded()
    expect(screen.getByTestId('batch-strip')).toHaveTextContent(name(89))
    const spool = Number(slot1().other.value)
    await user.click(slot1().other)
    await user.click(screen.getByTestId('batch-next'))

    await step(2, 2)
    await loaded()
    await waitFor(() => expect(Number(slot1().checked.value)).toBe(spool))
    expect(screen.getByTestId('batch-step-0')).toHaveAttribute('data-state', 'chosen')
    expect(screen.getByTestId('batch-next')).toHaveTextContent('Review')
    await user.click(screen.getByTestId('batch-next'))

    expect(await screen.findByRole('dialog', { name: 'Review 2 files' })).toBeInTheDocument()
    expect(saved).toHaveLength(2)
    for (const choice of saved)
      expect(choice.request?.filament_plan.slots).toContainEqual({ slot_id: 1, spool_id: spool })
    expect(screen.getByTestId('batch-row-1')).toHaveTextContent(/Slot 1: /)
  })

  it('saves a choice and its next step in one write, so neither is left behind (#2718 review)', async () => {
    const saved = watchItems()
    const batch = await newBatch(89, 91)
    // The separate step route down: Next and Skip never need it.
    server.use(
      http.put(`/api/v1/print/batches/${batch.id}/step`, () =>
        HttpResponse.json({ detail: 'down' }, { status: 500 }),
      ),
    )
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await step(1, 2)
    await loaded()
    await user.click(screen.getByTestId('batch-next'))

    await step(2, 2)
    expect(saved[0]?.step).toBe(1)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('opens a file whose slots differ on its own suggestion', async () => {
    server.use(
      http.get('/api/v1/print/library/91/choices', () =>
        HttpResponse.json({
          ...choicesView,
          filaments: {
            ...choicesView.filaments,
            library_file_id: 91,
            slots: (choicesView.filaments.slots ?? []).map((slot) =>
              slot.slot_id === 1 ? { ...slot, colour: '#FFFFFF' } : slot,
            ),
          },
        }),
      ),
    )
    const batch = await newBatch(89, 91)
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await step(1, 2)
    await loaded()
    const suggested = Number(slot1().checked.value)
    await user.click(slot1().other)
    await user.click(screen.getByTestId('batch-next'))

    await step(2, 2)
    await loaded()
    expect(Number(slot1().checked.value)).toBe(suggested)
  })

  it('flags a file the check refuses, skips one, and queues the rest with a result each', async () => {
    server.use(
      http.post('/api/v1/print/library/89/check', () =>
        HttpResponse.json({ errors: ['No spool loaded for slot 2.'], warnings: [], rack: null }),
      ),
    )
    const batch = await newBatch(89, 91, 104)
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await step(1, 3)
    await loaded()
    await screen.findByText('No spool loaded for slot 2.')
    // Flagged, not held: the review lists it, and Queue all lets the run refuse it.
    await user.click(screen.getByTestId('batch-next'))
    await step(2, 3)
    expect(screen.getByTestId('batch-step-0')).toHaveAttribute('data-state', 'flagged')
    await user.click(screen.getByTestId('batch-skip'))
    await step(3, 3)
    expect(screen.getByTestId('batch-step-1')).toHaveAttribute('data-state', 'skipped')
    expect(await screen.findByTestId('sliced-settings')).toBeInTheDocument()
    await loaded()
    await user.click(screen.getByTestId('batch-next'))

    await screen.findByRole('dialog', { name: 'Review 3 files' })
    expect(screen.getByTestId('batch-row-0')).toHaveTextContent('⚠ No spool loaded for slot 2.')
    expect(screen.getByTestId('batch-row-1')).toHaveTextContent('Skipped')
    expect(screen.getByTestId('batch-queue-all')).toHaveTextContent('Queue all (2)')
    expect(screen.getByTestId('batch-queue-all')).toHaveAttribute('data-agent-user-only')
    // An open review offers Cancel and Queue all, never Done, and its footer is only buttons (#2718 review).
    const footer = (name: string) => screen.getByRole('dialog', { name }).querySelector('[data-slot="dialog-footer"]')!
    expect(within(footer('Review 3 files') as HTMLElement).queryByRole('button', { name: 'Done' })).toBeNull()
    expect(within(footer('Review 3 files') as HTMLElement).getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
    expect(footer('Review 3 files')).not.toHaveTextContent('queued ?')
    await user.click(screen.getByTestId('batch-queue-all'))

    await screen.findByRole('dialog', { name: 'Batch print queued' })
    // A queued one offers only Done.
    expect(
      within(footer('Batch print queued') as HTMLElement)
        .getAllByRole('button')
        .map((button) => button.textContent),
    ).toEqual(['Done'])
    await waitFor(() => expect(screen.getByTestId('batch-outcome-0')).toHaveTextContent('Queued in Bambuddy.'))
    await waitFor(() => expect(screen.getByTestId('batch-outcome-2')).toHaveTextContent('Queued in Bambuddy.'))
    expect(screen.queryByTestId('batch-outcome-1')).toBeNull()
  })

  it('marks a sliced file that does not fit as warned, never flagged, and queues it (#2671)', async () => {
    const warning = 'Sliced for a 0.6 mm nozzle; 0.4 mm is mounted on H2C. Swap it before the print starts.'
    server.use(
      http.post('/api/v1/print/library/104/check', () =>
        HttpResponse.json({
          errors: [],
          warnings: [{ kind: 'sliced-nozzle-missing', slot_id: null, message: warning }],
          rack: null,
        }),
      ),
    )
    const batch = await newBatch(104, 89)
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await step(1, 2)
    await loaded()
    await screen.findByText(warning)
    await waitFor(() => expect(screen.getByTestId('batch-next')).toBeEnabled())
    await user.click(screen.getByTestId('batch-next'))
    await step(2, 2)
    expect(screen.getByTestId('batch-step-0')).toHaveAttribute('data-state', 'warned')
    expect(screen.getByTestId('batch-step-0')).toHaveAccessibleName(/chosen, with a warning/)
    await loaded()
    await user.click(screen.getByTestId('batch-next'))

    await screen.findByRole('dialog', { name: 'Review 2 files' })
    expect(screen.getByTestId('batch-row-0')).toHaveTextContent(`! ${warning}`)
    expect(screen.getByTestId('batch-row-0')).not.toHaveTextContent('⚠')
    expect(screen.getByTestId('batch-queue-all')).toHaveTextContent('Queue all (2)')
    await user.click(screen.getByTestId('batch-queue-all'))
    // The run is followed after Queue all answers: "Queueing…" until it is read.
    await waitFor(() => expect(screen.getByTestId('batch-outcome-0')).toHaveTextContent('Queued in Bambuddy.'))
  })

  it('lists what a step chose over the template, and says an edited refusal is of other choices (#3798, #3799)', async () => {
    const batch = await newBatch(89, 91)
    await api.putPrintBatchItem(batch.id, 0, {
      request: {
        ...REQUEST,
        supports: { enabled: true, type: 'tree', threshold_angle: 25, on_build_plate_only: true },
        prime_tower: { enabled: false },
        print_sequence: 'by object',
        filament_profiles: ['silk-pla'],
      },
      skipped: false,
      removed: false,
    })
    await api.putPrintBatchItem(batch.id, 1, { request: { ...REQUEST }, skipped: false, removed: false })
    await api.putPrintBatchStep(batch.id, 2)
    const read = (await api.getPrintBatch(batch.id)) as PrintBatch
    server.use(
      http.get(`/api/v1/print/batches/${batch.id}`, () =>
        HttpResponse.json({
          ...read,
          items: [
            read.items[0],
            {
              ...read.items[1],
              outcome: { status: 'refused', run_id: null, detail: 'No spool for slot 2.', attempt: 0, stale: true },
            },
          ],
        }),
      ),
    )
    renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await screen.findByRole('dialog', { name: 'Review 2 files' })

    expect(screen.getByTestId('batch-overrides-0')).toHaveTextContent(
      'Tree supports, 25° threshold, on the build plate only · No prime tower · By object · Filament profile: silk-pla',
    )
    expect(screen.queryByTestId('batch-overrides-1')).toBeNull()
    const edited = screen.getByTestId('batch-outcome-1')
    expect(edited).toHaveTextContent('Edited since it was refused (No spool for slot 2.). Queue all sends the new choices.')
    expect(edited).not.toHaveTextContent('Not queued')
  })

  it("reports a refused file beside the queued ones, and one refusal stops nothing", async () => {
    const batch = await newBatch(89, 91)
    await api.putPrintBatchItem(batch.id, 0, { request: { ...REQUEST }, skipped: false, removed: false })
    await api.putPrintBatchItem(batch.id, 1, { request: { ...REQUEST }, skipped: false, removed: false })
    await api.putPrintBatchStep(batch.id, 2)
    server.use(
      http.post(`/api/v1/print/batches/${batch.id}/queue`, async () => {
        const read = await (await fetch(`/api/v1/print/batches/${batch.id}`)).json()
        const done = read as PrintBatch
        return HttpResponse.json({
          ...done,
          status: 'open',
          items: [
            { ...done.items[0], outcome: { status: 'refused', run_id: null, detail: 'This file was sliced for a X1C.' } },
            { ...done.items[1], outcome: { status: 'unchosen', run_id: null, detail: 'No choices were made for this file.' } },
          ],
        })
      }),
    )
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await user.click(await screen.findByTestId('batch-queue-all'))
    await waitFor(() => expect(screen.getByTestId('batch-outcome-0')).toHaveTextContent('Not queued: This file was sliced for a X1C.'))
    expect(screen.getByTestId('batch-outcome-1')).toHaveTextContent('Not queued: No choices were made')
    // Open still: a refused file can be edited and the batch sent again.
    expect(within(screen.getByTestId('batch-row-0')).getByRole('button', { name: /^Edit / })).toBeEnabled()
    expect(screen.getByTestId('batch-queue-all')).toBeEnabled()
  })

  it('keeps a file still being checked pending, and Queue all again follows it (#2718 review)', async () => {
    const onClose = vi.fn()
    const batch = await newBatch(89)
    await api.putPrintBatchItem(batch.id, 0, { request: { ...REQUEST }, skipped: false, removed: false })
    await api.putPrintBatchStep(batch.id, 1)
    let sends = 0
    server.use(
      http.post(`/api/v1/print/batches/${batch.id}/queue`, async () => {
        sends += 1
        const done = (await (await fetch(`/api/v1/print/batches/${batch.id}`)).json()) as PrintBatch
        const detail = 'ScadBuddy is still checking this print. Try again to follow it.'
        return HttpResponse.json(
          sends === 1
            ? { ...done, status: 'open', items: [{ ...done.items[0], outcome: { status: 'pending', run_id: null, detail } }] }
            : { ...done, status: 'queued', items: [{ ...done.items[0], outcome: { status: 'started', run_id: 'run-1', detail: null } }] },
        )
      }),
    )
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={onClose} />)
    await user.click(await screen.findByTestId('batch-queue-all'))

    await waitFor(() => expect(screen.getByTestId('batch-outcome-0')).toHaveTextContent('Still being checked'))
    const outcome = screen.getByTestId('batch-outcome-0')
    expect(outcome).not.toHaveTextContent('Not queued')
    // It may be printing: no Edit into a second print, but Queue all again is offered.
    expect(within(screen.getByTestId('batch-row-0')).queryByRole('button', { name: /^Edit / })).toBeNull()
    expect(screen.getByTestId('batch-queue-all')).toHaveTextContent('Queue all (1)')

    await user.click(screen.getByTestId('batch-queue-all'))
    await screen.findByRole('dialog', { name: 'Batch print queued' })
    expect(sends).toBe(2)
  })

  it('keeps a batch with a file still being checked when it is closed', async () => {
    const onClose = vi.fn()
    const batch = await newBatch(89)
    await api.putPrintBatchItem(batch.id, 0, { request: { ...REQUEST }, skipped: false, removed: false })
    await api.putPrintBatchStep(batch.id, 1)
    server.use(
      http.post(`/api/v1/print/batches/${batch.id}/queue`, async () => {
        const done = (await (await fetch(`/api/v1/print/batches/${batch.id}`)).json()) as PrintBatch
        return HttpResponse.json({
          ...done,
          items: [{ ...done.items[0], outcome: { status: 'pending', run_id: null, detail: 'Temporal is down.' } }],
        })
      }),
    )
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={onClose} />)
    await user.click(await screen.findByTestId('batch-queue-all'))
    await screen.findByTestId('batch-outcome-0')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledOnce()
    await expect(api.getPrintBatch(batch.id)).resolves.toMatchObject({ id: batch.id })
  })

  it('removes a file in the review, and Edit goes back to its step on its own choices', async () => {
    const batch = await newBatch(89, 91)
    await api.putPrintBatchItem(batch.id, 0, { request: { ...REQUEST }, skipped: false, removed: false })
    await api.putPrintBatchItem(batch.id, 1, { request: { ...REQUEST }, skipped: false, removed: false })
    await api.putPrintBatchStep(batch.id, 2)
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await screen.findByRole('dialog', { name: 'Review 2 files' })
    await user.click(within(screen.getByTestId('batch-row-1')).getByRole('button', { name: /^Remove / }))
    await screen.findByRole('dialog', { name: 'Review 1 files' })
    expect(screen.queryByTestId('batch-row-1')).toBeNull()
    expect(screen.getByTestId('batch-queue-all')).toHaveTextContent('Queue all (1)')

    await user.click(within(screen.getByTestId('batch-row-0')).getByRole('button', { name: /^Edit / }))
    await step(1, 1)
    await loaded()
    await waitFor(() => expect(Number(slot1().checked.value)).toBe(REQUEST.filament_plan.slots?.[0]?.spool_id))
  })

  it('keeps a step\'s supports and prime tower through Edit and Review (#2718 review)', async () => {
    const batch = await newBatch(89, 91)
    const chosen = {
      ...REQUEST,
      supports: { enabled: true, type: 'tree' as const, on_build_plate_only: true },
      prime_tower: { enabled: true, no_sparse_layers: true },
    }
    await api.putPrintBatchItem(batch.id, 0, { request: chosen, skipped: false, removed: false })
    await api.putPrintBatchItem(batch.id, 1, { request: { ...REQUEST }, skipped: false, removed: false })
    await api.putPrintBatchStep(batch.id, 2)
    const saved = watchItems()
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await screen.findByRole('dialog', { name: 'Review 2 files' })

    await user.click(within(screen.getByTestId('batch-row-0')).getByRole('button', { name: /^Edit / }))
    await step(1, 2)
    await loaded()
    await waitFor(() => expect(screen.getByTestId('batch-next')).toBeEnabled())
    await user.click(screen.getByTestId('batch-next'))

    await waitFor(() => expect(saved).toHaveLength(1))
    expect(saved[0]?.request).toMatchObject({ supports: chosen.supports, prime_tower: chosen.prime_tower })
  })

  it('resumes where it was left after a reload', async () => {
    const batch = await newBatch(89, 91)
    await api.putPrintBatchItem(batch.id, 0, { request: { ...REQUEST }, skipped: false, removed: false })
    await api.putPrintBatchStep(batch.id, 1)
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await step(2, 2)
    expect(screen.getByTestId('batch-step-0')).toHaveAttribute('data-state', 'chosen')
    expect(screen.getByTestId('batch-step-1')).toHaveAttribute('aria-current', 'step')
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await step(1, 2)
  })

  it('closes the review on Escape, as Cancel does, rather than going back a step (#2718 review)', async () => {
    const onClose = vi.fn()
    const batch = await newBatch(89, 91)
    await api.putPrintBatchStep(batch.id, 2)
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={onClose} />)
    await screen.findByRole('dialog', { name: 'Review 2 files' })
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledOnce()
    await waitFor(() => expect(api.getPrintBatch(batch.id)).rejects.toMatchObject({ status: 404 }))
  })

  it('reopens a file whose run failed after it started, to edit and queue again (#2718 review)', async () => {
    const batch = await newBatch(89)
    await api.putPrintBatchItem(batch.id, 0, { request: { ...REQUEST }, skipped: false, removed: false })
    const chosen = await api.putPrintBatchStep(batch.id, 1)
    const detail = "Bambuddy's slice failed."
    // Every run had its 202, so the batch is queued; the failure comes after (#2718 review).
    const started = {
      ...chosen,
      status: 'queued',
      items: [{ ...chosen.items[0], outcome: { status: 'started', run_id: 'run-9', detail: null } }],
    }
    let failed = false
    server.use(
      http.post(`/api/v1/print/batches/${batch.id}/queue`, () => HttpResponse.json(started)),
      http.get('/api/v1/print/runs/run-9', () => {
        failed = true
        return HttpResponse.json({
          id: 'run-9',
          subject: 'library:89',
          output_id: 'library:89',
          status: 'failed',
          created_at: '2026-10-13T00:00:00Z',
          error: { type: 'about:blank', status: 502, title: 'Bad Gateway', detail, extensions: {} },
        })
      }),
      http.get(`/api/v1/print/batches/${batch.id}`, () =>
        HttpResponse.json(
          failed
            ? { ...chosen, items: [{ ...chosen.items[0], outcome: { status: 'failed', run_id: 'run-9', detail, attempt: 0 } }] }
            : chosen,
        ),
      ),
    )
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await user.click(await screen.findByTestId('batch-queue-all'))

    // The server opens the batch again for the failed file: the dialog follows it.
    await screen.findByRole('dialog', { name: 'Review 1 files' })
    const row = screen.getByTestId('batch-row-0')
    await waitFor(() => expect(within(row).getByRole('button', { name: /^Edit / })).toBeEnabled())
    expect(screen.getByTestId('batch-outcome-0')).toHaveTextContent(`Not queued: ${detail}`)
    expect(screen.getByTestId('batch-queue-all')).toHaveTextContent('Queue all (1)')
  })

  it('follows a Queue all that answers before its last file, from where it stopped (#2951)', async () => {
    const batch = await newBatch(89, 91)
    for (const position of [0, 1])
      await api.putPrintBatchItem(batch.id, position, { request: { ...REQUEST }, skipped: false, removed: false })
    const chosen = await api.putPrintBatchStep(batch.id, 2)
    const started = (n: number) => ({ status: 'started', run_id: `run-${n}`, detail: null, attempt: 0 })
    const sent: (string | null)[] = []
    server.use(
      http.post(`/api/v1/print/batches/${batch.id}/queue`, ({ request }) => {
        const from = new URL(request.url).searchParams.get('from_position')
        sent.push(from)
        if (from === null)
          return HttpResponse.json(
            { batch: { ...chosen, items: [{ ...chosen.items[0], outcome: started(0) }, chosen.items[1]] }, next_position: 1 },
            { status: 202 },
          )
        return HttpResponse.json({
          ...chosen,
          status: 'queued',
          items: chosen.items.map((item, n) => ({ ...item, outcome: started(n) })),
        })
      }),
      http.get('/api/v1/print/runs/:id', ({ params }) =>
        HttpResponse.json({
          id: params.id,
          subject: 'library:89',
          output_id: 'library:89',
          status: 'running',
          created_at: '2026-10-13T00:00:00Z',
        }),
      ),
    )
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await user.click(await screen.findByTestId('batch-queue-all'))

    await screen.findByRole('dialog', { name: 'Batch print queued' })
    expect(sent).toEqual([null, '1'])
  })

  it('stops following a Queue all once the dialog is gone (#2718 review)', async () => {
    const batch = await newBatch(89, 91)
    for (const position of [0, 1])
      await api.putPrintBatchItem(batch.id, position, { request: { ...REQUEST }, skipped: false, removed: false })
    const chosen = await api.putPrintBatchStep(batch.id, 2)
    const sent: (string | null)[] = []
    let answer: () => void = () => {}
    const answered = new Promise<void>((resolve) => (answer = resolve))
    server.use(
      http.post(`/api/v1/print/batches/${batch.id}/queue`, async ({ request }) => {
        sent.push(new URL(request.url).searchParams.get('from_position'))
        await answered
        return HttpResponse.json({ batch: chosen, next_position: 1 }, { status: 202 })
      }),
    )
    const { user, unmount } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await user.click(await screen.findByTestId('batch-queue-all'))
    await waitFor(() => expect(sent).toEqual([null]))

    unmount()
    answer()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(sent).toEqual([null])
  })

  it("saves a step's choices when it is left through the strip (#2718 review)", async () => {
    const saved = watchItems()
    const batch = await newBatch(89, 91)
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await step(1, 2)
    await loaded()
    const spool = Number(slot1().other.value)
    await user.click(slot1().other)

    await user.click(screen.getByTestId('batch-step-1'))

    await step(2, 2)
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(saved[0]).toMatchObject({ step: 1, skipped: false })
    expect(saved[0]?.request?.filament_plan?.slots).toContainEqual(expect.objectContaining({ slot_id: 1, spool_id: spool }))
    expect(screen.getByTestId('batch-step-0')).toHaveAttribute('data-state', 'chosen')
  })

  it("leaves a started file's step through the strip without saving it (#2718 review)", async () => {
    const saved = watchItems()
    const batch = await newBatch(89, 91)
    const started = {
      ...batch,
      items: [
        { ...batch.items[0], request: { ...REQUEST }, outcome: { status: 'started', run_id: 'run-0', detail: null, attempt: 0 } },
        { ...batch.items[1], request: { ...REQUEST }, outcome: { status: 'refused', run_id: null, detail: 'No.', attempt: 0 } },
      ],
    }
    let step0 = 0
    server.use(
      http.get(`/api/v1/print/batches/${batch.id}`, () => HttpResponse.json({ ...started, step: step0 })),
      http.put(`/api/v1/print/batches/${batch.id}/step`, async ({ request }) => {
        step0 = ((await request.json()) as { step: number }).step
        return HttpResponse.json({ ...started, step: step0 })
      }),
      http.put(`/api/v1/print/batches/${batch.id}/items/0`, () =>
        HttpResponse.json({ detail: "This file's print has started, or may have, so its choices cannot change." }, { status: 409 }),
      ),
      http.get('/api/v1/print/runs/:id', ({ params }) =>
        HttpResponse.json({ id: params.id, subject: 'library:89', output_id: 'library:89', status: 'running', created_at: '2026-10-13T00:00:00Z' }),
      ),
    )
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={vi.fn()} />)
    await step(1, 2)
    await loaded()

    await user.click(screen.getByTestId('batch-step-1'))

    await step(2, 2)
    expect(saved).toEqual([])
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('forgets the batch on Cancel', async () => {
    const onClose = vi.fn()
    const batch = await newBatch(89, 91)
    const { user } = renderPage(<BatchPrintDialog batchId={batch.id} onClose={onClose} />)
    await step(1, 2)
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledOnce()
    await waitFor(() => expect(api.getPrintBatch(batch.id)).rejects.toMatchObject({ status: 404 }))
  })
})

/** A choice as a step saves one: slot 1 on a spool the mock's inventory has. */
const REQUEST: PrintRunRequest = {
  printer_id: choicesView.printer_id ?? null,
  filament_plan: {
    slots: (choicesView.filaments.suggested ?? []).map((choice, at) =>
      at === 0
        ? {
            ...choice,
            spool_id:
              (choicesView.filaments.spools ?? []).find((spool) => spool.spool_id !== choice.spool_id)?.spool_id ??
              choice.spool_id,
          }
        : choice,
    ),
    force_colour_match: false,
  },
  choices: { nozzles: [{ size: '0.4', flow: 'standard' }], tier: 'standard', bed_type: 'Textured PEI Plate' },
  plate_id: 1,
  all_plates: false,
}
