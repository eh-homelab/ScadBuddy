import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { Order, OrderItemDetail, Output, PartSettingsMap, PrintRunRequest, Quote, Revision } from '../../api/types'
import { orderItemSource } from '../../lib/printSource'
import { PrintPicker } from '../PrintPicker'
import { quoteToPrint } from './price'
import { isKit, offersPrint } from './itemActions'
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
    // A LAN-only printer is named by its registry row alone (#5761 review).
    ...(setup.printer ? { printer: setup.printer } : {}),
    // No plan seed for a quote with none: the dialog keeps its own spool picks.
    ...(quote.filament_plan ? { filament_plan: quote.filament_plan as PrintRunRequest['filament_plan'] } : {}),
    choices: setup.choices,
    all_plates: true,
    ...(setup.prime_tower ? { prime_tower: setup.prime_tower } : {}),
    ...(setup.supports ? { supports: setup.supports } : {}),
    ...(setup.print_sequence ? { print_sequence: setup.print_sequence } : {}),
    // #2998: the quote's slicer settings, which its print sends again.
    ...(setup.process_overrides ? { process_overrides: setup.process_overrides } : {}),
    ...(quote.part_settings ? { part_settings: quote.part_settings as PartSettingsMap } : {}),
  } as PrintRunRequest
}

/** A slice quote's `choices` column (`orders/quote.py` `print_setup`). */
type QuotedSetup = {
  printer_id?: number | null
  printer?: string | null
  choices?: PrintRunRequest['choices'] | null
  prime_tower?: PrintRunRequest['prime_tower'] | null
  supports?: PrintRunRequest['supports'] | null
  print_sequence?: PrintRunRequest['print_sequence'] | null
  process_overrides?: PrintRunRequest['process_overrides'] | null
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
  if (!testRevision && isKit(item)) return <KitPrints item={item} onChanged={onChanged} />
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
 * #5701 PR 5 — a kit's outputs, each printed on its own in the plain print dialog. The prints
 * record no attempt of the item, so the shop records it under Fulfilment once every output
 * is printed. Each output shows its name and colours when the store answers for it. When the
 * render made more outputs than the order holds (`kit_count`), the page says to save the others
 * from the model's recent renders first.
 */
function KitPrints({ item, onChanged }: { item: OrderItemDetail; onChanged: () => void }) {
  const [printing, setPrinting] = useState<string | null>(null)
  const [details, setDetails] = useState<Record<string, Output>>({})
  const outputs = useMemo(() => item.kit_outputs ?? [], [item.kit_outputs])
  const count = Math.max(item.kit_count ?? 0, outputs.length)
  const slug = item.reference?.slug
  useEffect(() => {
    let live = true
    for (const id of outputs) {
      api.getOutput(id).then(
        (output) => live && setDetails((now) => ({ ...now, [id]: output })),
        () => undefined,
      )
    }
    return () => {
      live = false
    }
  }, [outputs])
  if (!slug) return null
  return (
    <div className="space-y-2 text-[13px]">
      <p className="text-muted-foreground">
        {`This item is a kit of ${count} outputs. Print each one, then record the attempt under Fulfilment.`}
      </p>
      {outputs.length < count && (
        <p role="note" className="text-amber-700 dark:text-amber-400">
          {`The order holds ${outputs.length} of them. Save the others from the model's recent renders, then print them too.`}
        </p>
      )}
      <ul aria-label="Outputs to print" className="divide-y rounded-md border">
        {outputs.map((id, index) => {
          const output = details[id]
          const label = output?.name || `Output ${index + 1}`
          return (
            <li key={id} className="flex items-center justify-between gap-2 px-2 py-1.5">
              <span className="flex min-w-0 items-center gap-2">
                <span className="truncate">{label}</span>
                {output?.colors && output.colors.length > 0 && (
                  <span className="flex shrink-0 gap-0.5" aria-label={`Colours ${output.colors.join(', ')}`}>
                    {output.colors.map((colour, at) => (
                      <span key={`${colour}-${at}`} className="size-3 rounded-full border" style={{ backgroundColor: colour }} />
                    ))}
                  </span>
                )}
              </span>
              <Button type="button" size="sm" variant="outline" aria-label={`Print output ${index + 1} of ${count}`} onClick={() => setPrinting(id)}>
                Print…
              </Button>
            </li>
          )
        })}
      </ul>
      {printing && <PrintPicker open source={{ kind: 'output', output: { id: printing, slug } }} onClose={() => setPrinting(null)} onRan={onChanged} />}
    </div>
  )
}

/**
 * The print dialog of one item, in its item mode: Print's, and the items card's bulk Print
 * (#4955), which opens one per selected item in turn. A kit (`isKit`) is not printed here.
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
