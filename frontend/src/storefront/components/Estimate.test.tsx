import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ESTIMATE, NO_PRICE, PENDING_PRICE } from '../../mocks/features/storefrontPublic'
import { Estimate } from './Estimate'

describe('Estimate', () => {
  it('shows a slice price as firm, and only the price (#6153)', () => {
    render(<Estimate estimate={ESTIMATE} />)
    expect(screen.getByRole('status')).toHaveTextContent('$12.40 for one')
    expect(screen.queryByText(/about|confirmed|check it/)).toBeNull()
    expect(screen.queryByText(/ g\b|filament|hour|minute/)).toBeNull()
  })

  it('says the price is being worked out while the slice runs', () => {
    render(<Estimate estimate={PENDING_PRICE} />)
    expect(screen.getByRole('status')).toHaveTextContent('Working out the price…')
    expect(screen.queryByText(/\$/)).toBeNull()
  })

  it('shows no number when there is no firm price: the shop prices it by hand', () => {
    render(<Estimate estimate={NO_PRICE} />)
    expect(screen.getByRole('status')).toHaveTextContent('We price this design by hand')
    expect(screen.queryByText(/\$/)).toBeNull()
  })
})
