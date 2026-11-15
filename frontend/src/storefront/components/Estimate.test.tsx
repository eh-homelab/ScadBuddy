import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ESTIMATE } from '../../mocks/features/storefrontPublic'
import { Estimate } from './Estimate'

describe('Estimate', () => {
  it('shows the visitor only the price, never the grams (owner, 2026-11-14)', () => {
    render(<Estimate estimate={ESTIMATE} />)
    expect(screen.getByRole('status')).toHaveTextContent('about $12.40, confirmed after we check it')
    expect(screen.queryByText(/ g\b|filament/)).toBeNull()
  })
})
