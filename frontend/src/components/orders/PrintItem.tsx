import { useMemo, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { Order, OrderItemDetail, PartSettingsMap, PrintRunRequest, Quote, Revision } from '../../api/types'
import { orderItemSource } from '../../lib/printSource'
import { PrintPicker } from '../PrintPicker'
import { quoteToPrint } from './price'
import { offersPrint } from './itemActions'
import { Button } from '@/components/ui/button'

/** #4673 — what a print refused because its setup differs from the quote's
 * (`reason: "setup_differs"`) adds: the steps this page offers. An item's print with no
 * choices prints the quote's setup, which the assistant can send; this dialog always
 * sends its own (it is reworked in a later step of #4673). */
export const REQUOTE_HINT = "Quote the item again, or ask the assistant to print it with the quote's setup."

function withHint(cause: unknown): unknown {
  if (!(cause instanceof ApiError) || (cause.problem as { reason?: string }).reason !== 'setup_differs') return cause
  return new ApiError({ ...cause.problem, detail: `${cause.detail.replace(/\.?$/, '.')} ${REQUOTE_HINT}` })
}

/**
 * #4673 — the quote's recorded print setup as the dialog's seed (a slice quote's
 * `choices` and `filament_plan`, order operations spec §5.4), so Print sends what was
 * quoted. None for a quote that records no setup (manual, archive, or older).
 */
function quotedSeed(quote: Quote | undefined): PrintRunRequest | null {
  const setup = quote?.choices as QuotedSetup | null | undefined
  if (!quote || !setup?.choices) return null
  return {
    printer_id: setup.printer_id ?? undefined,
    filament_plan: (quote.filament_plan ?? { slots: [] }) as PrintRunRequest['filament_plan'],
    choices: setup.choices,
    all_plates: true,
    ...(setup.prime_tower ? { prime_tower: setup.prime_tower } : {}),
    ...(setup.supports ? { supports: setup.supports } : {}),
    ...(setup.print_sequence ? { print_sequence: setup.print_sequence } : {}),
    ...(quote.part_settings ? { part_settings: quote.part_settings as PartSettingsMap } : {}),
  } as PrintRunRequest
}

/** A slice quote's `choices` column (`orders/quote.py` `print_setup`). */
type QuotedSetup = {
  printer_id?: number | null
  choices?: PrintRunRequest['choices'] | null
  prime_tower?: PrintRunRequest['prime_tower'] | null
  supports?: PrintRunRequest['supports'] | null
  print_sequence?: PrintRunRequest['print_sequence'] | null
}

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
  if (!(testRevision ? offersTestPrint(order, item) : offersPrint(order, item)) || !orderItemSource(item)) return null
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)} data-testid={testRevision ? 'test-print-item' : 'print-item'}>
        {testRevision ? 'Test print' : item.status === 'failed' ? 'Print again…' : 'Print…'}
      </Button>
      {open && <ItemPrintDialog order={order} item={item} testRevision={testRevision} onClose={() => setOpen(false)} onChanged={onChanged} />}
    </>
  )
}

/**
 * The print dialog of one item, in its item mode: Print's, and the items card's bulk Print
 * (#4955), which opens one per selected item in turn.
 */
export function ItemPrintDialog({
  order,
  item,
  testRevision,
  onClose,
  onChanged,
}: {
  order: Order
  item: OrderItemDetail
  testRevision?: Revision
  onClose: () => void
  onChanged: () => void
}) {
  const source = orderItemSource(item)
  // #5627 — the settings the print sends and slices with: the accepted quote's per colour,
  // or for a test print the newest slice quote of that revision (`test_print_check`).
  const quoted = quoteToPrint(item, testRevision?.id)
  const seed = useMemo(() => quotedSeed(quoted), [quoted])
  if (!source) return null
  return (
    <PrintPicker
      open
      source={source}
      onClose={onClose}
      onRan={() => undefined}
      itemPrint={{
        orderId: order.id,
        itemId: item.id,
        quoted: (quoted?.part_settings ?? null) as PartSettingsMap | null,
        seed,
        start: async (body) => {
          try {
            if (testRevision) await api.testPrintRevision(order.id, item.id, testRevision.id, body as PrintRunRequest)
            else await api.printItem(order.id, item.id, body)
          } catch (cause) {
            throw withHint(cause)
          }
          onChanged()
        },
      }}
    />
  )
}
