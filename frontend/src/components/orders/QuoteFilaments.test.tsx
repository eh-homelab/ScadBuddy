import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderPage } from '../../test/utils'
import { QuoteFilaments } from './QuoteFilaments'

describe('QuoteFilaments', () => {
  it('sends an empty price book to the Filament section, where it is edited', () => {
    renderPage(<QuoteFilaments slots={[]} filaments={[]} value={{}} onChange={() => {}} />)
    expect(screen.getByText(/The price book has no filaments yet/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Filament → Types' })).toHaveAttribute('href', expect.stringContaining('/filament/types'))
    expect(screen.queryByText(/Settings, Orders/)).toBeNull()
  })
})
