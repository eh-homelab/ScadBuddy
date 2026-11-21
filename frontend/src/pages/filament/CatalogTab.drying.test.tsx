import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { listCatalog } from '../../api/filament'
import { M } from '../../mocks/features/filament/seed'
import { renderPage } from '../../test/utils'
import { CatalogTab } from './CatalogTab'

describe('the material editor and the time outside dry storage', () => {
  it('sets the most hours a material may stay outside dry storage', async () => {
    const { user } = renderPage(<CatalogTab />)
    const table = await screen.findByRole('table', { name: 'Materials' })
    const row = within(table).getAllByRole('row').find((one) => one.dataset.name === 'PLA')!
    await user.click(within(row).getByRole('button', { name: 'Edit PLA' }))
    const field = await screen.findByLabelText('Most hours outside dry storage')
    expect(field).toHaveDisplayValue('')
    await user.type(field, '72')
    await user.click(screen.getByRole('button', { name: /^Save/ }))
    await screen.findByRole('table', { name: 'Materials' })
    expect((await listCatalog('materials')).find((one) => one.id === M.PLA)?.max_hours_outside_dry).toBe('72')
  })
})
