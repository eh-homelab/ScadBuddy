import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, printRunPoll, rackAlgorithmSave } from '../api/client'
import type { AnalysisRequest, Output, PrintRunResult } from '../api/types'
import { analysisReport, openEdgesDiagnostic } from '../mocks/analyzers'
import { choicesView, queuedResult } from '../mocks/choices'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { PrintPicker } from './PrintPicker'

const output = fixtures.outputs[0] as Output

function renderPicker(
  props: {
    onClose?: () => void
    onRan?: (result: PrintRunResult) => void
    onPrinterModel?: (model: string | null) => void
  } = {},
) {
  return renderPage(
    <PrintPicker
      open
      source={{ kind: 'output', output }}
      onClose={props.onClose ?? vi.fn()}
      onRan={props.onRan ?? vi.fn()}
      onPrinterModel={props.onPrinterModel}
    />,
  )
}

/** The picker as a page holds it: Cancel really closes it, and Reopen opens it again. */
function renderReopenable() {
  function Reopenable() {
    const [open, setOpen] = useState(true)
    return (
      <>
        <button type="button" onClick={() => setOpen(true)}>
          Reopen
        </button>
        <PrintPicker
          open={open}
          source={{ kind: 'output', output }}
          onClose={() => setOpen(false)}
          onRan={vi.fn()}
        />
      </>
    )
  }
  return renderPage(<Reopenable />)
}

/** The dialog has read its choices once the Advanced switch and the spools are on screen. */
async function loaded() {
  await screen.findByRole('switch', { name: 'Advanced' })
  await screen.findByTestId('filament-slot-1')
}

/** #768 — nozzles, quality, plate type, options, project and copies are Advanced steps. */
async function showAdvanced() {
  fireEvent.click(screen.getByRole('switch', { name: 'Advanced' }))
  await screen.findByRole('group', { name: /nozzles/i })
}

/** Every request body of one kind under `/print/`, in order (`/analyzers/run` is not one). */
function watch(method: string, suffix: string, prefix = '/api/v1/print/') {
  const bodies: Record<string, unknown>[] = []
  const urls: string[] = []
  server.events.on('request:start', async ({ request }) => {
    const path = new URL(request.url).pathname
    if (request.method === method && path.startsWith(prefix) && path.endsWith(suffix)) {
      urls.push(request.url)
      if (method !== 'GET') bodies.push((await request.clone().json()) as Record<string, unknown>)
    }
  })
  return { bodies, urls }
}

beforeEach(() => resetMockState())
afterEach(() => {
  server.events.removeAllListeners()
  vi.restoreAllMocks()
})

describe('PrintPicker', () => {
  it('opens in Simple mode on the spools, the checks and Print alone, with no pipeline list (#768)', async () => {
    renderPicker()
    await loaded()
    expect(screen.getByRole('switch', { name: 'Advanced' })).toHaveAttribute('aria-checked', 'false')
    expect(screen.queryByRole('group', { name: /nozzles/i })).toBeNull()
    expect(screen.queryByRole('group', { name: /quality/i })).toBeNull()
    expect(screen.queryByLabelText('Plate')).toBeNull()
    expect(screen.queryByLabelText('Copies')).toBeNull()
    expect(screen.queryByTestId('project-select')).toBeNull()
    expect(screen.queryByText('Options')).toBeNull()
    expect(screen.getByTestId('print-checks')).toBeInTheDocument()
    expect(screen.getByTestId('run-print')).toBeEnabled()
    expect(screen.queryByTestId('run-pipeline')).toBeNull()
    expect(screen.queryByText(/pipeline/i)).toBeNull()
  })

  it('marks Print user-only, since it queues a physical print', async () => {
    renderPicker()
    await loaded()
    expect(screen.getByTestId('run-print')).toHaveAttribute('data-agent-user-only')
  })

  it('sends the choices and the spool plan in one run request', async () => {
    const run = vi.spyOn(api, 'runPrint').mockResolvedValue(queuedResult)
    renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.click(screen.getByRole('radio', { name: /0\.2 mm/i }))
    fireEvent.click(screen.getByRole('button', { name: /^Print$/ }))
    await waitFor(() => expect(run).toHaveBeenCalled())
    const [, body] = run.mock.calls[0]!
    expect(body.choices).toMatchObject({ nozzles: [{ size: '0.2' }, { size: '0.2' }], tier: 'standard' })
    expect(body.filament_plan.slots?.length).toBeGreaterThan(0)
    expect(body).not.toHaveProperty('pipeline_id')
  })

  /** The dialog on printer 1 as if it had no track switch, so each AMS is wired to a side. */
  function unswitched() {
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', () =>
        HttpResponse.json({
          ...choicesView,
          filaments: { ...choicesView.filaments, track_switch: false },
        }),
      ),
    )
  }

  it('rules out no spool for the nozzle size, and keeps the suggestion (#768)', async () => {
    unswitched()
    renderPicker()
    await loaded()
    await showAdvanced()
    const slot = screen.getByTestId('filament-slot-2')
    // Spool 9 feeds the right extruder (the 0.2), spool 22 the left (the 0.4).
    expect(within(slot).getByTestId('spool-9')).toBeEnabled()
    fireEvent.click(screen.getByRole('radio', { name: /0\.2 mm/i }))
    expect(within(slot).getByTestId('spool-22')).toBeEnabled()
    expect(within(screen.getByTestId('filament-slot-1')).getByTestId('spool-21')).toBeChecked()
  })

  it('disables Print and names the slot when a spool has no preset for the size', async () => {
    vi.spyOn(api, 'runPrint').mockRejectedValue(
      new ApiError(422, 'Generic TPU has no slicer preset for a 0.2 mm nozzle. Pick one under Advanced.'),
    )
    renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.click(screen.getByRole('radio', { name: /0\.2 mm/i }))
    fireEvent.click(screen.getByRole('button', { name: /^Print$/ }))
    expect(await screen.findByText(/no slicer preset for a 0\.2 mm nozzle/)).toBeInTheDocument()
    // Still open, and Print waits for a change rather than repeating the same refusal.
    expect(screen.getByRole('dialog', { name: 'Print' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('radio', { name: /0\.4 mm/i }))
    expect(screen.queryByText(/no slicer preset for a 0\.2 mm nozzle/)).toBeNull()
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled()
  })

  it('shows the Advanced process list and per-slot preset override only when toggled', async () => {
    renderPicker()
    expect(screen.queryByLabelText(/process/i)).toBeNull()
    fireEvent.click(await screen.findByRole('switch', { name: /advanced/i }))
    expect(screen.getByLabelText(/process/i)).toBeInTheDocument()
  })

  it('sends the whole run request, with the plan the server suggested', async () => {
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()

    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')

    expect(bodies).toHaveLength(1)
    expect(bodies[0]).toEqual({
      printer_id: 1,
      filament_plan: {
        slots: [
          { slot_id: 1, spool_id: 21 },
          { slot_id: 2, spool_id: 27 },
        ],
        force_colour_match: false,
      },
      choices: {
        nozzles: [
          { size: '0.4', flow: 'standard' },
          { size: '0.4', flow: 'standard' },
        ],
        tier: 'standard',
        process_name: null,
        bed_type: 'Textured PEI Plate',
        filament_overrides: {},
      },
      plate_id: 1,
      all_plates: false,
      project_id: null,
      options: {},
      rack_position: null,
      // Not chosen in this dialog, so the backend applies the printer's remembered one.
      rack_algorithm: null,
      request_id: expect.stringMatching(/^[0-9a-f]{32}$/),
    })
  })

  it('sends a named process and a per-slot preset in Advanced mode', async () => {
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()

    await user.click(screen.getByRole('switch', { name: /advanced/i }))
    await user.selectOptions(screen.getByLabelText('Process'), '0.24mm Standard @BBL H2C')
    // Only the presets the chosen size takes are on offer.
    const override = screen.getByLabelText('Preset for slot 1')
    expect(within(override).queryByText(/0\.2 nozzle/)).toBeNull()
    await user.selectOptions(override, 'cloud:GFSB00_22')
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')

    expect(bodies[0]).toMatchObject({
      choices: {
        tier: null,
        process_name: '0.24mm Standard @BBL H2C',
        filament_overrides: { '1': { source: 'cloud', id: 'GFSB00_22' } },
      },
    })
  })

  it('drops the per-slot presets when the nozzle size changes', async () => {
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()

    await user.click(screen.getByRole('switch', { name: /advanced/i }))
    await user.selectOptions(screen.getByLabelText('Preset for slot 1'), 'cloud:GFSB00_22')
    await user.click(screen.getByRole('radio', { name: /0\.2 mm/i }))
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')

    expect(bodies[0]).toMatchObject({ choices: { filament_overrides: {} } })
  })

  it('drops a per-slot preset override when the output changes under an open dialog', async () => {
    const first = fixtures.outputs[0] as Output
    const second = fixtures.outputs[1] as Output
    const { bodies } = watch('POST', '/run')
    const { user, rerender } = renderPage(
      <PrintPicker open source={{ kind: 'output', output: first }} onClose={vi.fn()} onRan={vi.fn()} />,
    )
    await loaded()

    await user.click(screen.getByRole('switch', { name: /advanced/i }))
    await user.selectOptions(screen.getByLabelText('Preset for slot 1'), 'cloud:GFSB00_22')

    rerender(<PrintPicker open source={{ kind: 'output', output: second }} onClose={vi.fn()} onRan={vi.fn()} />)
    await loaded()

    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')

    const body = bodies[0] as { choices: { filament_overrides: Record<string, unknown> } }
    expect(body.choices.filament_overrides).toEqual({})
  })

  it('shows the run’s warnings once the print is queued', async () => {
    vi.spyOn(api, 'runPrint').mockResolvedValue({
      ...queuedResult,
      warnings: [
        {
          kind: 'hf-unsupported',
          message:
            "Bambuddy slices this as Standard flow; High Flow presets aren't supported by Bambuddy yet.",
        },
        {
          kind: 'plate-differs',
          message: "The 3DP-31B-598's last print used Engineering Plate. Swap to Textured PEI Plate.",
        },
      ],
    })
    const onRan = vi.fn()
    const { user } = renderPicker({ onRan })
    await loaded()
    await showAdvanced()
    await user.click(screen.getByRole('button', { name: /^Print$/ }))

    const warnings = await screen.findByTestId('run-warnings')
    expect(warnings).toHaveTextContent('High Flow presets')
    expect(warnings).toHaveTextContent('Swap to Textured PEI Plate')
    expect(screen.getByTestId('queued-items')).toHaveTextContent('Sliced and queued for 3DP-31B-598')
    expect(onRan).toHaveBeenCalledTimes(1)
  })

  it('shows why the dialog cannot open when the choices cannot be read', async () => {
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Bad Gateway', status: 502, detail: 'Bambuddy refused the API key' },
          { status: 502, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    renderPicker()

    expect(await screen.findByRole('alert')).toHaveTextContent('Bambuddy refused the API key')
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeDisabled()
  })

  it('reads the choices again on Retry after a failed read (#482)', async () => {
    let reads = 0
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', () => {
        reads += 1
        // Returning nothing falls through to the default handler, so the retry reads
        // real choices.
        if (reads > 1) return undefined
        return HttpResponse.json(
          { type: 'about:blank', title: 'Gateway Timeout', status: 504, detail: 'Bambuddy did not answer' },
          { status: 504, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
    )
    const { user } = renderPicker()

    expect(await screen.findByRole('alert')).toHaveTextContent('Bambuddy did not answer')
    await user.click(screen.getByRole('button', { name: 'Retry' }))

    await loaded()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(reads).toBe(2)
  })
})

describe('PrintPicker · Nozzle verdict (#755)', () => {
  // #768: the run checks no mounted nozzle any more; the verdict still shows what the
  // check answers.
  const refusal = "The two nozzles are different sizes. Bambuddy can't slice mixed nozzle sizes yet."

  it('says before Print what the run would refuse for the nozzles, and holds Print', async () => {
    const checks = watch('POST', '/check')
    const run = vi.spyOn(api, 'runPrint')
    server.use(
      http.post('/api/v1/print/outputs/:id/check', async ({ request }) => {
        const body = (await request.json()) as { choices: { nozzles: { size: string }[] } }
        const errors = body.choices.nozzles[0]?.size === '0.2' ? [refusal] : []
        return HttpResponse.json({ errors, warnings: [] })
      }),
    )
    const { user } = renderPicker()
    await loaded()
    expect(screen.queryByTestId('print-verdict-error')).toBeNull()

    await showAdvanced()
    await user.click(screen.getByRole('radio', { name: /0\.2 mm/i }))

    const error = await screen.findByTestId('print-verdict-error')
    expect(error).toHaveTextContent(refusal)
    expect(within(screen.getByTestId('print-checks')).getByTestId('print-verdict-error')).toBe(error)
    expect(screen.getByTestId('run-print')).toBeDisabled()
    expect(run).not.toHaveBeenCalled()
    const last = checks.bodies.at(-1) as { choices: unknown; filament_plan: unknown; printer_id: unknown }
    expect(last.choices).toMatchObject({ nozzles: [{ size: '0.2' }, { size: '0.2' }] })
    expect(last.filament_plan).toMatchObject({ slots: expect.any(Array) })
    expect(last.printer_id).toBe(choicesView.printer_id)
  })

  it('lists the nozzle advisories without holding Print', async () => {
    server.use(
      http.post('/api/v1/print/outputs/:id/check', () =>
        HttpResponse.json({
          errors: [],
          warnings: [
            {
              kind: 'plate-differs',
              message: "The 3DP-31B-598's last print used Engineering Plate. Swap to Textured PEI Plate.",
            },
          ],
        }),
      ),
    )
    renderPicker()
    await loaded()

    expect(await screen.findByTestId('print-verdict-warning')).toHaveTextContent(
      'Swap to Textured PEI Plate',
    )
    expect(screen.queryByTestId('print-verdict-error')).toBeNull()
    expect(screen.getByTestId('run-print')).toBeEnabled()
  })
  it('shows the mounted High Flow warning in Simple and Advanced, and never holds Print on it (#797)', async () => {
    const highFlow =
      'The left nozzle is High Flow and this print is sliced for Standard flow, so if it ' +
      'prints on the left, the printer pauses at the first layer.'
    server.use(
      http.post('/api/v1/print/outputs/:id/check', () =>
        HttpResponse.json({
          errors: [],
          warnings: [
            { kind: 'hf-mounted', message: highFlow },
            { kind: 'not-installed', message: 'No 0.6 mm nozzle is installed. Install one before this prints.' },
          ],
        }),
      ),
    )
    renderPicker()
    await loaded()
    // Simple mode: the mounted High Flow warning, and no nozzle-step note.
    const simple = await screen.findAllByTestId('print-verdict-warning')
    expect(simple).toHaveLength(1)
    expect(simple[0]).toHaveTextContent('The left nozzle is High Flow')
    expect(screen.queryByText(/No 0.6 mm nozzle is installed/)).toBeNull()
    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())

    await showAdvanced()
    const advanced = await screen.findAllByTestId('print-verdict-warning')
    expect(advanced[0]).toHaveTextContent('The left nozzle is High Flow')
    expect(advanced).toHaveLength(2)
    expect(screen.getByTestId('run-print')).toBeEnabled()
  })

  it('says when the check before Print could not run, and reads it again on request', async () => {
    let failing = true
    server.use(
      http.post('/api/v1/print/outputs/:id/check', () =>
        failing
          ? HttpResponse.json({ detail: 'Bambuddy did not answer' }, { status: 502 })
          : HttpResponse.json({ errors: [refusal], warnings: [] }),
      ),
    )
    const { user } = renderPicker()
    await loaded()

    const failed = await screen.findByTestId('print-verdict-failed')
    expect(failed).toHaveTextContent('The check before Print could not run: Bambuddy did not answer')
    expect(screen.getByTestId('run-print')).toBeEnabled()

    failing = false
    await user.click(within(failed).getByRole('button', { name: 'Check again' }))
    expect(await screen.findByTestId('print-verdict-error')).toHaveTextContent(refusal)
    expect(screen.queryByTestId('print-verdict-failed')).toBeNull()
    expect(screen.getByTestId('run-print')).toBeDisabled()
  })
})

describe('PrintPicker · Advanced and refusals (fix round 1)', () => {
  it('offers no per-slot preset override until Advanced is on', async () => {
    const { user } = renderPicker()
    await loaded()

    expect(screen.queryByLabelText('Preset for slot 1')).toBeNull()
    await user.click(screen.getByRole('switch', { name: /advanced/i }))
    expect(screen.getByLabelText('Preset for slot 1')).toBeInTheDocument()
  })

  it('puts the Advanced switch before the controls it reveals, and describes it', async () => {
    renderPicker()
    await loaded()
    await showAdvanced()

    const toggle = screen.getByRole('switch', { name: /advanced/i })
    const nozzles = screen.getByRole('group', { name: /nozzles/i })
    expect(toggle.compareDocumentPosition(nozzles) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(toggle).toHaveAccessibleDescription(/nozzle, process/i)
  })

  it('leaving Advanced resets both flows, the process and the slot presets', async () => {
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()

    await user.click(screen.getByRole('switch', { name: /advanced/i }))
    await user.click(screen.getByRole('radio', { name: 'Left High Flow' }))
    await user.click(screen.getByRole('radio', { name: 'Right High Flow' }))
    await user.selectOptions(screen.getByLabelText('Process'), '0.24mm Standard @BBL H2C')
    await user.selectOptions(screen.getByLabelText('Preset for slot 1'), 'cloud:GFSB00_22')
    await user.click(screen.getByRole('switch', { name: /advanced/i }))

    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    expect(bodies[0]).toMatchObject({
      choices: {
        nozzles: [
          { size: '0.4', flow: 'standard' },
          { size: '0.4', flow: 'standard' },
        ],
        tier: 'standard',
        process_name: null,
        filament_overrides: {},
      },
    })
  })

  it('leaves Print enabled to retry after a refusal that is not a 422', async () => {
    const run = vi
      .spyOn(api, 'runPrint')
      .mockRejectedValueOnce(new ApiError(409, 'Bambuddy refused the API key.'))
      .mockResolvedValueOnce(queuedResult)
    const { user } = renderPicker()
    await loaded()

    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Bambuddy refused the API key.')
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    expect(run).toHaveBeenCalledTimes(2)
    // #470: each press is its own print, so the server does not answer the second with
    // the first's run.
    const ids = run.mock.calls.map(([, body]) => body.request_id)
    expect(ids[0]).toMatch(/^[0-9a-f]{32}$/)
    expect(ids[1]).toMatch(/^[0-9a-f]{32}$/)
    expect(ids[0]).not.toBe(ids[1])
  })

  it('offers no Print after a failed run that had tried to queue (#470)', async () => {
    vi.spyOn(api, 'runPrint').mockRejectedValueOnce(
      new ApiError({
        title: 'Internal Server Error',
        status: 500,
        detail: 'Plate 2 failed to slice after plate 1 was queued.',
        may_have_queued: true,
      }),
    )
    const { user } = renderPicker()
    await loaded()

    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Plate 2 failed to slice after plate 1 was queued.')
    expect(alert).toHaveTextContent(
      "The print may still have been queued. Check Bambuddy's queue before printing again, or it may print twice.",
    )
    expect(screen.queryByRole('button', { name: /^Print$/ })).toBeNull()
    expect(await screen.findByRole('button', { name: "Open Bambuddy's queue" })).toBeInTheDocument()
  })

  it('keeps Print after a failed run that never tried to queue, even a Bambuddy timeout', async () => {
    vi.spyOn(api, 'runPrint').mockRejectedValueOnce(
      new ApiError({
        type: 'https://scadbuddy.dev/problems/bambuddy-unavailable',
        title: 'Gateway Timeout',
        status: 504,
        detail: 'could not reach Bambuddy to slice the plate: ReadTimeout',
        may_have_queued: false,
      }),
    )
    const { user } = renderPicker()
    await loaded()

    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('could not reach Bambuddy to slice the plate: ReadTimeout')
    expect(alert).not.toHaveTextContent('may still have been queued')
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled()
  })
})

describe('PrintPicker · A run that got no answer (#470)', () => {
  // runPrint re-sends an unanswered press (same request_id) before it gives up.
  beforeEach(() => {
    printRunPoll.intervalMs = 1
  })
  afterEach(() => {
    printRunPoll.intervalMs = 1000
  })

  function runAnswers(answer: () => Response) {
    const calls = watch('POST', '/run')
    server.use(http.post('/api/v1/print/outputs/:id/run', answer))
    return calls
  }

  it('says a proxy timeout may still have queued the print, and points at the queue', async () => {
    const opened = vi.spyOn(window, 'open').mockReturnValue(null)
    const { bodies } = runAnswers(
      () =>
        new HttpResponse('<html>upstream request timeout</html>', {
          status: 504,
          headers: { 'Content-Type': 'text/html' },
        }),
    )
    const onRan = vi.fn()
    const { user } = renderPicker({ onRan })
    await loaded()
    await user.click(screen.getByRole('button', { name: /^Print$/ }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The server took too long to answer (HTTP 504).')
    expect(alert).toHaveTextContent(
      "The print may still have been queued. Check Bambuddy's queue before printing again, or it may print twice.",
    )
    expect(alert).not.toHaveTextContent('Request failed')
    // No Print to press again: the way back to it is closing and reopening the dialog.
    expect(screen.queryByRole('button', { name: /^Print$/ })).toBeNull()
    await user.click(await screen.findByRole('button', { name: "Open Bambuddy's queue" }))
    expect(opened).toHaveBeenCalledWith(
      `${fixtures.settings.bambuddy_url}/queue`,
      expect.any(String),
      'noopener',
    )
    // One press, re-sent while unanswered: every try is the same run on the server.
    expect(bodies).toHaveLength(1 + printRunPoll.reattempts)
    expect(new Set(bodies.map((body) => body['request_id'])).size).toBe(1)
    expect(onRan).not.toHaveBeenCalled()
  })

  it('says the same when the connection drops after sending', async () => {
    runAnswers(() => HttpResponse.error())
    const { user } = renderPicker()
    await loaded()
    await user.click(screen.getByRole('button', { name: /^Print$/ }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(
      'ScadBuddy could not reach its server, or the connection dropped before it answered.',
    )
    expect(alert).toHaveTextContent('The print may still have been queued.')
    expect(screen.queryByRole('button', { name: /^Print$/ })).toBeNull()
    expect(await screen.findByRole('button', { name: "Open Bambuddy's queue" })).toBeInTheDocument()
  })

  it('says the same when the backend timed out on Bambuddy, which may have queued it', async () => {
    const { bodies } = runAnswers(() =>
      HttpResponse.json(
        {
          type: 'https://scadbuddy.dev/problems/bambuddy-unavailable',
          title: 'Gateway Timeout',
          status: 504,
          detail: 'could not reach Bambuddy to queue the print: ReadTimeout',
        },
        { status: 504, headers: { 'Content-Type': 'application/problem+json' } },
      ),
    )
    const onRan = vi.fn()
    const { user } = renderPicker({ onRan })
    await loaded()
    await user.click(screen.getByRole('button', { name: /^Print$/ }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('could not reach Bambuddy to queue the print: ReadTimeout')
    expect(alert).toHaveTextContent('The print may still have been queued.')
    expect(screen.queryByRole('button', { name: /^Print$/ })).toBeNull()
    expect(await screen.findByRole('button', { name: "Open Bambuddy's queue" })).toBeInTheDocument()
    expect(bodies).toHaveLength(1)
    expect(onRan).not.toHaveBeenCalled()
  })

  it('offers Print again once the dialog is reopened', async () => {
    runAnswers(() => new HttpResponse('timeout', { status: 524 }))
    const onClose = vi.fn()
    const { user } = renderPage(
      <PrintPicker
        open
        source={{ kind: 'output', output }}
        onClose={onClose}
        onRan={vi.fn()}
      />,
    )
    await loaded()
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByRole('button', { name: "Open Bambuddy's queue" })

    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('stays open through a run, so a reopened dialog cannot print it twice', async () => {
    let release: () => void = () => undefined
    let calls = 0
    server.use(
      http.post('/api/v1/print/outputs/:id/run', async () => {
        calls += 1
        await new Promise<void>((resolve) => (release = resolve))
        // #470: the route answers with the run, here one already finished.
        return HttpResponse.json(
          {
            id: 'run-1',
            output_id: output.id,
            status: 'succeeded',
            created_at: '2026-09-28T10:00:00Z',
            finished_at: '2026-09-28T10:00:01Z',
            result: queuedResult,
            error: null,
            may_have_queued: false,
            repeated: false,
          },
          { status: 202 },
        )
      }),
    )
    const onClose = vi.fn()
    const onRan = vi.fn()
    const { user } = renderPicker({ onClose, onRan })
    await loaded()
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await waitFor(() => expect(calls).toBe(1))

    // Escape while the print waits on a slow proxy: the dialog keeps it.
    await user.keyboard('{Escape}')
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Print/ })).toBeDisabled()

    release()
    await waitFor(() => expect(onRan).toHaveBeenCalledTimes(1))
    expect(calls).toBe(1)
  })

  it('opens the queue without a double slash when Settings has a trailing one', async () => {
    const opened = vi.spyOn(window, 'open').mockReturnValue(null)
    server.use(
      http.get('/api/v1/settings', () =>
        HttpResponse.json({ ...fixtures.settings, bambuddy_url: 'https://bambuddy.example/' }),
      ),
    )
    runAnswers(() => new HttpResponse('timeout', { status: 504 }))
    const { user } = renderPicker()
    await loaded()
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await user.click(await screen.findByRole('button', { name: "Open Bambuddy's queue" }))
    expect(opened).toHaveBeenCalledWith(
      'https://bambuddy.example/queue',
      expect.any(String),
      'noopener',
    )
  })

  it('reads Settings for the queue link only once a run is unanswered', async () => {
    const { urls } = watch('GET', '/settings', '/api/v1/')
    runAnswers(() => new HttpResponse('timeout', { status: 504 }))
    const { user } = renderPicker()
    await loaded()
    expect(urls).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByRole('button', { name: "Open Bambuddy's queue" })
    expect(urls).toHaveLength(1)
  })

  it('offers to read the queue link again when Settings could not be read', async () => {
    let settingsFail = true
    server.use(
      http.get('/api/v1/settings', () =>
        settingsFail
          ? HttpResponse.error()
          : HttpResponse.json(fixtures.settings),
      ),
    )
    runAnswers(() => new HttpResponse('timeout', { status: 504 }))
    const { user } = renderPicker()
    await loaded()
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('may still have been queued')
    const retry = await screen.findByRole('button', { name: "Find Bambuddy's queue" })
    expect(screen.queryByRole('button', { name: "Open Bambuddy's queue" })).toBeNull()

    settingsFail = false
    await user.click(retry)
    expect(await screen.findByRole('button', { name: "Open Bambuddy's queue" })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: "Find Bambuddy's queue" })).toBeNull()
  })

  it('keeps Print when Bambuddy answered an error, which queued nothing', async () => {
    runAnswers(() =>
      HttpResponse.json(
        {
          type: 'https://scadbuddy.dev/problems/bambuddy-unavailable',
          title: 'Bad Gateway',
          status: 502,
          detail: 'Bambuddy answered 500 when asked to queue the print',
          bambuddy_status: 500,
        },
        { status: 502, headers: { 'Content-Type': 'application/problem+json' } },
      ),
    )
    const { user } = renderPicker()
    await loaded()
    await user.click(screen.getByRole('button', { name: /^Print$/ }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Bambuddy answered 500 when asked to queue the print')
    expect(alert).not.toHaveTextContent('may still have been queued')
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled()
  })

  it('keeps Print for a 503, which nothing upstream took', async () => {
    runAnswers(() => new HttpResponse('no healthy upstream', { status: 503 }))
    const { user } = renderPicker()
    await loaded()
    await user.click(screen.getByRole('button', { name: /^Print$/ }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The server is not answering right now (HTTP 503).')
    expect(alert).not.toHaveTextContent('may still have been queued')
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled()
    expect(screen.queryByRole('button', { name: "Open Bambuddy's queue" })).toBeNull()
  })
})

describe('PrintPicker · Printer', () => {
  it('asks which printer only when there is more than one, and re-reads for the one chosen', async () => {
    const { urls } = watch('GET', '/choices')
    const { user } = renderPicker()
    await loaded()

    expect(screen.getByLabelText('Printer')).toHaveValue('1')
    await user.selectOptions(screen.getByLabelText('Printer'), '2')
    await waitFor(() => expect(urls.at(-1)).toContain('printer_id=2'))
  })

  it('does not ask which printer when there is only one', async () => {
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', () =>
        HttpResponse.json({ ...choicesView, printers: choicesView.printers!.slice(0, 1) }),
      ),
    )
    renderPicker()
    await loaded()
    expect(screen.queryByLabelText('Printer')).toBeNull()
  })

  it('reports the chosen printer’s model so the preview can draw its plate', async () => {
    const onPrinterModel = vi.fn()
    renderPicker({ onPrinterModel })
    await loaded()
    await waitFor(() => expect(onPrinterModel).toHaveBeenLastCalledWith('H2C'))
  })
})

describe('PrintPicker · Plate type', () => {
  it('opens on the plate the server resolved and remembers the one printed on', async () => {
    const { bodies, urls } = watch('PUT', '/bed-type')
    const { user } = renderPicker()
    await loaded()
    await showAdvanced()

    expect(screen.getByLabelText('Plate')).toHaveValue('Textured PEI Plate')
    await user.selectOptions(screen.getByLabelText('Plate'), 'Engineering Plate')
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    await waitFor(() => expect(bodies).toHaveLength(1))

    expect(urls[0]).toContain('/print/printers/1/bed-type')
    expect(bodies[0]).toEqual({ bed_type: 'Engineering Plate' })
  })
})

describe('PrintPicker · Simple-mode plate type (#768)', () => {
  it('sends the plate type the choices read chose, with the step not shown', async () => {
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', () =>
        HttpResponse.json({ ...choicesView, bed_type: 'Engineering Plate' }),
      ),
    )
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()

    expect(screen.queryByLabelText('Plate')).toBeNull()
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    expect(bodies[0]).toMatchObject({ choices: { bed_type: 'Engineering Plate' } })
  })
})

describe('PrintPicker · Copies', () => {
  /** Remembers print options the way Settings does, so the GET and the run both see them. */
  async function remember(scope: 'global' | 'printer' | 'model', options: object, key?: string) {
    await fetch('/api/v1/settings/print-options', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope, key: key ?? null, options }),
    })
  }

  it('leaves copies to the remembered quantity until the box is set', async () => {
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()

    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    expect(bodies[0]).not.toHaveProperty('copies')
  })

  it('sends the copies asked for and reports them', async () => {
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()
    await showAdvanced()

    await user.type(screen.getByLabelText('Copies'), '2')
    await user.click(screen.getByRole('button', { name: /^Print$/ }))

    expect(await screen.findByTestId('queued-items')).toHaveTextContent('2 copies')
    expect(bodies[0]).toMatchObject({ copies: 2 })
  })

  it('shows the remembered quantity the run will use (#145)', async () => {
    await remember('global', { quantity: 3 })
    const { user } = renderPicker()
    await loaded()
    await showAdvanced()

    const box = screen.getByLabelText('Copies')
    await waitFor(() => expect(box).toHaveAttribute('placeholder', '3'))
    expect(box).toHaveValue(null)
    expect(screen.getByTestId('remembered-copies')).toHaveTextContent('3 remembered')
    expect(screen.getByTestId('filament-slot-1')).toHaveTextContent('for 3 copies')

    await user.type(box, '2')
    expect(screen.queryByTestId('remembered-copies')).not.toBeInTheDocument()
    expect(screen.getByTestId('filament-slot-1')).toHaveTextContent('for 2 copies')
  })

  it('lets the chosen printer’s remembered quantity beat the global one', async () => {
    await remember('global', { quantity: 2 })
    await remember('printer', { quantity: 4 }, '1')
    renderPicker()
    await loaded()
    await showAdvanced()
    await waitFor(() => expect(screen.getByLabelText('Copies')).toHaveAttribute('placeholder', '4'))
  })

  it('lets the model’s remembered quantity beat the printer’s', async () => {
    await remember('printer', { quantity: 4 }, '1')
    await remember('model', { quantity: 5 }, 'name-keychain')
    renderPicker()
    await loaded()
    await showAdvanced()
    await waitFor(() => expect(screen.getByLabelText('Copies')).toHaveAttribute('placeholder', '5'))
  })
})

describe('PrintPicker · Options', () => {
  it('offers the print options and sends this print’s overrides with the run', async () => {
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()
    await showAdvanced()

    await user.click(screen.getByText('Options'))
    await user.selectOptions(await screen.findByLabelText('Timelapse'), 'true')
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await waitFor(() => expect(bodies).toHaveLength(1))

    expect(bodies[0]).toMatchObject({ options: { timelapse: true } })
    expect(bodies[0]).not.toHaveProperty('copies')
  })

  it('keeps the Copies box and the Quantity row one value', async () => {
    const { user } = renderPicker()
    await loaded()
    await showAdvanced()

    await user.type(screen.getByLabelText('Copies'), '3')
    await user.click(screen.getByText('Options'))
    expect(await screen.findByLabelText('Quantity')).toHaveValue(3)
  })
})

describe('PrintPicker · Projects', () => {
  it('files the print under the chosen project once the queue entries are known', async () => {
    const { bodies } = watch('POST', '/project')
    server.use(
      http.get('/api/v1/print/outputs/:id/progress', () =>
        HttpResponse.json({ ...fixtures.queuedSliceProgress, settled: true }),
      ),
    )
    const { user } = renderPicker()
    await loaded()
    await showAdvanced()

    await user.selectOptions(await screen.findByTestId('project-select'), '2')
    await user.click(screen.getByRole('button', { name: /^Print$/ }))

    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ project_id: 2, queue_item_ids: [4471] })
  })

  it('does not file a print that was sent without a project', async () => {
    const { bodies } = watch('POST', '/project')
    server.use(
      http.get('/api/v1/print/outputs/:id/progress', () =>
        HttpResponse.json({ ...fixtures.queuedSliceProgress, settled: true }),
      ),
    )
    const { user } = renderPicker()
    await loaded()

    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await waitFor(() => expect(screen.getByTestId('print-progress')).toBeInTheDocument())
    expect(bodies).toEqual([])
  })

  /** A `POST` that waits until the returned function is called, then falls through. */
  function hold(path: string): () => void {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.post(path, async () => {
        await gate
        return undefined
      }),
    )
    return release
  }

  it('refuses to close while its own "Create project" is in flight, and disables the select (#710 review)', async () => {
    function Harness() {
      const [open, setOpen] = useState(true)
      return (
        <PrintPicker
          open={open}
          source={{ kind: 'output', output }}
          onClose={() => setOpen(false)}
          onRan={vi.fn()}
        />
      )
    }
    const { user } = renderPage(<Harness />)
    await loaded()
    await showAdvanced()

    const picker = screen.getByTestId<HTMLSelectElement>('project-select')
    await user.selectOptions(picker, 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')

    const release = hold('/api/v1/print/projects')
    await user.click(screen.getByTestId('create-project'))
    // The picker's own in-flight save must not look reselectable, mid-create.
    await waitFor(() => expect(picker).toBeDisabled())
    // Print would go out with the project selected before the create lands.
    expect(screen.getByTestId('run-print')).toBeDisabled()

    // Closing (Cancel here; Escape and the backdrop go through the same `close()`) would
    // unmount the picker and drop its guard before the abandoned request lands. The new-project
    // form has its own Cancel; the dialog's is the footer's, rendered last.
    await user.click(screen.getAllByRole('button', { name: 'Cancel' }).at(-1)!)
    expect(screen.getByRole('dialog', { name: 'Print' })).toBeInTheDocument()
    expect(picker).toBeDisabled()

    release()
    await waitFor(() => expect(picker).toBeEnabled())
    expect(picker.selectedOptions[0]).toHaveTextContent(/Workshop Bins/)
    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Print' })).toBeNull())
  })

  it('holds the Advanced switch, and a picker behind it, mid-create (#710 review)', async () => {
    function Harness() {
      const [open, setOpen] = useState(true)
      return (
        <PrintPicker
          open={open}
          source={{ kind: 'output', output }}
          onClose={() => setOpen(false)}
          onRan={vi.fn()}
        />
      )
    }
    const { user } = renderPage(<Harness />)
    await loaded()
    await showAdvanced()

    await user.selectOptions(screen.getByTestId('project-select'), 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')
    const release = hold('/api/v1/print/projects')
    await user.click(screen.getByTestId('create-project'))
    await waitFor(() => expect(screen.getByTestId('project-select')).toBeDisabled())

    // Switching modes would unmount the picker running the create, so it is held.
    const advanced = screen.getByRole('switch', { name: 'Advanced' })
    expect(advanced).toBeDisabled()
    await user.click(advanced)
    expect(screen.getByTestId('project-select')).toBeDisabled()
    expect(screen.getByTestId('run-print')).toBeDisabled()
    await user.click(screen.getAllByRole('button', { name: 'Cancel' }).at(-1)!)
    expect(screen.getByRole('dialog', { name: 'Print' })).toBeInTheDocument()

    release()
    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())
    expect(advanced).toBeEnabled()
    // Remounted by a mode round-trip once the create has settled, the picker is usable again.
    await user.click(advanced)
    await user.click(advanced)
    expect(screen.getByTestId('project-select')).toBeEnabled()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Print' })).toBeNull())
  })

  it('sends the last project in Simple mode, with no project from the page (#772 review)', async () => {
    const { bodies } = watch('POST', '/run')
    server.use(
      http.get('/api/v1/print/projects', () =>
        HttpResponse.json({ projects: [...fixtures.projectViews], last_project_id: 2 }),
      ),
    )
    const { user } = renderPicker()
    await loaded()
    expect(screen.queryByTestId('project-select')).toBeNull()

    // Print is held until the project list has seeded the project.
    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toMatchObject({ project_id: 2 })
  })
})

describe('PrintPicker · Remembered choices', () => {
  async function putChoices(body: object) {
    await fetch('/api/v1/print/models/name-keychain/choices', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  }

  it('reopens on the spools this model last printed with', async () => {
    await putChoices({ printer_id: 1, filament_plan: [{ slot_id: 1, spool_id: 26 }] })
    renderPicker()
    await loaded()

    // The shelf's blue, over the loaded blue the auto-match picks.
    expect(within(screen.getByTestId('filament-slot-1')).getByTestId('spool-26')).toBeChecked()
    // A slot with nothing remembered still opens on the auto-match.
    expect(within(screen.getByTestId('filament-slot-2')).getByTestId('spool-27')).toBeChecked()
  })

  it('does not reopen on a remembered spool whose colour no longer fits the slot (#933)', async () => {
    // Hot Pink was what slot 2 last printed with; the slot is #FF1493 now.
    await putChoices({ printer_id: 1, filament_plan: [{ slot_id: 2, spool_id: 22 }] })
    renderPicker()
    await loaded()

    expect(within(screen.getByTestId('filament-slot-2')).getByTestId('spool-27')).toBeChecked()
  })

  it('reopens on the nozzle size and quality this model last printed with', async () => {
    await putChoices({
      printer_id: 1,
      filament_plan: [],
      nozzles: [
        { size: '0.2', flow: 'standard' },
        { size: '0.2', flow: 'standard' },
      ],
      tier: 'fine',
      process_name: null,
    })
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()

    // Simple mode shows neither, and sends both (#768).
    expect(screen.getByRole('switch', { name: /advanced/i })).toHaveAttribute('aria-checked', 'false')
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    expect(bodies[0]).toMatchObject({
      choices: { nozzles: [{ size: '0.2' }, { size: '0.2' }], tier: 'fine', process_name: null },
    })
  })

  it('reopens in Advanced on a remembered process', async () => {
    await putChoices({
      printer_id: 1,
      filament_plan: [],
      nozzles: [
        { size: '0.4', flow: 'high_flow' },
        { size: '0.4', flow: 'standard' },
      ],
      tier: null,
      process_name: '0.24mm Standard @BBL H2C',
    })
    renderPicker()
    await loaded()

    expect(screen.getByRole('switch', { name: /advanced/i })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByLabelText('Process')).toHaveValue('0.24mm Standard @BBL H2C')
    expect(screen.getByRole('radio', { name: 'Left High Flow' })).toBeChecked()
  })

  it('reopens in Advanced on a remembered High Flow with no named process', async () => {
    await putChoices({
      printer_id: 1,
      filament_plan: [],
      nozzles: [
        { size: '0.4', flow: 'high_flow' },
        { size: '0.4', flow: 'standard' },
      ],
      tier: 'standard',
      process_name: null,
    })
    renderPicker()
    await loaded()

    expect(screen.getByRole('switch', { name: /advanced/i })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: 'Left High Flow' })).toBeChecked()
    // No named process: Advanced shows the remembered tier's own.
    expect(screen.getByLabelText('Process')).toHaveValue('0.20mm Standard @BBL H2C')
  })

  it('keeps a remembered size and quality sent with no printer and no spool plan', async () => {
    // The backend forgets only an all-default body; the mock once forgot this one too.
    await putChoices({
      printer_id: null,
      filament_plan: [],
      nozzles: [
        { size: '0.2', flow: 'standard' },
        { size: '0.2', flow: 'standard' },
      ],
      tier: 'fine',
      process_name: null,
    })
    renderPicker()
    await loaded()
    await showAdvanced()

    expect(screen.getByRole('radio', { name: /0\.2 mm/ })).toBeChecked()
    expect(screen.getByLabelText('Process')).toHaveValue(
      choicesView.tiers?.['0.2']?.find((row) => row.tier === 'fine')?.process_name,
    )
  })

  it('re-seeds from memory when the same mounted dialog is closed and reopened', async () => {
    await putChoices({
      printer_id: 1,
      filament_plan: [],
      nozzles: [
        { size: '0.2', flow: 'standard' },
        { size: '0.2', flow: 'standard' },
      ],
      tier: 'fine',
      process_name: null,
    })
    // ActionBar's shape: one PrintPicker stays mounted and `open` toggles.
    const target = { ...output, library_file_id: undefined }
    function Harness() {
      const [open, setOpen] = useState(true)
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Reopen
          </button>
          <PrintPicker
            open={open}
            source={{ kind: 'output', output: target }}
            onClose={() => setOpen(false)}
            onRan={vi.fn()}
          />
        </>
      )
    }
    const { user } = renderPage(<Harness />)
    await loaded()
    await showAdvanced()
    expect(screen.getByRole('radio', { name: /0\.2 mm/ })).toBeChecked()

    // Move off everything remembered (Advanced included), then cancel.
    await user.click(screen.getByRole('radio', { name: /0\.4 mm/ }))
    await user.selectOptions(screen.getByLabelText('Printer'), '2')
    await waitFor(() => expect(screen.getByLabelText('Printer')).toHaveValue('2'))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Print' })).toBeNull())

    // What the model remembers changed while the dialog was closed.
    await putChoices({
      printer_id: 1,
      filament_plan: [],
      nozzles: [
        { size: '0.6', flow: 'standard' },
        { size: '0.6', flow: 'standard' },
      ],
      tier: 'draft',
      process_name: null,
    })
    await user.click(screen.getByRole('button', { name: 'Reopen' }))

    await waitFor(() => expect(screen.getByLabelText('Printer')).toHaveValue('1'))
    expect(screen.getByRole('switch', { name: /advanced/i })).toHaveAttribute('aria-checked', 'false')
    await showAdvanced()
    await waitFor(() => expect(screen.getByRole('radio', { name: /0\.6 mm/ })).toBeChecked())
    expect(screen.getByLabelText('Process')).toHaveValue(
      choicesView.tiers?.['0.6']?.find((row) => row.tier === 'draft')?.process_name,
    )
  })

  it('opens on 0.4 mm and Standard when nothing is remembered', async () => {
    renderPicker()
    await loaded()
    await showAdvanced()
    expect(screen.getByRole('radio', { name: /0\.4 mm/ })).toBeChecked()
    expect(screen.getByLabelText('Process')).toHaveValue(
      choicesView.tiers?.['0.4']?.find((row) => row.tier === 'standard')?.process_name,
    )
  })

  it('falls back to the auto-match for a remembered spool no longer in the inventory', async () => {
    await putChoices({ printer_id: null, filament_plan: [{ slot_id: 2, spool_id: 999 }] })
    renderPicker()
    await loaded()
    expect(within(screen.getByTestId('filament-slot-2')).getByTestId('spool-27')).toBeChecked()
  })

  it('remembers the nozzle size and quality it printed with', async () => {
    const { bodies } = watch('PUT', '/choices')
    const { user } = renderPicker()
    await loaded()
    await showAdvanced()

    await user.click(screen.getByRole('radio', { name: /0\.2 mm/ }))
    await user.selectOptions(
      screen.getByLabelText('Process'),
      choicesView.tiers!['0.2']!.find((row) => row.tier === 'fine')!.process_name,
    )
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    await waitFor(() => expect(bodies).toHaveLength(1))

    expect(bodies[0]).toMatchObject({
      printer_id: 1,
      nozzles: [
        { size: '0.2', flow: 'standard' },
        { size: '0.2', flow: 'standard' },
      ],
      tier: null,
      process_name: choicesView.tiers!['0.2']!.find((row) => row.tier === 'fine')!.process_name,
    })
  })

  it('remembers the printer and the spools it printed with', async () => {
    const { bodies } = watch('PUT', '/choices')
    const { user } = renderPicker()
    await loaded()

    await user.selectOptions(screen.getByLabelText('Printer'), '2')
    await waitFor(() => expect(screen.getByLabelText('Printer')).toHaveValue('2'))
    const slot = await screen.findByTestId('filament-slot-2')
    await user.click(within(slot).getByTestId('spool-22'))
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    await waitFor(() => expect(bodies).toHaveLength(1))

    expect(bodies[0]).toEqual({
      printer_id: 2,
      filament_plan: expect.arrayContaining([
        { slot_id: 1, spool_id: 21 },
        { slot_id: 2, spool_id: 22 },
      ]),
      nozzles: [
        { size: '0.4', flow: 'standard' },
        { size: '0.4', flow: 'standard' },
      ],
      tier: 'standard',
      process_name: null,
    })
  })

  it('still prints when the choices cannot be saved', async () => {
    server.use(
      http.put('/api/v1/print/models/:slug/choices', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Internal Server Error', status: 500, detail: 'disk full' },
          { status: 500, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const onRan = vi.fn()
    const { user } = renderPicker({ onRan })
    await loaded()
    await user.click(within(screen.getByTestId('filament-slot-2')).getByTestId('spool-22'))
    await user.click(screen.getByRole('button', { name: /^Print$/ }))

    expect(await screen.findByTestId('queued-items')).toBeInTheDocument()
    expect(onRan).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('PrintPicker · Superseded reads', () => {
  /** A response held back until `release` is called, so reads can answer out of order. */
  function held() {
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    return { gate, release }
  }

  it('keeps the latest printer when an earlier printer read answers last', async () => {
    const printers = [
      ...(choicesView.printers ?? []),
      { id: 3, name: '3DP-00C-003', model: 'H2C', is_active: true, nozzle_count: 2 },
    ]
    const slow = held()
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', async ({ request }) => {
        const asked = Number(new URL(request.url).searchParams.get('printer_id') ?? '1')
        if (asked === 2) await slow.gate
        return HttpResponse.json({ ...choicesView, printers, printer_id: asked })
      }),
    )
    const reads = vi.spyOn(api, 'getChoices')
    const { user } = renderPicker()
    await loaded()

    await user.selectOptions(screen.getByLabelText('Printer'), '2')
    await user.selectOptions(screen.getByLabelText('Printer'), '3')
    await waitFor(() => expect(screen.getByLabelText('Printer')).toHaveValue('3'))
    expect(reads.mock.calls.map(([, printerId]) => printerId)).toEqual([null, 2, 3])

    slow.release()
    await act(async () => {
      await reads.mock.results[1]!.value
    })
    expect(screen.getByLabelText('Printer')).toHaveValue('3')
  })

  it("keeps the latest plate's slots when an earlier plate's read answers last", async () => {
    const plateTwo = {
      ...choicesView.filaments,
      slots: (choicesView.filaments.slots ?? []).filter((slot) => slot.slot_id === 1),
      suggested: (choicesView.filaments.suggested ?? []).filter((c) => c.slot_id === 1),
    }
    const slow = held()
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: false },
          { index: 2, has_thumbnail: false },
        ]),
      ),
      http.get('/api/v1/print/outputs/:id/filaments', async () => {
        await slow.gate
        return HttpResponse.json(plateTwo)
      }),
    )
    const reads = vi.spyOn(api, 'getFilaments')
    const { user } = renderPicker()
    await loaded()

    const plates = await screen.findByTestId('plate-choice')
    await user.click(within(plates).getByRole('radio', { name: /Plate 2/ }))
    await waitFor(() => expect(reads).toHaveBeenCalledTimes(1))
    // Back to plate 1, whose slots are in the choices read already, before plate 2 answers.
    await user.click(within(plates).getByRole('radio', { name: /Plate 1/ }))

    slow.release()
    await act(async () => {
      await reads.mock.results[0]!.value
    })
    expect(screen.getByTestId('filament-slot-2')).toBeInTheDocument()
  })
})

describe('PrintPicker · Plates of a 3MF', () => {
  it('does not ask which plate of a one-plate output to print', async () => {
    renderPicker()
    await loaded()
    expect(screen.queryByTestId('plate-choice')).not.toBeInTheDocument()
  })

  it('never GETs /filaments for plate 1 without all plates', async () => {
    // #525 finding 3: the msw mock's per-plate filtering in `handlers.ts` is only
    // safe because plate 1 without `all_plates` is seeded from the bulk
    // `choices.filaments` payload and never hits this route. Pin that directly.
    const reads = watch('GET', '/filaments')
    renderPicker()
    await loaded()
    expect(reads.urls).toHaveLength(0)
  })

  it('offers each plate of a multi-plate output, reading that plate’s slots', async () => {
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: false },
          { index: 2, has_thumbnail: true },
        ]),
      ),
    )
    const reads = watch('GET', '/filaments')
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()

    const plates = await screen.findByTestId('plate-choice')
    expect(within(plates).getByRole('img', { name: 'Plate 2' })).toHaveAttribute(
      'src',
      expect.stringContaining(`/outputs/${output.id}/plates/2/thumbnail`),
    )
    await user.click(within(plates).getByRole('radio', { name: /Plate 2/ }))
    await waitFor(() => expect(reads.urls.at(-1)).toContain('plate_id=2'))
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')

    expect(bodies[0]).toMatchObject({ plate_id: 2, all_plates: false })
  })

  it('labels each plate by its name, with the number only as secondary text (#929)', async () => {
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: true, name: 'Body' },
          { index: 2, has_thumbnail: false, name: null },
        ]),
      ),
    )
    renderPicker()
    await loaded()

    const plates = await screen.findByTestId('plate-choice')
    const body = within(plates).getByRole('radio', { name: /Body/ })
    expect(within(plates).getByRole('img', { name: 'Body' })).toBeInTheDocument()
    const number = within(body.closest('label')!).getByText('Plate 1')
    expect(number).toHaveClass('text-faint')
    // No name from the 3MF: "Plate N" is the label itself, the last resort.
    const unnamed = within(plates).getByRole('radio', { name: 'Plate 2' })
    // Its number keeps the numeric face a named plate's secondary text has.
    expect(within(unnamed.closest('label')!).getByText('2')).toHaveClass('sb-num')
  })

  it('queues every plate when asked for all of them', async () => {
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: false },
          { index: 2, has_thumbnail: false },
        ]),
      ),
    )
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()

    await user.click(
      within(await screen.findByTestId('plate-choice')).getByRole('radio', { name: 'All plates' }),
    )
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')

    expect(bodies[0]).toMatchObject({ all_plates: true, plate_id: 1 })
  })

  it('reads every plate for all plates, not plate 1 alone', async () => {
    // #480: the default mock gives plate N only slot N and `all_plates` the union, so a
    // read that dropped `all_plates` would come back with slot 1 alone.
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: false },
          { index: 2, has_thumbnail: false },
        ]),
      ),
    )
    const reads = watch('GET', '/filaments')
    const { user } = renderPicker()
    await loaded()
    const plates = await screen.findByTestId('plate-choice')

    await user.click(within(plates).getByRole('radio', { name: /Plate 2/ }))
    await waitFor(() => expect(screen.queryByTestId('filament-slot-1')).not.toBeInTheDocument())
    await user.click(within(plates).getByRole('radio', { name: 'All plates' }))

    expect(await screen.findByTestId('filament-slot-1')).toBeInTheDocument()
    expect(screen.getByTestId('filament-slot-2')).toBeInTheDocument()
    expect(reads.urls.at(-1)).toContain('all_plates=true')
  })

  it('offers a row for a slot only a later plate uses when printing all plates', async () => {
    // Final review 1 / spec §2 step 1: plate 1 uses only slot 1, so the choices read
    // carries one row; "All plates" reads every plate's slots and slot 2 gets a spool.
    const plateOne = {
      ...choicesView.filaments,
      slots: (choicesView.filaments.slots ?? []).filter((slot) => slot.slot_id === 1),
      suggested: (choicesView.filaments.suggested ?? []).filter((c) => c.slot_id === 1),
    }
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: false },
          { index: 2, has_thumbnail: false },
        ]),
      ),
      http.get('/api/v1/print/outputs/:id/choices', () =>
        HttpResponse.json({ ...choicesView, filaments: plateOne }),
      ),
      http.get('/api/v1/print/outputs/:id/filaments', ({ request }) =>
        HttpResponse.json(
          new URL(request.url).searchParams.get('all_plates') === 'true'
            ? choicesView.filaments
            : plateOne,
        ),
      ),
    )
    const reads = watch('GET', '/filaments')
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()
    expect(screen.queryByTestId('filament-slot-2')).not.toBeInTheDocument()

    await user.click(
      within(await screen.findByTestId('plate-choice')).getByRole('radio', { name: 'All plates' }),
    )

    expect(await screen.findByTestId('filament-slot-2')).toBeInTheDocument()
    expect(reads.urls.at(-1)).toContain('all_plates=true')
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    const slots = (bodies[0]?.filament_plan as { slots: { slot_id: number }[] }).slots
    expect(slots.map((slot) => slot.slot_id).sort()).toEqual([1, 2])
  })

  it("drops the previous output's plates while the next output's load", async () => {
    // Both outputs are ones the mock knows, so the second's choices read lands and the
    // dialog stays up: only the reset can take the first output's plates away.
    const [firstFixture, secondFixture] = fixtures.outputs
    expect(firstFixture, 'fixtures.outputs[0]').toBeDefined()
    expect(secondFixture, 'fixtures.outputs[1]').toBeDefined()
    const first = { ...(firstFixture as Output), library_files: [] }
    const second = { ...(secondFixture as Output), library_files: [] }
    server.use(
      http.get('/api/v1/outputs/:id/plates', async ({ params }) => {
        if (params.id === second.id) await delay('infinite')
        return HttpResponse.json([
          { index: 1, has_thumbnail: false },
          { index: 2, has_thumbnail: false },
        ])
      }),
    )
    const reads = watch('GET', '/choices')
    const { rerender } = renderPage(
      <PrintPicker open source={{ kind: 'output', output: first }} onClose={vi.fn()} onRan={vi.fn()} />,
    )
    await screen.findByTestId('plate-choice')

    rerender(<PrintPicker open source={{ kind: 'output', output: second }} onClose={vi.fn()} onRan={vi.fn()} />)
    await waitFor(() => expect(reads.urls.at(-1)).toContain(`/print/outputs/${second.id}/choices`))
    await waitFor(() => expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled())
    expect(screen.queryByTestId('plate-choice')).not.toBeInTheDocument()
  })
})

describe('PrintPicker · Checks (#284)', () => {
  it('judges the request the dialog would print with, and again when a choice changes', async () => {
    const { bodies } = watch('POST', '/run', '/api/v1/analyzers/')
    const { user } = renderPicker()
    await loaded()
    await waitFor(() => expect(bodies.length).toBeGreaterThan(0))
    expect(bodies.at(-1)).toMatchObject({
      target: { output_id: output.id },
      detail: 'advanced',
      request: {
        printer_id: 1,
        plate_id: 1,
        choices: { nozzles: [{ size: '0.4' }, { size: '0.4' }], bed_type: 'Textured PEI Plate' },
        filament_plan: { force_colour_match: false },
      },
    })
    expect(await screen.findByTestId('diagnostic-SB1003')).toBeVisible()

    await showAdvanced()
    await user.click(screen.getByRole('radio', { name: /0\.2 mm/i }))
    await waitFor(() =>
      expect(bodies.at(-1)).toMatchObject({
        request: { choices: { nozzles: [{ size: '0.2' }, { size: '0.2' }] } },
      }),
    )
  })

  it('judges an all-plates print on every plate', async () => {
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: false },
          { index: 2, has_thumbnail: false },
        ]),
      ),
    )
    const { bodies } = watch('POST', '/run', '/api/v1/analyzers/')
    const { user } = renderPicker()
    await loaded()
    await waitFor(() => expect(bodies.at(-1)).toMatchObject({ request: { all_plates: false } }))

    await user.click(
      within(await screen.findByTestId('plate-choice')).getByRole('radio', { name: 'All plates' }),
    )
    await waitFor(() =>
      expect(bodies.at(-1)).toMatchObject({ request: { all_plates: true, plate_id: 1 } }),
    )
  })

  it('leaves Print enabled when a check reports a problem', async () => {
    server.use(
      http.post('/api/v1/analyzers/run', async ({ request }) => {
        const body = (await request.json()) as { request: AnalysisRequest }
        return HttpResponse.json(
          analysisReport(output, body.request, [
            { ...openEdgesDiagnostic, id: 'SB1001', key: 'SB1001:part-2', severity: 'error' },
          ]),
        )
      }),
    )
    renderPicker()
    await loaded()
    expect(await screen.findByTestId('diagnostic-SB1001:part-2')).toHaveTextContent('Problem')
    expect(screen.getByRole('button', { name: /^Print$/ })).toBeEnabled()
  })
})

describe('PrintPicker · A library file (#313)', () => {
  const LIBRARY = { kind: 'library', file: { id: 89, filename: 'bag-clip.3mf' } } as const

  it('prints a library file through the library run and remembers per file', async () => {
    const run = vi.spyOn(api, 'runLibraryPrint')
    const remember = vi.spyOn(api, 'putLibraryChoices')
    const outputRun = vi.spyOn(api, 'runPrint')
    const modelRemember = vi.spyOn(api, 'putModelChoices')
    const { user } = renderPage(
      <PrintPicker open source={LIBRARY} onClose={vi.fn()} onRan={vi.fn()} />,
    )
    await loaded()
    await showAdvanced()
    // A library file is no model, so its options cannot be remembered per model.
    await user.click(screen.getByText('Options'))
    expect(await screen.findByLabelText('Remember for')).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'This model' })).toBeNull()

    await user.click(screen.getByRole('radio', { name: /0\.2 mm/ }))
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    expect(run).toHaveBeenCalledWith(
      89,
      expect.objectContaining({ printer_id: expect.any(Number) }),
      expect.any(AbortSignal),
      expect.any(Function),
    )
    await waitFor(() =>
      expect(remember).toHaveBeenCalledWith(89, expect.objectContaining({ nozzles: expect.any(Array) })),
    )
    expect(outputRun).not.toHaveBeenCalled()
    expect(modelRemember).not.toHaveBeenCalled()
    expect(screen.queryByTestId('print-progress')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open in queue' })).toBeInTheDocument()
  })

  it('files a Simple-mode print under the last project printed to, with no picker shown (#768)', async () => {
    server.use(
      http.get('/api/v1/print/projects', () =>
        HttpResponse.json({ projects: fixtures.projectViews, last_project_id: 2 }),
      ),
    )
    const run = vi.spyOn(api, 'runLibraryPrint')
    const { user } = renderPage(
      <PrintPicker open source={LIBRARY} onClose={vi.fn()} onRan={vi.fn()} />,
    )
    await loaded()

    expect(screen.getByRole('switch', { name: 'Advanced' })).toHaveAttribute('aria-checked', 'false')
    expect(screen.queryByTestId('project-select')).toBeNull()
    const print = screen.getByRole('button', { name: /^Print$/ })
    await waitFor(() => expect(print).toBeEnabled())
    await user.click(print)
    await screen.findByTestId('queued-items')
    expect(run).toHaveBeenCalledWith(
      89,
      expect.objectContaining({ project_id: 2 }),
      expect.any(AbortSignal),
      expect.any(Function),
    )
  })

  it('reopens on the spools this file last printed with', async () => {
    server.use(
      http.get('/api/v1/print/library/89/choices', () =>
        HttpResponse.json({
          ...choicesView,
          model_choices: {
            printer_id: 1,
            // Slot 1's suggestion is 21; 26 is in the inventory and the same blue, 99999
            // is not in the inventory.
            filament_plan: [
              { slot_id: 1, spool_id: 26 },
              { slot_id: 2, spool_id: 99999 },
            ],
          },
        }),
      ),
    )
    renderPage(<PrintPicker open source={LIBRARY} onClose={vi.fn()} onRan={vi.fn()} />)
    await loaded()
    expect(within(screen.getByTestId('filament-slot-1')).getByTestId('spool-26')).toBeChecked()
    // A remembered spool that is no longer in the inventory falls back to the suggestion.
    const suggested = choicesView.filaments.suggested?.find((c) => c.slot_id === 2)?.spool_id
    expect(within(screen.getByTestId('filament-slot-2')).getByTestId(`spool-${suggested}`)).toBeChecked()
  })

  it('carries nothing of an output over when the source becomes a library file', async () => {
    server.use(
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: false },
          { index: 2, has_thumbnail: false },
        ]),
      ),
    )
    const reads = watch('GET', '/choices')
    const { bodies, urls } = watch('POST', '/run')
    const first = fixtures.outputs[0] as Output
    const { user, rerender } = renderPage(
      <PrintPicker open source={{ kind: 'output', output: first }} onClose={vi.fn()} onRan={vi.fn()} />,
    )
    await loaded()

    // A changed spool, a plate, a preset override and an option, all for the output.
    await user.click(within(screen.getByTestId('filament-slot-2')).getByTestId('spool-22'))
    await user.click(within(await screen.findByTestId('plate-choice')).getByRole('radio', { name: /Plate 2/ }))
    await user.click(screen.getByRole('switch', { name: /advanced/i }))
    // Plate 2 uses only slot 2 (#480's mock), so the override is slot 2's.
    await user.selectOptions(await screen.findByLabelText('Preset for slot 2'), 'cloud:GFSB00_22')
    await user.click(screen.getByText('Options'))
    await user.selectOptions(await screen.findByLabelText('Timelapse'), 'true')

    rerender(<PrintPicker open source={LIBRARY} onClose={vi.fn()} onRan={vi.fn()} />)
    await waitFor(() => expect(reads.urls.at(-1)).toContain('/print/library/89/choices'))
    await loaded()
    await waitFor(() => expect(screen.queryByTestId('plate-choice')).not.toBeInTheDocument())
    expect(within(screen.getByTestId('filament-slot-2')).getByTestId('spool-27')).toBeChecked()

    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    expect(urls).toEqual([expect.stringContaining('/print/library/89/run')])
    // toMatchObject would take `{}` as "any object": the cleared halves are compared exactly.
    const body = bodies[0] as { options: unknown; choices: { filament_overrides: unknown } }
    expect(body).toMatchObject({
      plate_id: 1,
      all_plates: false,
      filament_plan: { slots: expect.arrayContaining([{ slot_id: 2, spool_id: 27 }]) },
    })
    expect(body.options).toEqual({})
    expect(body.choices.filament_overrides).toEqual({})
  })
})

describe('PrintPicker · rack nozzle (#836)', () => {
  const rack = {
    group_id: null,
    position: 3,
    reason: 'already loaded with this color',
    unsafe_material: false,
    glow_unchecked: false,
    options: [
      { position: 2, nozzle_diameter: '0.4', flow: 'standard', color: '#00629B', nozzle_type: 'HS01', material: null, prints: 4, print_seconds: 7200 },
      { position: 3, nozzle_diameter: '0.4', flow: 'standard', color: '#FF6A13', nozzle_type: 'HS01', material: null, prints: 0, print_seconds: 0 },
    ],
  }

  it('shows the pick and the unsafe-material warning in Simple mode without holding Print', async () => {
    server.use(
      http.post('/api/v1/print/outputs/:id/check', () =>
        HttpResponse.json({
          errors: [],
          warnings: [{ kind: 'rack-unsafe-material', slot_id: null, message: 'No hardened 0.4 nozzle in the rack for PLA-CF; position 3 is not known to be hardened.' }],
          rack: { ...rack, unsafe_material: true },
        }),
      ),
    )
    renderPicker()
    await loaded()
    expect(await screen.findByTestId('rack-nozzle-line')).toHaveTextContent('position 3 (0.4 Standard)')
    expect(await screen.findByTestId('print-verdict-warning')).toHaveTextContent('No hardened 0.4 nozzle')
    expect(screen.getByTestId('run-print')).toBeEnabled()
  })

  it('sends a hand-picked position and the chosen algorithm, and remembers the algorithm', async () => {
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })))
    const runs = watch('POST', '/run')
    const checks = watch('POST', '/check')
    const puts = watch('PUT', '/rack-algorithm')
    renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack algorithm'), { target: { value: 'newest_first' } })
    fireEvent.change(screen.getByLabelText('Rack nozzle position'), { target: { value: '2' } })
    await waitFor(() =>
      expect(checks.bodies.at(-1)).toMatchObject({ rack_position: 2, rack_algorithm: 'newest_first' }),
    )
    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())
    fireEvent.click(screen.getByTestId('run-print'))

    await waitFor(() => expect(runs.bodies.length).toBe(1))
    expect(runs.bodies[0]).toMatchObject({ rack_position: 2, rack_algorithm: 'newest_first' })
    expect(puts.bodies).toEqual([{ algorithm: 'newest_first' }])
  })

  it('goes back to Automatic when the nozzle size changes', async () => {
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })))
    renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack nozzle position'), { target: { value: '2' } })
    expect(screen.getByLabelText('Rack nozzle position')).toHaveValue('2')
    fireEvent.click(screen.getByRole('radio', { name: /0\.2 mm/i }))
    await waitFor(() => expect(screen.getByLabelText('Rack nozzle position')).toHaveValue(''))
  })

  it('keeps the rack step on screen when the hand pick is refused, and holds Print', async () => {
    server.use(
      http.post('/api/v1/print/outputs/:id/check', async ({ request }) => {
        const body = (await request.json()) as { rack_position?: number | null }
        return HttpResponse.json(
          body.rack_position === 2
            ? { errors: ['Rack position 2 holds a 0.4 Standard nozzle, not the 0.2 this print needs.'], warnings: [], rack }
            : { errors: [], warnings: [], rack },
        )
      }),
    )
    renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack nozzle position'), { target: { value: '2' } })
    expect(await screen.findByText(/Rack position 2 holds/)).toBeInTheDocument()
    expect(screen.getByLabelText('Rack nozzle position')).toBeInTheDocument()
    expect(screen.getByTestId('run-print')).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Rack nozzle position'), { target: { value: '' } })
    await waitFor(() => expect(screen.queryByText(/Rack position 2 holds/)).toBeNull())
    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())
  })

  it('drops a hand pick the check no longer offers, so it is never sent unseen', async () => {
    // claude-review on #1043, finding 3: the re-check with the pick comes back without
    // the rack (unreadable this time), so the step and its select vanish.
    server.use(
      http.post('/api/v1/print/outputs/:id/check', async ({ request }) => {
        const body = (await request.json()) as { rack_position?: number | null }
        return HttpResponse.json({ errors: [], warnings: [], rack: body.rack_position === 2 ? null : rack })
      }),
    )
    const runs = watch('POST', '/run')
    const checks = watch('POST', '/check')
    renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack nozzle position'), { target: { value: '2' } })
    await waitFor(() => expect(checks.bodies.at(-1)).toMatchObject({ rack_position: 2 }))
    await waitFor(() => expect(checks.bodies.at(-1)).toMatchObject({ rack_position: null }))
    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())
    fireEvent.click(screen.getByTestId('run-print'))

    await waitFor(() => expect(runs.bodies.length).toBe(1))
    expect(runs.bodies[0]).toMatchObject({ rack_position: null })
  })

  it('drops a hand pick whose position a re-check no longer lists', async () => {
    let options = rack.options
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack: { ...rack, options } })))
    const checks = watch('POST', '/check')
    renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack nozzle position'), { target: { value: '2' } })
    await waitFor(() => expect(checks.bodies.at(-1)).toMatchObject({ rack_position: 2 }))
    options = rack.options.filter((option) => option.position !== 2)
    fireEvent.change(screen.getByLabelText('Rack algorithm'), { target: { value: 'oldest_first' } })
    await waitFor(() => expect(checks.bodies.at(-1)).toMatchObject({ rack_algorithm: 'oldest_first' }))
    await waitFor(() => expect(checks.bodies.at(-1)).toMatchObject({ rack_position: null }))
    expect(screen.getByLabelText('Rack nozzle position')).toHaveValue('')
  })

  it('says when the algorithm could not be remembered, and still prints with it', async () => {
    // claude-review on #1043, finding 3: a failed PUT was swallowed.
    server.use(
      http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })),
      http.put('/api/v1/print/printers/:id/rack-algorithm', () =>
        HttpResponse.json({ title: 'Service Unavailable', status: 503 }, { status: 503 }),
      ),
    )
    const runs = watch('POST', '/run')
    renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack algorithm'), { target: { value: 'oldest_first' } })
    expect(await screen.findByTestId('rack-algorithm-unsaved')).toHaveTextContent(
      'Not remembered for this printer',
    )
    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())
    fireEvent.click(screen.getByTestId('run-print'))
    await waitFor(() => expect(runs.bodies.length).toBe(1))
    expect(runs.bodies[0]).toMatchObject({ rack_algorithm: 'oldest_first' })
  })

  it("never sends one printer's algorithm for another", async () => {
    // claude-review on #1043, finding 4: after a printer switch, the old printer's
    // algorithm went out until the new choices arrived, overriding the remembered one.
    server.use(
      http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })),
      // Printer 2's choices arrive late: the window the old algorithm leaked through.
      http.get('/api/v1/print/outputs/:id/choices', async ({ request }) => {
        const asked = new URL(request.url).searchParams.get('printer_id')
        if (asked === '2') await delay(400)
        return HttpResponse.json({ ...choicesView, printer_id: asked === null ? choicesView.printer_id : Number(asked) })
      }),
    )
    const checks = watch('POST', '/check')
    const { user } = renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack algorithm'), { target: { value: 'oldest_first' } })
    await waitFor(() => expect(checks.bodies.at(-1)).toMatchObject({ rack_algorithm: 'oldest_first' }))
    await user.selectOptions(screen.getByLabelText('Printer'), '2')
    await waitFor(() => expect(checks.bodies.at(-1)).toMatchObject({ printer_id: 2 }))
    expect(checks.bodies.filter((b) => (b as { printer_id?: number }).printer_id === 2)).toEqual(
      expect.not.arrayContaining([expect.objectContaining({ rack_algorithm: 'oldest_first' })]),
    )
  })

  it('forgets the chosen algorithm and its failed save when the dialog is closed', async () => {
    // #1084: close() reset the hand pick but not the algorithm, so a reopen on the same
    // printer sent the old session's choice and still said it was not remembered.
    server.use(
      http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })),
      http.put('/api/v1/print/printers/:id/rack-algorithm', () =>
        HttpResponse.json({ title: 'Service Unavailable', status: 503 }, { status: 503 }),
      ),
    )
    const runs = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack algorithm'), { target: { value: 'oldest_first' } })
    await screen.findByTestId('rack-algorithm-unsaved')

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await loaded()
    expect(screen.queryByTestId('rack-algorithm-unsaved')).toBeNull()
    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())
    fireEvent.click(screen.getByTestId('run-print'))
    await waitFor(() => expect(runs.bodies.length).toBe(1))
    expect(runs.bodies[0]).toMatchObject({ rack_algorithm: null })
  })

  it('ignores a save that fails after the dialog was closed', async () => {
    // #1086 review: a PUT still in flight at close() would land its failure on the next
    // session and say an algorithm nobody chose there was not remembered.
    let release!: () => void
    const held = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })),
      http.put('/api/v1/print/printers/:id/rack-algorithm', async () => {
        await held
        return HttpResponse.json({ title: 'Service Unavailable', status: 503 }, { status: 503 })
      }),
    )
    const saves: Promise<unknown>[] = []
    const put = api.putPrinterRackAlgorithm.bind(api)
    vi.spyOn(api, 'putPrinterRackAlgorithm').mockImplementation((...args) => {
      const save = put(...args)
      saves.push(save)
      return save
    })
    const { user } = renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack algorithm'), { target: { value: 'oldest_first' } })
    await waitFor(() => expect(saves.length).toBe(1))

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await loaded()
    // close() puts the dialog back in Simple mode, which has no rack step.
    expect(screen.queryByLabelText('Rack algorithm')).toBeNull()
    await showAdvanced()
    await screen.findByLabelText('Rack algorithm')
    release()
    // The picker's own .catch was chained first, so it has run once this settles.
    await act(() => Promise.allSettled(saves))
    expect(screen.queryByTestId('rack-algorithm-unsaved')).toBeNull()
  })

  it('shows a save that lands after a close and reopen, and leaves it to the server', async () => {
    // #1086 review: the reopened dialog read the old algorithm before the save landed, so
    // it labelled one the backend no longer used. It re-reads the choices, and sends no
    // choice of its own: the stored one is the server's to apply.
    let release!: () => void
    const held = new Promise<void>((resolve) => (release = resolve))
    // The printer's stored algorithm, as the choices read reports it.
    let stored = 'least_used'
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', () => HttpResponse.json({ ...choicesView, rack_algorithm: stored })),
      http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })),
      http.put('/api/v1/print/printers/:id/rack-algorithm', async () => {
        await held
        stored = 'oldest_first'
        return HttpResponse.json({ algorithm: 'oldest_first' })
      }),
    )
    const saves: Promise<unknown>[] = []
    const put = api.putPrinterRackAlgorithm.bind(api)
    vi.spyOn(api, 'putPrinterRackAlgorithm').mockImplementation((...args) => {
      const save = put(...args)
      saves.push(save)
      return save
    })
    const checks = watch('POST', '/check')
    const choiceReads = watch('GET', '/choices')
    const { user } = renderReopenable()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack algorithm'), { target: { value: 'oldest_first' } })
    await waitFor(() => expect(saves.length).toBe(1))

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Reopen' }))
    await loaded()
    await showAdvanced()
    const select = await screen.findByLabelText<HTMLSelectElement>('Rack algorithm')
    expect(select.value).toBe('least_used')
    // What the reopened dialog sets before the save lands must survive it.
    await user.selectOptions(screen.getByLabelText('Plate'), 'Engineering Plate')
    const spool = within(screen.getByTestId('filament-slot-2')).getByTestId('spool-22')
    fireEvent.click(spool)
    expect(spool).toBeChecked()
    const reads = choiceReads.urls.length
    release()
    await act(() => Promise.allSettled(saves))

    await waitFor(() => expect(select.value).toBe('oldest_first'))
    expect(screen.getByLabelText('Plate')).toHaveValue('Engineering Plate')
    expect(within(screen.getByTestId('filament-slot-2')).getByTestId('spool-22')).toBeChecked()
    expect(choiceReads.urls.length).toBe(reads)
    expect(checks.bodies.at(-1)).toMatchObject({ rack_algorithm: null })

    // A third open reads the choices again, which now carry the saved algorithm.
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Reopen' }))
    await loaded()
    await showAdvanced()
    const third = await screen.findByLabelText<HTMLSelectElement>('Rack algorithm')
    await waitFor(() => expect(third.value).toBe('oldest_first'))
  })

  /** Holds each algorithm PUT until the test answers it, in any order. */
  function heldSaves() {
    const answers: ((status: number) => void)[] = []
    // The printer's stored algorithm, as the choices read reports it.
    let stored = 'least_used'
    // While set, each choices read answers with what was stored when it started, but only
    // once the test releases it.
    let holdReads = false
    const heldReads: (() => void)[] = []
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', async ({ request }) => {
        const asked = new URL(request.url).searchParams.get('printer_id')
        const printer_id = asked === null ? choicesView.printer_id : Number(asked)
        const rack_algorithm = stored
        if (holdReads) await new Promise<void>((resolve) => heldReads.push(resolve))
        return HttpResponse.json({ ...choicesView, printer_id, rack_algorithm })
      }),
      http.put('/api/v1/print/printers/:id/rack-algorithm', async ({ request }) => {
        const { algorithm } = (await request.json()) as { algorithm: string }
        const status = await new Promise<number>((resolve) => answers.push(resolve))
        if (status !== 200) return HttpResponse.json({ title: 'Service Unavailable', status }, { status })
        stored = algorithm
        return HttpResponse.json({ algorithm })
      }),
    )
    const saves: Promise<unknown>[] = []
    const put = api.putPrinterRackAlgorithm.bind(api)
    vi.spyOn(api, 'putPrinterRackAlgorithm').mockImplementation((...args) => {
      const save = put(...args)
      saves.push(save)
      return save
    })
    /** Answer the `index`th save with `status`. */
    const answer = (index: number, status: number) => answers[index]?.(status)
    const holdChoiceReads = (hold: boolean) => {
      holdReads = hold
    }
    /** Answer every held choices read. */
    const releaseReads = () => heldReads.splice(0).forEach((resolve) => resolve())
    /** What the printer stores now. */
    const storedNow = () => stored
    return { answers, answer, saves, heldReads, holdChoiceReads, releaseReads, stored: storedNow }
  }

  it('lets only the latest of two saves say it was not remembered', async () => {
    // #1086 review: an earlier save failing after a later one succeeded said the later
    // choice was not remembered.
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })))
    const { answers, answer, saves } = heldSaves()
    renderPicker()
    await loaded()
    await showAdvanced()
    const select = await screen.findByLabelText('Rack algorithm')
    fireEvent.change(select, { target: { value: 'oldest_first' } })
    fireEvent.change(select, { target: { value: 'least_used' } })
    await waitFor(() => expect(answers.length).toBe(1))

    answer(0, 503)
    await waitFor(() => expect(answers.length).toBe(2))
    answer(1, 200)
    await act(() => Promise.allSettled(saves))
    expect(screen.queryByTestId('rack-algorithm-unsaved')).toBeNull()
  })

  it('shows what two saves landing after a close and reopen left stored', async () => {
    // #1086 review: the first to arrive was adopted, and the later choice then ignored.
    const checks = watch('POST', '/check')
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })))
    const { answers, answer, saves } = heldSaves()
    const { user } = renderReopenable()
    await loaded()
    await showAdvanced()
    const first = await screen.findByLabelText('Rack algorithm')
    fireEvent.change(first, { target: { value: 'oldest_first' } })
    fireEvent.change(first, { target: { value: 'bambuddy' } })
    await waitFor(() => expect(answers.length).toBe(1))

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Reopen' }))
    await loaded()
    await showAdvanced()
    const select = await screen.findByLabelText<HTMLSelectElement>('Rack algorithm')
    answer(0, 200)
    await waitFor(() => expect(answers.length).toBe(2))
    answer(1, 200)
    await act(() => Promise.allSettled(saves))

    await waitFor(() => expect(select.value).toBe('bambuddy'))
    expect(checks.bodies.at(-1)).toMatchObject({ rack_algorithm: null })
  })

  it('sends a save only once the one before it is answered, so the last choice is stored', async () => {
    // #1086 review: two PUTs in flight at once could be applied in either order, leaving
    // the printer on a choice the user had replaced.
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })))
    const { answers, answer, saves, stored } = heldSaves()
    renderPicker()
    await loaded()
    await showAdvanced()
    const select = await screen.findByLabelText('Rack algorithm')
    fireEvent.change(select, { target: { value: 'oldest_first' } })
    fireEvent.change(select, { target: { value: 'bambuddy' } })
    await waitFor(() => expect(answers.length).toBe(1))
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(answers.length).toBe(1)

    answer(0, 200)
    await waitFor(() => expect(answers.length).toBe(2))
    answer(1, 200)
    await act(() => Promise.allSettled(saves))
    expect(stored()).toBe('bambuddy')
  })

  it('counts a save that never answers as not remembered, and sends the next one', async () => {
    // #1086 review: saves go one at a time, so one PUT left unanswered held every later
    // save back for the life of the page, with nothing shown.
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })))
    const { answers, answer, saves, stored } = heldSaves()
    rackAlgorithmSave.timeoutMs = 100
    try {
      renderPicker()
      await loaded()
      await showAdvanced()
      const select = await screen.findByLabelText('Rack algorithm')
      fireEvent.change(select, { target: { value: 'oldest_first' } })
      expect(await screen.findByTestId('rack-algorithm-unsaved')).toBeInTheDocument()

      fireEvent.change(select, { target: { value: 'bambuddy' } })
      await waitFor(() => expect(answers.length).toBe(2))
      answer(1, 200)
      await act(() => Promise.allSettled(saves))
      expect(stored()).toBe('bambuddy')
      expect(screen.queryByTestId('rack-algorithm-unsaved')).toBeNull()
    } finally {
      rackAlgorithmSave.timeoutMs = 25_000
    }
  })

  it('keeps showing a save that lands while the reopened dialog is still reading', async () => {
    // #1086 review: a choices read that started before the save landed answered with the
    // old algorithm and cleared the save's label, so the dialog showed least_used while the
    // print used oldest_first.
    const checks = watch('POST', '/check')
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })))
    const { answers, answer, saves, heldReads, holdChoiceReads, releaseReads } = heldSaves()
    const { user } = renderReopenable()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack algorithm'), { target: { value: 'oldest_first' } })
    await waitFor(() => expect(answers.length).toBe(1))

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    holdChoiceReads(true)
    await user.click(screen.getByRole('button', { name: 'Reopen' }))
    await waitFor(() => expect(heldReads.length).toBe(1))
    answer(0, 200)
    await act(() => Promise.allSettled(saves))
    holdChoiceReads(false)
    releaseReads()

    await loaded()
    await showAdvanced()
    const select = await screen.findByLabelText<HTMLSelectElement>('Rack algorithm')
    await waitFor(() => expect(checks.bodies.at(-1)).toMatchObject({ rack_algorithm: null }))
    expect(select.value).toBe('oldest_first')
  })

  it('shows a save on another printer nowhere, and leaves this one alone', async () => {
    // #1086 review: a save on printer 1 landing after a switch to printer 2 re-read
    // printer 2's choices and reset what had been set there.
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })))
    const { answers, answer, saves } = heldSaves()
    const choiceReads = watch('GET', '/choices')
    const { user } = renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack algorithm'), { target: { value: 'oldest_first' } })
    await waitFor(() => expect(answers.length).toBe(1))

    await user.selectOptions(screen.getByLabelText('Printer'), '2')
    await waitFor(() => expect(screen.getByLabelText('Printer')).toHaveValue('2'))
    await loaded()
    const select = await screen.findByLabelText<HTMLSelectElement>('Rack algorithm')
    await waitFor(() => expect(select.value).toBe('least_used'))
    await user.selectOptions(screen.getByLabelText('Plate'), 'Engineering Plate')
    const reads = choiceReads.urls.length
    answer(0, 200)
    await act(() => Promise.allSettled(saves))

    expect(select.value).toBe('least_used')
    expect(screen.getByLabelText('Plate')).toHaveValue('Engineering Plate')
    expect(choiceReads.urls.length).toBe(reads)
  })

  it('drops the hand pick when going back to Simple, which cannot show it', async () => {
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })))
    const runs = watch('POST', '/run')
    const checks = watch('POST', '/check')
    renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack nozzle position'), { target: { value: '2' } })
    await waitFor(() => expect(checks.bodies.at(-1)).toMatchObject({ rack_position: 2 }))
    fireEvent.click(screen.getByRole('switch', { name: 'Advanced' }))
    await waitFor(() => expect(checks.bodies.at(-1)).toMatchObject({ rack_position: null }))
    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())
    fireEvent.click(screen.getByTestId('run-print'))

    await waitFor(() => expect(runs.bodies.length).toBe(1))
    expect(runs.bodies[0]).toMatchObject({ rack_position: null })
  })
})
