import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { T } from '../../../mocks/features/filament/seed'
import { renderPage } from '../../../test/utils'
import { PricePanel } from './PricePanel'

describe('the price panel', () => {
  it('offers "Use as default cost" for a price in the shop currency', async () => {
    const use = vi.fn()
    const { user } = renderPage(<PricePanel typeId={T.JADE} currency="USD" exponent={2} onUseAsCost={use} />)
    const button = await screen.findByRole('button', { name: /Use as default cost from/ })
    expect(button).toBeEnabled()
    await user.click(button)
    expect(use).toHaveBeenCalledTimes(1)
  })

  it('does not offer a price in another currency as the default cost', async () => {
    const use = vi.fn()
    renderPage(<PricePanel typeId={T.JADE} currency="EUR" exponent={2} onUseAsCost={use} />)
    const button = await screen.findByRole('button', { name: /Use as default cost from/ })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', expect.stringContaining('USD'))
  })
})
