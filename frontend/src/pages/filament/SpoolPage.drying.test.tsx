import { screen, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { spoolId } from '../../mocks/features/filament/seed'
import { renderPage } from '../../test/utils'
import { SpoolPage } from './SpoolPage'

const open = (number: number) =>
  renderPage(
    <Routes>
      <Route path="/filament/spools/:id" element={<SpoolPage />} />
    </Routes>,
    { route: `/filament/spools/${spoolId(number)}` },
  )

describe('the spool page and drying', () => {
  it('shows the badge in the header and the hours outside dry storage in Storage', async () => {
    open(2)
    const header = await screen.findByRole('heading', { level: 1, name: /Spool #2\b/ })
    expect(within(header.parentElement!).getByText('Needs drying')).toBeInTheDocument()
    const storage = screen.getByRole('region', { name: 'Storage' })
    expect(within(storage).getByText('61.5 hours outside dry storage')).toBeInTheDocument()
  })

  it('shows neither the badge nor a warning for a spool that is fine', async () => {
    open(1)
    await screen.findByRole('heading', { level: 1, name: /Spool #1\b/ })
    expect(screen.queryByText('Needs drying')).toBeNull()
    expect(within(screen.getByRole('region', { name: 'Storage' })).getByText('No time outside dry storage')).toBeInTheDocument()
  })
})
