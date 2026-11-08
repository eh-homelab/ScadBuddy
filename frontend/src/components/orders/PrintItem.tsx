import { useState } from 'react'
import { api } from '../../api/client'
import type { Order, OrderItemDetail, PartSettingsMap, PrintRunRequest, Revision } from '../../api/types'
import { orderItemSource } from '../../lib/printSource'
import { PrintPicker } from '../PrintPicker'
import { quoteToPrint } from './price'
import { offersPrint } from './itemActions'
import { Button } from '@/components/ui/button'

/** The order states past acceptance, in which an item is made (§3.5). */
const IN_PRODUCTION = new Set(['accepted', 'in_production', 'ready', 'shipped'])
const ENDED = new Set(['closed', 'cancelled'])

/** Whether the newest revision can be test printed now: before the order is accepted
 * (#5280, spec §4.4), from the output its quote was made from. */
function offersTestPrint(order: Order, item: OrderItemDetail): boolean {
  return item.kind !== 'custom' && item.reference !== null && item.output_id !== null && !IN_PRODUCTION.has(order.status) && !ENDED.has(order.status) && !order.on_hold
}

/**
 * #4719 — Print an order item from its accepted quote (spec 2026-10-18 §5.4): the print
 * dialog, in its item mode, posts `POST /orders/{id}/items/{item}/print` with its choices;
 * the item becomes `queued` and then follows the print, which the order's own refresh
 * shows. A refusal (`requote_needed`, a print run's own) stays in the dialog.
 * With `testRevision` (#5280) it is the test print of that revision instead: one copy,
 * offered before the order is accepted, which counts for nothing.
 */
export function PrintItem({ order, item, onChanged, testRevision }: { order: Order; item: OrderItemDetail; onChanged: () => void; testRevision?: Revision }) {
  const [open, setOpen] = useState(false)
  const source = orderItemSource(item)
  // #5627 — the settings the print sends and slices with: the accepted quote's per colour,
  // or for a test print the newest slice quote of that revision (`test_print_check`).
  const quoted = quoteToPrint(item, testRevision?.id)
  if (!(testRevision ? offersTestPrint(order, item) : offersPrint(order, item)) || !source) return null
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)} data-testid={testRevision ? 'test-print-item' : 'print-item'}>
        {testRevision ? 'Test print' : item.status === 'failed' ? 'Print again…' : 'Print…'}
      </Button>
      {open && (
        <PrintPicker
          open
          source={source}
          onClose={() => setOpen(false)}
          onRan={() => undefined}
          itemPrint={{
            orderId: order.id,
            itemId: item.id,
            quoted: (quoted?.part_settings ?? null) as PartSettingsMap | null,
            start: async (body) => {
              if (testRevision) await api.testPrintRevision(order.id, item.id, testRevision.id, body as PrintRunRequest)
              else await api.printItem(order.id, item.id, body)
              onChanged()
            },
          }}
        />
      )}
    </>
  )
}
