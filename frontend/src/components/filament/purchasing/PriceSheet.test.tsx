import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { PricePoint, PriceSheetRow } from '../../../api/filamentPurchasing'
import { PriceSheet } from './PriceSheet'
import { changeLabel } from './priceFormat'
import type { ShopMoney } from './useMoney'

const money: ShopMoney = { currency: 'USD', exponent: 2, format: (amount, currency = 'USD') => `${currency} ${amount}` }

const point = (price: string, per_kg: string, on_date: string): PricePoint => ({
  id: `p-${price}`,
  product_id: 'prod',
  price,
  price_per_kg: per_kg,
  currency: 'USD',
  on_date,
  source: 'manual',
  invoice_line_id: null,
  created_at: `${on_date}T00:00:00Z`,
  updated_at: `${on_date}T00:00:00Z`,
})

const row = (id: string, code: string, newest: PricePoint | null, previous: PricePoint | null, change_pct: string | null): PriceSheetRow => ({
  product: { id, supplier_id: 's', type_id: `t-${id}`, sku: `SKU-${id}`, product_url: null, pack_size: 1, note: null, archived_at: null, created_at: '', updated_at: '' },
  type_code: code,
  type_label: code,
  newest,
  previous,
  change_pct,
})

const show = (rows: PriceSheetRow[]) =>
  render(
    <MemoryRouter>
      <PriceSheet rows={rows} types={new Map()} money={money} />
    </MemoryRouter>,
  )

describe('the price sheet', () => {
  it('says a change with its sign', () => {
    expect(changeLabel('10.0')).toBe('+10.0 %')
    expect(changeLabel('-5.2')).toBe('-5.2 %')
    expect(changeLabel('0.0')).toBe('+0.0 %')
  })

  it('shows the newest price, the previous one and the change chip', () => {
    show([row('a', 'JADE', point('16.50', '16.5000', '2026-10-01'), point('15.00', '15.0000', '2026-09-01'), '10.0')])
    const cells = within(screen.getByRole('table', { name: 'Price sheet' }))
    expect(cells.getByText('USD 16.50')).toBeInTheDocument()
    expect(cells.getByText('USD 15.00')).toBeInTheDocument()
    expect(cells.getByText('+10.0 %')).toBeInTheDocument()
  })

  it('shows a fall with a minus and a one-point product with a dash for the change', () => {
    show([
      row('a', 'PINK', point('14.25', '14.2500', '2026-10-01'), point('15.00', '15.0000', '2026-09-01'), '-5.0'),
      row('b', 'TPU', point('28.99', '28.9900', '2026-09-20'), null, null),
    ])
    expect(screen.getByText('-5.0 %')).toBeInTheDocument()
    const tpu = screen.getAllByRole('row').find((one) => one.dataset.product === 'b')!
    expect(within(tpu).getByLabelText('No change to show')).toHaveTextContent('—')
    expect(within(tpu).queryByText(/%/)).toBeNull()
  })

  it('invites a first product when the sheet is empty', () => {
    show([])
    expect(screen.getByText(/No products yet/)).toBeInTheDocument()
  })
})
