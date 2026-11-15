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

// #2998 — Advanced's "Slicer settings": any Bambu Studio process key, sent as
// `process_overrides` by the check, the run and Remember, and seeded from what was remembered.

const output = fixtures.outputs[0] as Output

beforeEach(() => resetMockState())
afterEach(() => vi.restoreAllMocks())

async function openSettings(): Promise<HTMLElement> {
  await screen.findByTestId('filament-slot-1')
  const advanced = screen.getByRole('switch', { name: 'Advanced' })
  if (advanced.getAttribute('aria-checked') !== 'true') fireEvent.click(advanced)
  fireEvent.click(await screen.findByTestId('slicer-settings-toggle'))
  const list = await screen.findByTestId('slicer-settings')
  await within(list).findByTestId('slicer-settings-skirt_loops-value')
  return list
}

function printButton() {
  return screen.getByRole('button', { name: /^Print$/ })
}

describe('PrintPicker · slicer settings (#2998)', () => {
  it('lists the process values and sends a change on the check, the run and Remember', async () => {
    const values = vi.spyOn(api, 'processValues')
    const check = vi.spyOn(api, 'checkPrint')
    const run = vi.spyOn(api, 'runPrint')
    const remember = vi.spyOn(api, 'putModelChoices')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    const list = await openSettings()
    expect(values).toHaveBeenCalled()
    expect(within(list).getByTestId('slicer-settings-skirt_loops-value')).toHaveAttribute('placeholder', '0')

    fireEvent.change(within(list).getByTestId('slicer-settings-skirt_loops-value'), { target: { value: '2' } })
    expect(screen.getByTestId('slicer-settings-toggle')).toHaveTextContent('(1 changed)')
    await waitFor(() => expect(check.mock.calls.at(-1)?.[1]).toMatchObject({ process_overrides: { skirt_loops: '2' } }))

    await waitFor(() => expect(printButton()).toBeEnabled())
    fireEvent.click(printButton())
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]?.[1]).toMatchObject({ process_overrides: { skirt_loops: '2' } })
    await waitFor(() =>
      expect(remember).toHaveBeenCalledWith(output.slug, expect.objectContaining({ process_overrides: { skirt_loops: '2' } })),
    )
  })

  it('sends no field when nothing is set', async () => {
    const check = vi.spyOn(api, 'checkPrint')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await screen.findByTestId('filament-slot-1')
    await waitFor(() => expect(check).toHaveBeenCalled())
    expect(check.mock.calls.at(-1)?.[1]).not.toHaveProperty('process_overrides')
  })

  it('opens in Advanced on the remembered settings', async () => {
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', () =>
        HttpResponse.json({
          ...choicesView,
          model_choices: { printer_id: null, filament_plan: [], process_overrides: { enable_prime_tower: '0' } },
        }),
      ),
    )
    const run = vi.spyOn(api, 'runPrint')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    expect(await screen.findByTestId('slicer-settings-toggle')).toHaveTextContent('(1 changed)')
    await waitFor(() => expect(printButton()).toBeEnabled())
    fireEvent.click(printButton())
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]?.[1]).toMatchObject({ process_overrides: { enable_prime_tower: '0' } })
  })
})
