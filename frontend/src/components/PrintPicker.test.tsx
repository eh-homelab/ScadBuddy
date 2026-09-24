import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Output, PrintRunResult } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { PrintPicker } from './PrintPicker'

const output = fixtures.outputs[0] as Output

function open(onRan: (result: PrintRunResult) => void = vi.fn()) {
  return renderPage(
    <PrintPicker
      open
      slug="name-keychain"
      output={{ ...output, library_file_id: undefined, pipeline_run_id: undefined }}
      onClose={vi.fn()}
      onRan={onRan}
    />,
  )
}

// A pipeline's accessible name is its whole card — name, target, bed type and presets —
// so these match on the part that is unique to one row.
const TEXTURED = /Textured PEI · 0\.20 mm/
const DRAFT = /Draft · 0\.28 mm/
const ANY_H2C = /Any H2C/

/** The panel lists pipelines, then checks eligibility, so wait for both to land. */
async function listed() {
  await screen.findByRole('radio', { name: TEXTURED })
  await waitFor(() => expect(screen.getAllByText(/^(ready|not ready)$/).length).toBeGreaterThan(0))
}

function row(name: RegExp) {
  return screen.getByRole('radio', { name }).closest('li') as HTMLElement
}

describe('PrintPicker', () => {
  beforeEach(() => resetMockState())

  it('lists each pipeline with its target, bed type and presets', async () => {
    open()
    await listed()

    const textured = row(TEXTURED)
    expect(textured).toHaveTextContent('3DP-31B-598')
    expect(textured).toHaveTextContent('Textured PEI Plate')
    // The nozzle diameter lives in the process preset's name, which is why it is shown.
    expect(textured).toHaveTextContent('0.20mm Standard @BBL H2C')
    // A printer_class target reads as the class, not as a printer.
    expect(row(ANY_H2C)).toHaveTextContent('any H2C')
  })

  it('shows the per-slot issues inline for a pipeline that is not ready', async () => {
    open()
    await listed()

    const draft = row(DRAFT)
    expect(draft).toHaveTextContent('not ready')
    // slot_index is 0-based on the wire and 1-based for a human.
    expect(draft).toHaveTextContent('filament type mismatch (slot 1): expected ABS, found PLA')
    // slot_index: null is a whole-plate issue and gets no slot number.
    expect(draft).toHaveTextContent('nozzle diameter mismatch')
    expect(within(draft).getByText(/nozzle diameter mismatch/)).not.toHaveTextContent('slot')
  })

  it('preselects the model default and falls back to the global one', async () => {
    open()
    await listed()
    // fixtures.settings.pipeline_id is 1 and no model default is set yet.
    expect(screen.getByRole('radio', { name: TEXTURED })).toBeChecked()
  })

  it('asks which printer only when the pipeline targets a printer class', async () => {
    const { user } = open()
    await listed()

    expect(screen.queryByLabelText('Printer')).not.toBeInTheDocument()
    // A specific_printer target needs no question — it names the printer it derived.
    expect(screen.getByText(/from the pipeline/)).toHaveTextContent('3DP-31B-598')

    await user.click(screen.getByRole('radio', { name: ANY_H2C }))

    const printer = await screen.findByLabelText('Printer')
    expect(printer).toHaveValue('')
    expect(screen.getByTestId('run-pipeline')).toBeDisabled()
  })

  it('narrows a printer-class report to the printer that was chosen', async () => {
    const { user } = open()
    await listed()
    await user.click(screen.getByRole('radio', { name: ANY_H2C }))

    // `ok: true` under printer_class means at least ONE printer passes; printer 2 does not,
    // and its reasons are in printer_reports rather than the empty top-level `issues`.
    await user.selectOptions(await screen.findByLabelText('Printer'), '2')
    await waitFor(() =>
      expect(screen.getByTestId('issues-3')).toHaveTextContent('ams slot empty (slot 2)'),
    )
    expect(row(ANY_H2C)).toHaveTextContent('not ready')

    await user.selectOptions(screen.getByLabelText('Printer'), '1')
    await waitFor(() => expect(screen.queryByTestId('issues-3')).not.toBeInTheDocument())
    expect(row(ANY_H2C)).toHaveTextContent('ready')
  })

  it('runs the selected pipeline with the copies asked for and reports the queue entries', async () => {
    const onRan = vi.fn()
    const { user } = open(onRan)
    await listed()

    await user.clear(screen.getByLabelText('Copies'))
    await user.type(screen.getByLabelText('Copies'), '2')
    await user.click(screen.getByTestId('run-pipeline'))

    expect(await screen.findByText(/Pipeline run/)).toHaveTextContent('2 copies')
    // Which printer each copy landed on is Bambuddy's answer, from run.jobs[].
    expect(screen.getByTestId('run-jobs')).toHaveTextContent('Copy 1 on 3DP-31B-598')
    expect(onRan).toHaveBeenCalledTimes(1)
  })

  it('only offers force once the issues have been shown', async () => {
    const { user } = open()
    await listed()

    // The ready pipeline shows nothing to override.
    expect(screen.queryByTestId('force')).not.toBeInTheDocument()

    await user.click(screen.getByRole('radio', { name: DRAFT }))

    expect(await screen.findByTestId('force')).toBeInTheDocument()
  })

  it('surfaces a refused run and then runs it with force', async () => {
    const { user } = open()
    await listed()
    await user.click(screen.getByRole('radio', { name: DRAFT }))

    await user.click(screen.getByTestId('run-pipeline'))

    const alert = await screen.findByRole('alert')
    // Bambuddy's own report, listed rather than paraphrased.
    expect(alert).toHaveTextContent('filament type mismatch (slot 1)')

    await user.click(screen.getByTestId('force'))
    expect(screen.getByTestId('run-pipeline')).toHaveTextContent('Run anyway')
    await user.click(screen.getByTestId('run-pipeline'))

    expect(await screen.findByText(/Pipeline run/)).toBeInTheDocument()
    expect(screen.getByText(/eligibility check overridden/)).toBeInTheDocument()
  })

  it('remembers the pipeline for this model when asked to', async () => {
    const writes = watchDefaultWrites()
    const { user } = open()
    await listed()

    // No model default yet, so ticking the box is a real change and is written.
    await user.click(screen.getByLabelText(/Always use this pipeline/))
    await user.click(screen.getByTestId('run-pipeline'))
    await screen.findByText(/Pipeline run/)

    expect(writes).toEqual([{ pipeline_id: 1 }])
  })

  /** The model already prints with pipeline 2 (Draft); 1 (Textured) is the global fallback. */
  function withModelDefault(pipelineId: number | null = 2) {
    server.use(
      http.get('/api/v1/print/models/:slug/pipelines', () =>
        HttpResponse.json({
          pipelines: fixtures.pipelineViews,
          printers: fixtures.targets.printers,
          model_pipeline_id: pipelineId,
          global_pipeline_id: 1,
          default_pipeline_id: pipelineId ?? 1,
        }),
      ),
    )
  }

  /** Every `PUT /print/models/{slug}/pipeline` body, in order. */
  function watchDefaultWrites(): { pipeline_id: number | null }[] {
    const writes: { pipeline_id: number | null }[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'PUT' && request.url.includes('/print/models/')) {
        writes.push((await request.clone().json()) as { pipeline_id: number | null })
      }
    })
    return writes
  }

  it('does not re-point an existing model default at whatever is selected next', async () => {
    withModelDefault(2)
    const writes = watchDefaultWrites()
    const { user } = open()
    await listed()

    // Opened on its default, so the box reflects that this pipeline *is* the default.
    expect(screen.getByRole('radio', { name: DRAFT })).toBeChecked()
    expect(screen.getByLabelText(/Always use this pipeline/)).toBeChecked()

    await user.click(screen.getByRole('radio', { name: TEXTURED }))

    // Switching for one print must not carry the tick — and so must not silently make
    // the newly chosen pipeline the model's default.
    expect(screen.getByLabelText(/Always use this pipeline/)).not.toBeChecked()

    await user.click(screen.getByTestId('run-pipeline'))
    await screen.findByText(/Pipeline run/)

    // And it must not CLEAR the default either: printing something else once says nothing
    // about what this model should default to, so the stored default is left alone.
    expect(writes).toEqual([])
  })

  it('clears the default only when the user unticks the pipeline that is the default', async () => {
    withModelDefault(2)
    const writes = watchDefaultWrites()
    const { user } = open()
    await listed()
    expect(screen.getByRole('radio', { name: DRAFT })).toBeChecked()

    // Deliberately unticking the box on the pipeline that *is* the default is the one
    // gesture that means "stop defaulting to this".
    await user.click(screen.getByLabelText(/Always use this pipeline/))
    await user.click(screen.getByTestId('force'))
    await user.click(screen.getByTestId('run-pipeline'))
    await screen.findByText(/Pipeline run/)

    expect(writes).toEqual([{ pipeline_id: null }])
  })

  it('writes the default only when the tick actually changes it', async () => {
    withModelDefault(2)
    const writes = watchDefaultWrites()
    const { user } = open()
    await listed()

    // Ticked and unchanged on the pipeline that already is the default: nothing to write.
    await user.click(screen.getByTestId('force'))
    await user.click(screen.getByTestId('run-pipeline'))
    await screen.findByText(/Pipeline run/)

    expect(writes).toEqual([])
  })

  it('ignores an eligibility answer that arrives after the output has changed', async () => {
    // The panel is never unmounted (ActionBar always renders it), so a slow check for one
    // output could otherwise overwrite a newer one's badges.
    let call = 0
    server.use(
      http.post('/api/v1/print/outputs/:id/eligibility', async () => {
        call += 1
        const slow = call === 1
        if (slow) await delay(400)
        return HttpResponse.json({
          library_file_id: 8801,
          reports: [
            {
              pipeline_id: 1,
              report: slow
                ? // The stale answer: pipeline 1 is NOT ready for the older output.
                  {
                    ok: false,
                    target_kind: 'specific_printer',
                    target_printer_id: 1,
                    target_printer_name: '3DP-31B-598',
                    target_model_class: null,
                    issues: [{ kind: 'stale_answer', slot_index: null }],
                    printer_reports: [],
                  }
                : fixtures.eligibilityReports[1],
            },
          ],
        })
      }),
    )

    const first = { ...output, id: 'a'.repeat(32), library_file_id: undefined }
    const second = { ...output, id: 'b'.repeat(32), library_file_id: undefined }
    const { rerender } = renderPage(
      <PrintPicker open slug="name-keychain" output={first} onClose={vi.fn()} onRan={vi.fn()} />,
    )
    await screen.findByRole('radio', { name: TEXTURED })
    // Switch outputs while the first check is still in flight.
    rerender(
      <PrintPicker open slug="name-keychain" output={second} onClose={vi.fn()} onRan={vi.fn()} />,
    )

    await waitFor(() => expect(row(TEXTURED)).toHaveTextContent('ready'))
    // Long enough for the superseded request to land if it were going to be applied.
    await new Promise((resolve) => setTimeout(resolve, 500))
    expect(screen.queryByText(/stale answer/)).not.toBeInTheDocument()
    expect(row(TEXTURED)).not.toHaveTextContent('not ready')
  })

  it('marks a pipeline Bambuddy could not judge as unchecked, not as blocked', async () => {
    server.use(
      http.post('/api/v1/print/outputs/:id/eligibility', () =>
        HttpResponse.json({
          library_file_id: 8801,
          reports: [
            { pipeline_id: 1, report: null, error: 'Bambuddy answered 500: the slicer fell over' },
            { pipeline_id: 2, report: fixtures.eligibilityReports[2] },
          ],
        }),
      ),
    )
    open()
    await screen.findByRole('radio', { name: TEXTURED })

    // The row that could not be answered says so, and claims neither state.
    expect(await screen.findByTestId('uncheckable-1')).toHaveTextContent('the slicer fell over')
    expect(row(TEXTURED)).not.toHaveTextContent('ready')
    // The one that did answer is unaffected — a single failure does not blank the picker.
    expect(row(DRAFT)).toHaveTextContent('not ready')
    // And an unanswered check is not grounds for offering `force`: nothing was shown to
    // override, so Run just gets Bambuddy's own verdict.
    expect(screen.queryByTestId('force')).not.toBeInTheDocument()
  })

  it('reports a failure that hit every pipeline once, not once per row', async () => {
    // A bad API key or an unreachable Bambuddy fails every check with the same message;
    // repeating it per row says nothing extra and buries the actual problem.
    const detail = "Bambuddy refused the API key. The key needs the 'Manage Queue' scope"
    server.use(
      http.post('/api/v1/print/outputs/:id/eligibility', () =>
        HttpResponse.json({
          library_file_id: 8801,
          reports: fixtures.pipelineViews.map((pipeline) => ({
            pipeline_id: pipeline.id,
            report: null,
            error: detail,
          })),
        }),
      ),
    )
    open()
    await screen.findByRole('radio', { name: TEXTURED })

    expect(await screen.findByTestId('eligibility-unavailable')).toHaveTextContent(detail)
    // Said once, not three times.
    expect(screen.queryByTestId('uncheckable-1')).not.toBeInTheDocument()
    expect(screen.queryByTestId('uncheckable-2')).not.toBeInTheDocument()
    // And nothing claims to be ready or blocked off the back of an answer nobody got.
    expect(screen.queryByText(/^(ready|not ready)$/)).not.toBeInTheDocument()
  })

  it('still shows a blank reason as unchecked rather than as nothing at all', async () => {
    server.use(
      http.post('/api/v1/print/outputs/:id/eligibility', () =>
        HttpResponse.json({
          library_file_id: 8801,
          // The backend rejects a blank reason, so this is the belt to that braces: the
          // row is decided by whether a REPORT arrived, not by the error string's truth.
          reports: [
            { pipeline_id: 1, report: null, error: '' },
            { pipeline_id: 2, report: fixtures.eligibilityReports[2] },
          ],
        }),
      ),
    )
    open()
    await screen.findByRole('radio', { name: TEXTURED })

    expect(await screen.findByTestId('uncheckable-1')).toBeInTheDocument()
    expect(row(TEXTURED)).toHaveTextContent('not checked')
    expect(row(TEXTURED)).not.toHaveTextContent(/^ready/)
  })

  it('says so when Bambuddy has no pipelines at all', async () => {
    server.use(
      http.get('/api/v1/print/models/:slug/pipelines', () =>
        HttpResponse.json({
          pipelines: [],
          printers: [],
          model_pipeline_id: null,
          global_pipeline_id: null,
          default_pipeline_id: null,
        }),
      ),
    )
    open()

    expect(await screen.findByText(/no slicer pipelines yet/)).toBeInTheDocument()
    expect(screen.getByTestId('run-pipeline')).toBeDisabled()
  })
})

describe('PrintPicker · New pipeline', () => {
  beforeEach(() => resetMockState())

  it('builds one from a printer, process and per-colour filament preset plus a bed type', async () => {
    const { user } = open()
    await listed()
    await user.click(screen.getByTestId('new-pipeline'))

    // Process and filament stay empty until a printer preset is named: unfiltered they are
    // thousands of rows on a real Bambuddy.
    const process = await screen.findByLabelText('Process preset')
    expect(process).toBeDisabled()

    await user.selectOptions(
      screen.getByLabelText('Printer preset'),
      'cloud:GM041', // Bambu Lab H2C 0.4 nozzle
    )
    await waitFor(() => expect(screen.getByLabelText('Process preset')).toBeEnabled())
    // Only the 0.4-nozzle process preset is compatible with that printer preset.
    expect(
      screen.getByRole('option', { name: '0.20mm Standard @BBL H2C' }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('option', { name: /0.08mm High Quality/ }),
    ).not.toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('Process preset'), 'cloud:GP252')
    // One filament select per colour of the output, in slot order.
    await user.selectOptions(screen.getByLabelText('Filament for slot 1'), 'cloud:GFSA05_22')
    await user.selectOptions(screen.getByLabelText('Filament for slot 2'), 'local:2')
    await user.selectOptions(screen.getByLabelText('Bed type'), 'Textured PEI Plate')

    const create = screen.getByRole('button', { name: 'Create pipeline' })
    expect(create).toBeEnabled()
    await user.click(create)

    // Back to the list, with the new pipeline selected and re-checked for eligibility.
    const created = await screen.findByRole('radio', {
      name: /Bambu Lab H2C 0.4 nozzle · 0.20mm Standard/,
    })
    expect(created).toBeChecked()
  })

  it('keeps Create disabled until every slot has a filament preset', async () => {
    const { user } = open()
    await listed()
    await user.click(screen.getByTestId('new-pipeline'))
    await user.selectOptions(await screen.findByLabelText('Printer preset'), 'cloud:GM041')
    await waitFor(() => expect(screen.getByLabelText('Process preset')).toBeEnabled())
    await user.selectOptions(screen.getByLabelText('Process preset'), 'cloud:GP252')

    // The output has two colours; Bambuddy rejects a short filament list (minItems: 1,
    // and one per slot is what makes the plate slice).
    await user.selectOptions(screen.getByLabelText('Filament for slot 1'), 'cloud:GFSA05_22')

    expect(screen.getByRole('button', { name: 'Create pipeline' })).toBeDisabled()
  })
})
