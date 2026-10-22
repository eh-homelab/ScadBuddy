import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { FILAMENT_PLA } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { FilamentsEditor } from './FilamentsEditor'

// #4250 — the price book's filaments, under Settings' Orders section.
describe('FilamentsEditor (#4250)', () => {
  beforeEach(() => resetMockState())

  it('lists, adds, edits and archives the filaments', async () => {
    const { user } = renderPage(<FilamentsEditor />)
    const table = await screen.findByRole('table', { name: 'Filaments' })
    await within(table).findByText(/Sunflower Yellow/)
    const form = screen.getByRole('form', { name: 'Add a filament' })
    await user.type(within(form).getByLabelText('Material'), 'ABS')
    await user.clear(within(form).getByLabelText('Colour'))
    await user.type(within(form).getByLabelText('Colour'), '#123456')
    await user.type(within(form).getByLabelText('Cost per kg'), '22.75')
    await user.click(within(form).getByRole('button', { name: 'Add filament' }))
    await waitFor(async () => expect((await api.priceBook()).filaments.map((f) => f.material)).toContain('ABS'))
    await within(await screen.findByRole('table', { name: 'Filaments' })).findByText('#123456')

    const row = screen.getByTestId(`filament-${FILAMENT_PLA}`)
    await user.click(within(row).getByRole('button', { name: 'Edit' }))
    const edit = screen.getByRole('form', { name: 'Edit filament' })
    await user.clear(within(edit).getByLabelText('Cost per kg'))
    await user.type(within(edit).getByLabelText('Cost per kg'), '30')
    await user.click(within(edit).getByRole('button', { name: 'Save filament' }))
    await waitFor(async () => expect((await api.priceBook()).filaments.find((f) => f.id === FILAMENT_PLA)?.cost_per_kg).toBe('30'))

    await user.click(within(screen.getByTestId(`filament-${FILAMENT_PLA}`)).getByRole('button', { name: /^Archive/ }))
    await waitFor(() => expect(screen.queryByTestId(`filament-${FILAMENT_PLA}`)).toBeNull())
  })

  it('says what is wrong with a filament before sending it', async () => {
    const { user } = renderPage(<FilamentsEditor />)
    await screen.findByRole('table', { name: 'Filaments' })
    const form = screen.getByRole('form', { name: 'Add a filament' })
    await user.type(within(form).getByLabelText('Material'), 'PLA')
    await user.type(within(form).getByLabelText('Cost per kg'), 'cheap')
    await user.click(within(form).getByRole('button', { name: 'Add filament' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('cost per kilogram')
  })
})
