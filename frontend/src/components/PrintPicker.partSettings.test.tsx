import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Output } from '../api/types'
import { choicesView } from '../mocks/choices'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { PrintPicker } from './PrintPicker'

// #5627 — "Settings for this colour" in the filament step: each control opens on the
// process's value (the mocked check's `process_part_settings`), only changed ones are
// sent, and they are remembered with the model's other choices.

const output = fixtures.outputs[0] as Output

beforeEach(() => resetMockState())
afterEach(() => vi.restoreAllMocks())

async function loaded() {
  await screen.findByRole('switch', { name: 'Advanced' })
  await screen.findByTestId('filament-slot-1')
}

/** Opens a slot's settings, and waits for the check that reads the process's values. */
async function openSlot(slot: number): Promise<HTMLElement> {
  fireEvent.click(await screen.findByTestId(`part-settings-toggle-${slot}`))
  const settings = await screen.findByTestId(`part-settings-${slot}`)
  await waitFor(() => expect(within(settings).getByLabelText(/Top shell layers/)).toBeEnabled())
  return settings
}

function printButton() {
  return screen.getByRole('button', { name: /^Print$/ })
}

describe('PrintPicker · settings per colour (#5627)', () => {
  it('opens each control on the process value and sends only what changed', async () => {
    const run = vi.spyOn(api, 'runPrint')
    const remember = vi.spyOn(api, 'putModelChoices')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await loaded()
    const slot = await openSlot(1)
    const top = within(slot).getByLabelText(/Top shell layers/)
    await waitFor(() => expect(top).toHaveValue(5))

    fireEvent.change(top, { target: { value: '8' } })
    // The process's own value is no change: unset again.
    fireEvent.change(within(slot).getByLabelText(/Wall loops/), { target: { value: '2' } })
    expect(screen.getByTestId('part-settings-toggle-1')).toHaveTextContent('(1 changed)')

    await waitFor(() => expect(printButton()).toBeEnabled())
    fireEvent.click(printButton())
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]?.[1]).toMatchObject({ part_settings: { '1': { top_shell_layers: 8 } } })
    await waitFor(() =>
      expect(remember).toHaveBeenCalledWith(
        output.slug,
        expect.objectContaining({ part_settings: { '1': { top_shell_layers: 8 } } }),
      ),
    )
  })

  it("opens a slot the template sets on the template's value, and sends the process's when chosen (#5626)", async () => {
    const real = api.checkPrint.bind(api)
    vi.spyOn(api, 'checkPrint').mockImplementation(async (outputId, body, processValues) => {
      const answer = await real(outputId, body, processValues)
      if (!processValues) return answer
      const process = { ...answer.process_part_settings, top_shell_layers: 3 }
      return { ...answer, process_part_settings: process, slot_part_settings: { '2': { ...process, top_shell_layers: 5 } } }
    })
    const run = vi.spyOn(api, 'runPrint')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await loaded()
    const slot = await openSlot(2)
    const top = within(slot).getByLabelText(/Top shell layers/)
    // What prints with nothing sent: the template's 5, not the process's 3.
    await waitFor(() => expect(top).toHaveValue(5))

    fireEvent.change(top, { target: { value: '3' } })
    expect(screen.getByTestId('part-settings-toggle-2')).toHaveTextContent('(1 changed)')
    await waitFor(() => expect(printButton()).toBeEnabled())
    fireEvent.click(printButton())
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]?.[1]).toMatchObject({ part_settings: { '2': { top_shell_layers: 3 } } })
  })

  it('keeps a value the user set when a later check reads a process whose default equals it', async () => {
    const real = api.checkPrint.bind(api)
    let flagged = 0
    vi.spyOn(api, 'checkPrint').mockImplementation(async (outputId, body, processValues) => {
      const answer = await real(outputId, body, processValues)
      if (!processValues) return answer
      flagged += 1
      // The first answer's process has 5 top shell layers; every later one, 8.
      return { ...answer, process_part_settings: { ...answer.process_part_settings, top_shell_layers: flagged === 1 ? 5 : 8 } }
    })
    const run = vi.spyOn(api, 'runPrint')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await loaded()
    const slot = await openSlot(1)
    const top = within(slot).getByLabelText(/Top shell layers/)
    await waitFor(() => expect(top).toHaveValue(5))

    fireEvent.change(top, { target: { value: '8' } })
    // The request changed, so the check reads again: now the default is 8 too.
    await waitFor(() => expect(flagged).toBeGreaterThan(1))
    await waitFor(() => expect(printButton()).toBeEnabled())
    expect(screen.getByTestId('part-settings-toggle-1')).toHaveTextContent('(1 changed)')
    fireEvent.click(printButton())
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]?.[1]).toMatchObject({ part_settings: { '1': { top_shell_layers: 8 } } })
  })

  it('sends a whole-percent density that differs from a fractional process value', async () => {
    const real = api.checkPrint.bind(api)
    vi.spyOn(api, 'checkPrint').mockImplementation(async (outputId, body, processValues) => {
      const answer = await real(outputId, body, processValues)
      if (!processValues) return answer
      // The process's 15.5% shows as 16.
      return {
        ...answer,
        process_part_settings: { ...answer.process_part_settings, sparse_infill_density: 16 },
        process_inexact: ['sparse_infill_density'],
      }
    })
    const run = vi.spyOn(api, 'runPrint')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await loaded()
    const slot = await openSlot(1)
    const density = within(slot).getByLabelText(/Sparse infill density/)
    await waitFor(() => expect(density).toHaveValue(16))

    // The field already shows 16: type another number, then 16 (the same text fires no change).
    fireEvent.focus(density)
    fireEvent.change(density, { target: { value: '1' } })
    fireEvent.change(density, { target: { value: '16' } })
    fireEvent.blur(density)
    expect(screen.getByTestId('part-settings-toggle-1')).toHaveTextContent('(1 changed)')
    await waitFor(() => expect(printButton()).toBeEnabled())
    fireEvent.click(printButton())
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]?.[1]).toMatchObject({ part_settings: { '1': { sparse_infill_density: 16 } } })
  })

  it('says a number out of range was kept within the bounds', async () => {
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await loaded()
    const slot = await openSlot(1)
    const density = within(slot).getByLabelText(/Sparse infill density/)
    fireEvent.focus(density)
    fireEvent.change(density, { target: { value: '150' } })
    expect(density).toHaveAttribute('aria-invalid', 'true')
    expect(within(slot).getByRole('status')).toHaveTextContent('Kept within 0–100.')
  })

  it('sends no field when nothing is changed, and the check sees what Print sends', async () => {
    const run = vi.spyOn(api, 'runPrint')
    const check = vi.spyOn(api, 'checkPrint')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await loaded()
    await waitFor(() => expect(check).toHaveBeenCalled())
    expect(check.mock.calls.at(-1)?.[1]).not.toHaveProperty('part_settings')
    // The process's values are asked for only once a slot's settings are open.
    expect(check.mock.calls.every((call) => !call[2])).toBe(true)

    const slot = await openSlot(1)
    expect(check.mock.calls.at(-1)?.[2]).toBe(true)
    fireEvent.change(within(slot).getByLabelText(/Bottom shell layers/), { target: { value: '6' } })
    await waitFor(() =>
      expect(check.mock.calls.at(-1)?.[1]).toMatchObject({ part_settings: { '1': { bottom_shell_layers: 6 } } }),
    )
    fireEvent.click(within(slot).getByTestId('part-settings-1-bottom_shell_layers-reset'))

    await waitFor(() => expect(printButton()).toBeEnabled())
    fireEvent.click(printButton())
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]?.[1]).not.toHaveProperty('part_settings')
  })

  it('opens on the model’s remembered settings', async () => {
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', () =>
        HttpResponse.json({
          ...choicesView,
          model_choices: { printer_id: null, filament_plan: [], part_settings: { '1': { wall_loops: 4 } } },
        }),
      ),
    )
    const run = vi.spyOn(api, 'runPrint')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await loaded()
    expect(await screen.findByTestId('part-settings-toggle-1')).toHaveTextContent('(1 changed)')
    await waitFor(() => expect(printButton()).toBeEnabled())
    fireEvent.click(printButton())
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]?.[1]).toMatchObject({ part_settings: { '1': { wall_loops: 4 } } })
  })

  it("drops a remembered setting equal to the process's once the process is read", async () => {
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', () =>
        HttpResponse.json({
          ...choicesView,
          model_choices: {
            printer_id: null,
            filament_plan: [],
            part_settings: { '1': { top_shell_layers: 5, wall_loops: 4 } },
          },
        }),
      ),
    )
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await loaded()
    expect(await screen.findByTestId('part-settings-toggle-1')).toHaveTextContent('(2 changed)')
    await openSlot(1)
    await waitFor(() => expect(screen.getByTestId('part-settings-toggle-1')).toHaveTextContent('(1 changed)'))
  })

  it('offers the controls for a library file too (#5628)', async () => {
    renderPage(
      <PrintPicker
        open
        source={{ kind: 'library', file: { id: 89, filename: 'bag-clip.3mf' } }}
        onClose={vi.fn()}
        onRan={vi.fn()}
      />,
    )
    await loaded()
    const slot = await openSlot(1)
    expect(within(slot).getByLabelText(/Top shell layers/)).toHaveValue(5)
  })

  it('opens a library file on its remembered settings (#5628)', async () => {
    server.use(
      http.get('/api/v1/print/library/:id/choices', () =>
        HttpResponse.json({
          ...choicesView,
          model_choices: { printer_id: null, filament_plan: [], part_settings: { '1': { wall_loops: 4 } } },
        }),
      ),
    )
    renderPage(
      <PrintPicker
        open
        source={{ kind: 'library', file: { id: 89, filename: 'bag-clip.3mf' } }}
        onClose={vi.fn()}
        onRan={vi.fn()}
      />,
    )
    await loaded()
    expect(await screen.findByTestId('part-settings-toggle-1')).toHaveTextContent('(1 changed)')
  })

  it('takes backspace and a new number in a field, and a cleared field is the process value on blur', async () => {
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await loaded()
    const slot = await openSlot(1)
    const walls = within(slot).getByLabelText(/Wall loops/)
    await waitFor(() => expect(walls).toHaveValue(2))
    fireEvent.focus(walls)
    fireEvent.change(walls, { target: { value: '' } })
    expect(walls).toHaveValue(null)
    fireEvent.change(walls, { target: { value: '4' } })
    expect(screen.getByTestId('part-settings-toggle-1')).toHaveTextContent('(1 changed)')
    fireEvent.change(walls, { target: { value: '' } })
    fireEvent.blur(walls)
    expect(walls).toHaveValue(2)
    expect(screen.getByTestId('part-settings-toggle-1')).not.toHaveTextContent('changed')
  })

  it('an item print opens on the quote’s settings, sends them as the background slice does, and holds Print when changed', async () => {
    const start = vi.fn(async () => undefined)
    const checked = vi.spyOn(api, 'checkPrint')
    renderPage(
      <PrintPicker
        open
        source={{ kind: 'output', output }}
        onClose={vi.fn()}
        onRan={vi.fn()}
        itemPrint={{ orderId: 'o1', itemId: 'i1', quoted: { '1': { top_shell_layers: 8 } }, start }}
      />,
    )
    await loaded()
    const slot = await openSlot(1)
    expect(within(slot).getByLabelText(/Top shell layers/)).toHaveValue(8)

    fireEvent.change(within(slot).getByLabelText(/Top shell layers/), { target: { value: '9' } })
    expect(await screen.findByTestId('part-settings-unquoted')).toBeInTheDocument()
    expect(screen.getByTestId('run-item-print')).toBeDisabled()

    fireEvent.click(screen.getByTestId('part-settings-use-quoted'))
    await waitFor(() => expect(screen.getByTestId('run-item-print')).toBeEnabled())
    fireEvent.click(screen.getByTestId('run-item-print'))
    await waitFor(() => expect(start).toHaveBeenCalled())
    const sent = (start.mock.calls[0] as unknown[])[0] as { part_settings?: unknown }
    expect(sent.part_settings).toEqual({ '1': { top_shell_layers: 8 } })
    // The same settings as the check's body, which the background slice is made from
    // (`previewRequest`), so the print reuses that slice.
    expect(checked).toHaveBeenCalled()
    for (const call of checked.mock.calls) expect(call[1].part_settings).toEqual(sent.part_settings)
  })
})
