import { screen, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import type { OrderItem } from '../../api/types'
import { itemPictureSources } from '../entities/attachmentMedia'
import { resetMockState } from '../../mocks/handlers'
import { ITEM_APPROVED, ORDER_ACCEPTED, OUTPUT_REAGAN, state } from '../../mocks/features/orders/state'
import { OrderPage } from '../../pages/OrderPage'
import { renderPage } from '../../test/utils'

// #8481 — an order line shows what the customer ordered: its own render, never the
// template's picture while it has one, and a link to that render's page.

const item = (fields: Partial<OrderItem> = {}) =>
  ({
    id: 'i1',
    kind: 'model',
    title: 'Cap Keychain',
    output_id: 'o'.repeat(32),
    delivered_attachment_id: null,
    reference: { slug: 'cap', commit: 'a'.repeat(40), inputs: {}, template_name: 'Cap Keychain' },
    ...fields,
  }) as unknown as OrderItem

describe('itemPictureSources (#8481)', () => {
  it("shows the item's render, then its plate cover, and never the template's picture", () => {
    const sources = itemPictureSources('ord', item(), [])
    expect(sources.map((s) => s.url)).toEqual([`/api/v1/outputs/${'o'.repeat(32)}/thumbnail`, `/api/v1/outputs/${'o'.repeat(32)}/plates/1/thumbnail`])
    expect(sources.map((s) => s.label)).not.toContain("Cap Keychain's picture")
  })

  it("falls back to the template's picture only with no output", () => {
    const sources = itemPictureSources('ord', item({ output_id: null }), [])
    expect(sources.map((s) => s.url)).toEqual(['/api/v1/models/cap/thumbnail'])
    expect(sources[0]!.label).toBe("Cap Keychain's picture")
  })
})

function render(route: string) {
  return renderPage(
    <Routes>
      <Route path="/orders/:id" element={<OrderPage />} />
    </Routes>,
    { route },
  )
}

describe("an order line's render (#8481)", () => {
  beforeEach(() => resetMockState())

  it('links the row picture and the drawer to the render, with its plates', async () => {
    render(`/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}`)
    const drawer = await screen.findByRole('dialog', { name: 'Name Keychain' })
    expect(within(drawer).getByRole('link', { name: 'Open the ordered design' })).toHaveAttribute('href', `/edit/${OUTPUT_REAGAN}`)
    expect(await within(drawer).findByRole('figure', { name: 'Plates' })).toBeInTheDocument()
    const row = screen.getByTestId(`order-item-${ITEM_APPROVED}`)
    expect(within(row).getByRole('link', { name: 'Open the render of Name Keychain', hidden: true })).toHaveAttribute('href', `/edit/${OUTPUT_REAGAN}`)
  })

  it('offers no render link for an item with no output', async () => {
    state.items.find((i) => i.id === ITEM_APPROVED)!.output_id = null
    render(`/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}`)
    const drawer = await screen.findByRole('dialog', { name: 'Name Keychain' })
    expect(within(drawer).queryByRole('link', { name: 'Open the ordered design' })).toBeNull()
    const row = screen.getByTestId(`order-item-${ITEM_APPROVED}`)
    expect(within(row).queryByRole('link', { name: /Open the render/, hidden: true })).toBeNull()
  })
})
