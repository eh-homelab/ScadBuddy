import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { Order, OrderItemDetail, Quote } from '../../api/types'
import { ItemsBulk, MAX_BULK_FULFIL } from './ItemsBulk'

// #4955 — the bulk fulfil route takes at most 50 lines (`api/orders.py` `MAX_BATCH_ITEMS`):
// past that the bar refuses, and says why.

const order = { id: 'o1', status: 'accepted', on_hold: false } as Order
const approved = (n: number) =>
  ({
    id: `i${n}`,
    kind: 'custom',
    title: `Item ${n}`,
    status: 'approved',
    quantity: 1,
    quotes: [{ id: `q${n}`, accepted: true, quantity: 1, created_at: '2026-11-08T10:00:00Z' } as Quote],
    fulfilments: [],
    reference: null,
    output_id: null,
    library_file: null,
    origin_archive_ids: [],
    revised_after_acceptance: false,
  }) as unknown as OrderItemDetail

function bar(count: number) {
  render(
    <ItemsBulk
      order={order}
      selected={Array.from({ length: count }, (_, n) => approved(n))}
      shipments={[]}
      unfulfilAllowed
      busy={false}
      onWriting={() => undefined}
      onClear={() => undefined}
      onChanged={() => undefined}
    />,
  )
}

describe('ItemsBulk (#4955)', () => {
  it('fulfils up to the route’s limit at once', () => {
    bar(MAX_BULK_FULFIL)
    expect(screen.getByRole('button', { name: 'Fulfil…' })).toBeEnabled()
  })

  it('refuses more than the route takes, and says why', () => {
    bar(MAX_BULK_FULFIL + 1)
    expect(screen.getByRole('button', { name: 'Fulfil…' })).toBeDisabled()
    expect(screen.getByTestId('bulk-actions')).toHaveTextContent(`Fulfil takes ${MAX_BULK_FULFIL} items at a time. Select fewer.`)
  })
})
