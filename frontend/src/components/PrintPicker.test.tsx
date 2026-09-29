import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { useState } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
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
      slug="name-keychain"
      output={{ ...output, library_files: [] }}
      onClose={props.onClose ?? vi.fn()}
      onRan={props.onRan ?? vi.fn()}
      onPrinterModel={props.onPrinterModel}
    />,
  )
}

/** The dialog has read its choices once the nozzle step is on screen. */
async function loaded() {
  await screen.findByRole('group', { name: /nozzles/i })
  await screen.findByTestId('filament-slot-1')
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
  it('opens on the choices, with Simple mode and no pipeline list', async () => {
    renderPicker()
    expect(await screen.findByRole('group', { name: /nozzles/i })).toBeInTheDocument()
    expect(screen.queryByTestId('run-pipeline')).toBeNull()
    expect(screen.queryByText(/pipeline/i)).toBeNull()
  })

  it('marks Print user-only, since it queues a physical print', async () => {
    renderPicker()
    await screen.findByRole('group', { name: /nozzles/i })
    expect(screen.getByTestId('run-print')).toHaveAttribute('data-agent-user-only')
  })

  it('sends the choices and the spool plan in one run request', async () => {
    const run = vi.spyOn(api, 'runPrint').mockResolvedValue(queuedResult)
    renderPicker()
    fireEvent.click(await screen.findByRole('radio', { name: /0\.2 mm/i }))
    fireEvent.click(screen.getByRole('radio', { name: /Fine/ }))
    fireEvent.click(screen.getByRole('button', { name: /^Print$/ }))
    await waitFor(() => expect(run).toHaveBeenCalled())
    const [, body] = run.mock.calls[0]!
    expect(body.choices).toMatchObject({ nozzles: [{ size: '0.2' }, { size: '0.2' }], tier: 'fine' })
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

  it('rules out spools by the nozzle size chosen, following a change of size (#469)', async () => {
    unswitched()
    renderPicker()
    const slot = await screen.findByTestId('filament-slot-2')
    // The default 0.4: spool 9 feeds the right extruder, where the 0.2 is fitted.
    expect(within(slot).getByTestId('spool-9')).toBeDisabled()
    expect(within(slot).getByTestId('spool-22')).toBeEnabled()

    fireEvent.click(screen.getByRole('radio', { name: /0\.2 mm/i }))

    await waitFor(() => expect(within(slot).getByTestId('spool-9')).toBeEnabled())
    expect(within(slot).getByTestId('spool-22')).toBeDisabled()
  })

  it('never opens on a spool the size rules out, and swaps one a size change rules out (#469)', async () => {
    unswitched()
    const { user } = renderPicker()
    // The suggestion is spool 21 (on the right's 0.2) for slot 1; at the default 0.4 it
    // is swapped for the same blue on the shelf, which has no side to rule it out.
    const one = await screen.findByTestId('filament-slot-1')
    await waitFor(() => expect(within(one).getByTestId('spool-26')).toBeChecked())
    expect(within(one).getByTestId('spool-21')).not.toBeChecked()

    // Spool 22 is on the left's 0.4; at 0.2 it can't print, so the pink on the shelf
    // takes its place rather than leaving a selection Print would be refused for.
    const two = screen.getByTestId('filament-slot-2')
    await user.click(within(two).getByTestId('spool-22'))
    await user.click(screen.getByRole('radio', { name: /0\.2 mm/i }))
    await waitFor(() => expect(within(two).getByTestId('spool-27')).toBeChecked())
  })

  it('disables Print and names the slot when a spool has no preset for the size', async () => {
    vi.spyOn(api, 'runPrint').mockRejectedValue(
      new ApiError(422, 'Generic TPU has no slicer preset for a 0.2 mm nozzle. Pick one under Advanced.'),
    )
    renderPicker()
    fireEvent.click(await screen.findByRole('radio', { name: /0\.2 mm/i }))
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
      <PrintPicker open slug="name-keychain" output={first} onClose={vi.fn()} onRan={vi.fn()} />,
    )
    await loaded()

    await user.click(screen.getByRole('switch', { name: /advanced/i }))
    await user.selectOptions(screen.getByLabelText('Preset for slot 1'), 'cloud:GFSB00_22')

    rerender(
      <MemoryRouter>
        <PrintPicker open slug="name-keychain" output={second} onClose={vi.fn()} onRan={vi.fn()} />
      </MemoryRouter>,
    )
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

    const toggle = screen.getByRole('switch', { name: /advanced/i })
    const nozzles = screen.getByRole('group', { name: /nozzles/i })
    expect(toggle.compareDocumentPosition(nozzles) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(toggle).toHaveAccessibleDescription(/any process/i)
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
  })
})

describe('PrintPicker · A run that got no answer (#470)', () => {
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
    expect(bodies).toHaveLength(1)
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
        slug="name-keychain"
        output={{ ...output, library_files: [] }}
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
        return HttpResponse.json(queuedResult)
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

    expect(screen.getByLabelText('Plate')).toHaveValue('Textured PEI Plate')
    await user.selectOptions(screen.getByLabelText('Plate'), 'Engineering Plate')
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    await waitFor(() => expect(bodies).toHaveLength(1))

    expect(urls[0]).toContain('/print/printers/1/bed-type')
    expect(bodies[0]).toEqual({ bed_type: 'Engineering Plate' })
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

    await user.type(screen.getByLabelText('Copies'), '2')
    await user.click(screen.getByRole('button', { name: /^Print$/ }))

    expect(await screen.findByTestId('queued-items')).toHaveTextContent('2 copies')
    expect(bodies[0]).toMatchObject({ copies: 2 })
  })

  it('shows the remembered quantity the run will use (#145)', async () => {
    await remember('global', { quantity: 3 })
    const { user } = renderPicker()
    await loaded()

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
    await waitFor(() => expect(screen.getByLabelText('Copies')).toHaveAttribute('placeholder', '4'))
  })

  it('lets the model’s remembered quantity beat the printer’s', async () => {
    await remember('printer', { quantity: 4 }, '1')
    await remember('model', { quantity: 5 }, 'name-keychain')
    renderPicker()
    await loaded()
    await waitFor(() => expect(screen.getByLabelText('Copies')).toHaveAttribute('placeholder', '5'))
  })
})

describe('PrintPicker · Options', () => {
  it('offers the print options and sends this print’s overrides with the run', async () => {
    const { bodies } = watch('POST', '/run')
    const { user } = renderPicker()
    await loaded()

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
    await putChoices({ printer_id: 1, filament_plan: [{ slot_id: 2, spool_id: 22 }] })
    renderPicker()
    await loaded()

    expect(within(screen.getByTestId('filament-slot-2')).getByTestId('spool-22')).toBeChecked()
    // A slot with nothing remembered still opens on the auto-match.
    expect(within(screen.getByTestId('filament-slot-1')).getByTestId('spool-21')).toBeChecked()
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
    renderPicker()
    await loaded()

    expect(screen.getByRole('radio', { name: /0\.2 mm/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Fine/ })).toBeChecked()
    expect(screen.getByRole('switch', { name: /advanced/i })).toHaveAttribute('aria-checked', 'false')
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

    expect(screen.getByRole('radio', { name: /0\.2 mm/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Fine/ })).toBeChecked()
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
            slug="name-keychain"
            output={target}
            onClose={() => setOpen(false)}
            onRan={vi.fn()}
          />
        </>
      )
    }
    const { user } = renderPage(<Harness />)
    await loaded()
    expect(screen.getByRole('radio', { name: /0\.2 mm/ })).toBeChecked()

    // Move off everything remembered, then cancel.
    await user.click(screen.getByRole('radio', { name: /0\.4 mm/ }))
    await user.selectOptions(screen.getByLabelText('Printer'), '2')
    await waitFor(() => expect(screen.getByLabelText('Printer')).toHaveValue('2'))
    await user.click(screen.getByRole('switch', { name: /advanced/i }))
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

    await waitFor(() => expect(screen.getByRole('radio', { name: /0\.6 mm/ })).toBeChecked())
    expect(screen.getByRole('radio', { name: /Draft/ })).toBeChecked()
    expect(screen.getByLabelText('Printer')).toHaveValue('1')
    expect(screen.getByRole('switch', { name: /advanced/i })).toHaveAttribute('aria-checked', 'false')
  })

  it('opens on 0.4 mm and Standard when nothing is remembered', async () => {
    renderPicker()
    await loaded()
    expect(screen.getByRole('radio', { name: /0\.4 mm/ })).toBeChecked()
    expect(screen.getByRole('radio', { name: /Standard — / })).toBeChecked()
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

    await user.click(screen.getByRole('radio', { name: /0\.2 mm/ }))
    await user.click(screen.getByRole('radio', { name: /Fine/ }))
    await user.click(screen.getByRole('button', { name: /^Print$/ }))
    await screen.findByTestId('queued-items')
    await waitFor(() => expect(bodies).toHaveLength(1))

    expect(bodies[0]).toMatchObject({
      printer_id: 1,
      nozzles: [
        { size: '0.2', flow: 'standard' },
        { size: '0.2', flow: 'standard' },
      ],
      tier: 'fine',
      process_name: null,
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
    const first = { ...output, id: 'a'.repeat(32), library_files: [] }
    const second = { ...output, id: 'b'.repeat(32), library_files: [] }
    server.use(
      http.get('/api/v1/outputs/:id/plates', async ({ params }) => {
        if (params.id === second.id) await delay('infinite')
        return HttpResponse.json([
          { index: 1, has_thumbnail: false },
          { index: 2, has_thumbnail: false },
        ])
      }),
    )
    const { rerender } = renderPage(
      <PrintPicker open slug="name-keychain" output={first} onClose={vi.fn()} onRan={vi.fn()} />,
    )
    await screen.findByTestId('plate-choice')

    rerender(
      <MemoryRouter>
        <PrintPicker open slug="name-keychain" output={second} onClose={vi.fn()} onRan={vi.fn()} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.queryByTestId('plate-choice')).not.toBeInTheDocument())
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
