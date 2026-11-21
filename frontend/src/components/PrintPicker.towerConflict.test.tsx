import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Output, PrintRunRequest } from '../api/types'
import { choicesView } from '../mocks/choices'
import { mockRackCheck } from '../mocks/features/rack'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { mockTowerCheck } from '../mocks/towerCheck'
import { choose } from '../test/select'
import { renderPage } from '../test/utils'
import { PrintPicker } from './PrintPicker'

// #6758 — the check judges the prime tower before the slice (`PrintCheck.prime_tower`), and
// "Move the prime tower" sends its fix as `wipe_tower_x`/`_y` process overrides.

const output = fixtures.outputs[0] as Output

beforeEach(() => resetMockState())
afterEach(() => vi.restoreAllMocks())

async function chooseNoSparseLayers(): Promise<void> {
  await screen.findByTestId('filament-slot-1')
  const advanced = screen.getByRole('switch', { name: 'Advanced' })
  if (advanced.getAttribute('aria-checked') !== 'true') fireEvent.click(advanced)
  fireEvent.click(await screen.findByRole('checkbox', { name: /No sparse layers/ }))
}

describe('PrintPicker · prime tower before the slice (#6758)', () => {
  it('shows the conflict, and Move sends the free spot to the check and the run', async () => {
    const check = vi.spyOn(api, 'checkPrint')
    const run = vi.spyOn(api, 'runPrint')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await chooseNoSparseLayers()

    const notice = await screen.findByTestId('tower-conflict')
    expect(notice).toHaveTextContent('The slicer will refuse this prime tower')
    expect(notice).toHaveTextContent(/Plate 1: the prime tower is closer to the objects/)
    expect(notice).toHaveTextContent('Free spot: x 157.2, y 5.')
    fireEvent.click(within(notice).getByRole('button', { name: 'Move the prime tower' }))

    await waitFor(() =>
      expect(check.mock.calls.at(-1)?.[1]).toMatchObject({
        process_overrides: { wipe_tower_x: ['157.243'], wipe_tower_y: ['5'] },
      }),
    )
    expect(await screen.findByTestId('tower-moved')).toHaveTextContent('The prime tower moves clear of the objects.')
    expect(screen.queryByTestId('tower-conflict')).not.toBeInTheDocument()

    const print = screen.getByRole('button', { name: /^Print$/ })
    await waitFor(() => expect(print).toBeEnabled())
    fireEvent.click(print)
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]?.[1]).toMatchObject({
      process_overrides: { wipe_tower_x: ['157.243'], wipe_tower_y: ['5'] },
    })
  })

  it("Remember leaves out the tower place, which belongs to this file's layout (#6796)", async () => {
    const run = vi.spyOn(api, 'runPrint')
    const remember = vi.spyOn(api, 'putModelChoices')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await chooseNoSparseLayers()
    fireEvent.click(await screen.findByRole('button', { name: 'Move the prime tower' }))
    await screen.findByTestId('tower-moved')

    const print = screen.getByRole('button', { name: /^Print$/ })
    await waitFor(() => expect(print).toBeEnabled())
    fireEvent.click(print)
    await waitFor(() => expect(run).toHaveBeenCalled())
    expect(run.mock.calls[0]?.[1]).toMatchObject({
      process_overrides: { wipe_tower_x: ['157.243'], wipe_tower_y: ['5'] },
    })
    await waitFor(() => expect(remember).toHaveBeenCalled())
    expect(remember.mock.calls.at(-1)?.[1].process_overrides ?? null).toBeNull()
  })

  it('Undo takes the move out again', async () => {
    const check = vi.spyOn(api, 'checkPrint')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await chooseNoSparseLayers()
    fireEvent.click(await screen.findByRole('button', { name: 'Move the prime tower' }))
    fireEvent.click(within(await screen.findByTestId('tower-moved')).getByRole('button', { name: 'Undo' }))

    await waitFor(() => expect(check.mock.calls.at(-1)?.[1]).not.toHaveProperty('process_overrides'))
    expect(await screen.findByTestId('tower-conflict')).toBeInTheDocument()
  })

  it('says nothing when the tower stands clear', async () => {
    const check = vi.spyOn(api, 'checkPrint')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await screen.findByTestId('filament-slot-1')
    await waitFor(() => expect(check).toHaveBeenCalled())
    expect(screen.queryByTestId('tower-conflict')).not.toBeInTheDocument()
    expect(screen.queryByTestId('tower-moved')).not.toBeInTheDocument()
  })

  it('shows no moved line for a tower place the user set, and keeps it through a Re-arrange', async () => {
    remember({ wipe_tower_x: '120', wipe_tower_y: '30' })
    const check = vi.spyOn(api, 'checkPrint')
    const { user } = renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await chooseNoSparseLayers()
    await waitFor(() =>
      expect(check.mock.calls.at(-1)?.[1]).toMatchObject({ prime_tower: { enabled: true, no_sparse_layers: true } }),
    )
    expect(screen.queryByTestId('tower-moved')).not.toBeInTheDocument()

    await choose(user, screen.getByLabelText('Arrange for'), 'Fewest filament swaps')
    await user.click(screen.getByRole('button', { name: 'Re-arrange for these spools' }))
    expect(await screen.findByText('Arranged onto 1 plate.')).toBeInTheDocument()
    // The dialog now checks the arranged output, with the user's place still set.
    await waitFor(() => expect(check.mock.calls.at(-1)?.[0]).not.toBe(output.id))
    expect(check.mock.calls.at(-1)?.[1]).toMatchObject({
      process_overrides: { wipe_tower_x: '120', wipe_tower_y: '30' },
    })
    expect(screen.queryByTestId('tower-moved')).not.toBeInTheDocument()
  })

  it('Undo gives back the tower place the user set before the move', async () => {
    remember({ wipe_tower_x: '200' })
    // The user's place stands in the keep-out: only the free spot clears it.
    server.use(
      http.post('/api/v1/print/outputs/:id/check', async ({ request }) => {
        const body = (await request.json()) as PrintRunRequest
        const moved = body.process_overrides?.['wipe_tower_x']?.toString() === '157.243'
        return HttpResponse.json({
          ...mockRackCheck(null),
          prime_tower: mockTowerCheck(moved ? body : { ...body, process_overrides: {} }),
        })
      }),
    )
    const check = vi.spyOn(api, 'checkPrint')
    renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await chooseNoSparseLayers()
    fireEvent.click(await screen.findByRole('button', { name: 'Move the prime tower' }))
    fireEvent.click(within(await screen.findByTestId('tower-moved')).getByRole('button', { name: 'Undo' }))

    await waitFor(() => expect(check.mock.calls.at(-1)?.[1].process_overrides).toEqual({ wipe_tower_x: '200' }))
    expect(await screen.findByTestId('tower-conflict')).toBeInTheDocument()
  })

  it('hides the moved line once the prime tower is off', async () => {
    const { user } = renderPage(<PrintPicker open source={{ kind: 'output', output }} onClose={vi.fn()} onRan={vi.fn()} />)
    await chooseNoSparseLayers()
    fireEvent.click(await screen.findByRole('button', { name: 'Move the prime tower' }))
    await screen.findByTestId('tower-moved')

    await choose(user, screen.getByLabelText('Prime tower'), 'Off')
    await waitFor(() => expect(screen.queryByTestId('tower-moved')).not.toBeInTheDocument())
  })
})

/** Remembered choices for the output that set these slicer settings by hand. */
function remember(processOverrides: Record<string, string>): void {
  server.use(
    http.get('/api/v1/print/outputs/:id/choices', () =>
      HttpResponse.json({
        ...choicesView,
        model_choices: { printer_id: null, filament_plan: [], process_overrides: processOverrides },
      }),
    ),
  )
}
