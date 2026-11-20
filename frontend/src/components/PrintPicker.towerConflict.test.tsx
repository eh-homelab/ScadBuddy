import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Output } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
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
})
