import { useState } from 'react'
import { api } from '../../api/client'
import type { OrderItem, PartSettingsMap, Quote, SliceQuoteRequest } from '../../api/types'
import { formatMoney, toMajor } from '../../lib/money'
import { useAsync } from '../../lib/useAsync'
import { orderItemSource } from '../../lib/printSource'
import { useDebounced } from '../../lib/useDebounced'
import { PrintPicker } from '../PrintPicker'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { byCreated, parseUnitPrice, recordQuote, UNIT_PRICE_HINT } from './price'
import { ArchiveQuote } from './ArchiveQuote'
import { QuoteWarning } from './QuoteWarning'
import { refusalText } from './refusal'
import type { SliceQuoteRun } from './useSliceQuotes'
import { elapsed, useNow } from './useNow'
import { When } from './When'

/** How long the typed price must stay still before the warning asks the route about it. */
export const CHECK_DEBOUNCE_MS = 300

/** A quote line's kind as the drawer names it (§3.4, #4250 Ruling 5). */
const LINE_KINDS: Record<Quote['lines'][number]['kind'], string> = {
  filament: 'Filament',
  machine_time: 'Machine time',
  setup: 'Setup',
  labour: 'Labour',
  markup: 'Markup',
  rounding: 'Rounding',
  manual: 'Price',
}

/**
 * A typed price for a line item (§3.4's `manual` quote, plan Ruling 1), a quote from a
 * slice of it (#4250: the print dialog in its quote mode, which slices every plate and
 * prices it with the price book), and the item's quotes with their lines and the accepted
 * one marked. `editable` while the order is draft or quoted, or past that while the item has
 * no accepted quote (#5349) or was revised after acceptance (#5350, `afterAcceptance.ts`
 * `priceOpen`). The slice quote runs on the
 * page (`slice`, #4693), so it outlives the dialog and this drawer. `acceptPending` offers
 * Accept on the pending quote alone, with nothing to record (#5541: a custom item, whose
 * price is its own field, added after acceptance or by the agent with its price `quoted`).
 * An item taken from a past print (#4777) also quotes from that print (`ArchiveQuote`).
 */
export function QuoteEditor({
  orderId,
  itemId,
  currency,
  exponent,
  quotes,
  editable,
  acceptPending = false,
  onChanged,
  item,
  slice,
  parts = '',
}: {
  orderId: string
  itemId: string
  currency: string
  /** The order's `minor_exponent`. */
  exponent: number
  quotes: Quote[]
  editable: boolean
  /** Offer Accept on the newest quote while none is accepted, though `editable` is false. */
  acceptPending?: boolean
  onChanged: () => void
  /** What a slice quote slices: the item's output or library file, and its colours' filaments. */
  item?: Pick<OrderItem, 'output_id' | 'reference' | 'colours'> & Partial<Pick<OrderItem, 'library_file' | 'origin_archive_ids'>>
  /** The page's slice quote of this item (`useSliceQuotes`): its run, and how to start one. */
  slice?: { run?: SliceQuoteRun; start: (body: SliceQuoteRequest) => Promise<void>; dismiss: () => void }
  /** #5543: the item's parts as a key (`part:per_unit,…`): the history's unit cost carries
   * them, so a change reads it again. */
  parts?: string
}) {
  const [slicing, setSlicing] = useState(false)
  const [fromPrint, setFromPrint] = useState(false)
  // What a slice quote slices: the item's output, or a library file item's file (#5544).
  const sliceSource = item ? orderItemSource(item) : null
  const [price, setPrice] = useState('')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // #5328, job cost spec §6.3: the warning judges the typed price, else the newest quote
  // not yet accepted: what is about to be sent. The route decides whether it warns
  // (`warning`, `below_cost`); the browser never works out the thin rule itself. Each
  // price, once typing stops, is a read of its own, so a job fulfilled meanwhile counts.
  // A typed price that does not parse warns about nothing: the pending quote is not what
  // the field holds (#5533).
  const slug = editable ? item?.reference?.slug : undefined
  const pending = quotes.some((q) => q.accepted) ? undefined : [...quotes].sort(byCreated).at(-1)
  const current = price.trim() ? parseUnitPrice(price, exponent) : (pending?.unit_price_minor ?? null)
  // #5554 — the newest quote is marked when there are several to tell apart.
  const newest = quotes.length > 1 ? [...quotes].sort(byCreated).at(-1)?.id : undefined
  // #5627 — a new slice quote opens on the newest one's settings per colour, so a quote
  // made again keeps them; with none, on the model's remembered ones.
  const lastSlice = quotes.filter((q) => q.source === 'slice').sort(byCreated).at(-1)
  const running = slice?.run && !slice.run.error ? slice.run : undefined
  const now = useNow(running !== undefined)
  const checked = useDebounced(current, CHECK_DEBOUNCE_MS)
  const history = useAsync(
    () => (slug && checked !== null ? api.unitCostHistory(slug, currency, checked, itemId) : Promise.resolve(null)),
    [slug, currency, checked, itemId, parts],
  )

  async function record(accept: boolean) {
    const minor = parseUnitPrice(price, exponent)
    if (minor === null) {
      setError(UNIT_PRICE_HINT)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await recordQuote(orderId, itemId, { unit_price_minor: minor, ...(label.trim() ? { label: label.trim() } : {}) }, accept)
      setPrice('')
      setLabel('')
      onChanged()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  async function accept(quoteId: string) {
    setBusy(true)
    setError(null)
    try {
      await api.acceptOrderQuote(orderId, itemId, quoteId)
      onChanged()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="flex flex-col gap-2" aria-label="Price">
      <h3 className="text-xs text-muted-foreground">Price</h3>
      {quotes.length > 0 && (
        <ul className="flex flex-col gap-1 text-[13px]">
          {quotes.map((quote) => (
            <li key={quote.id} className="flex flex-col gap-0.5" data-testid={`quote-${quote.id}`}>
              <span className="flex flex-wrap items-center gap-2">
                <span className="sb-num">{formatMoney(quote.unit_price_minor, currency, exponent)} × {quote.quantity} = {formatMoney(quote.total_minor, currency, exponent)}</span>
                <span className="text-muted-foreground">
                  {quote.source === 'slice' ? 'From a slice' : quote.source === 'archive' ? 'From a past print' : (quote.lines[0]?.label ?? quote.source)}
                  {quote.sliced_sha256 ? <span className="font-mono text-xs"> · {quote.sliced_sha256.slice(0, 12)}</span> : null}
                </span>
                <span className="text-xs text-muted-foreground">
                  Quoted <When at={quote.created_at} />
                </span>
                {quote.id === newest && <span className="text-xs text-foreground" data-testid="quote-newest">Newest</span>}
                {quote.accepted ? (
                  <span className="text-xs text-primary">Accepted</span>
                ) : (
                  (editable || (acceptPending && quote.id === pending?.id)) && (
                    <Button size="xs" variant="outline" disabled={busy} onClick={() => void accept(quote.id)}>
                      Accept
                    </Button>
                  )
                )}
              </span>
              {quote.source !== 'manual' && (
                <ul className="ml-3 flex flex-col text-xs text-muted-foreground" aria-label="Quote lines">
                  {quote.lines.map((line, index) => (
                    <li key={index} className="flex justify-between gap-3">
                      <span>
                        <span className="text-foreground">{LINE_KINDS[line.kind]}</span> {line.label}
                      </span>
                      <span className="sb-num">{formatMoney(line.amount_minor, currency, exponent)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
      {slice && running && (
        <p role="status" className="flex flex-wrap items-center gap-2 text-[13px] text-muted-foreground" data-testid="quote-running">
          <Spinner />
          {running.detached ? (
            <span>
              No answer from the quote started at <When at={running.startedAt} /> ({elapsed(running.startedAt, now)} ago): it may still be slicing, and shows here when it lands. If it never started, stop waiting and quote again.
            </span>
          ) : (
            <span>
              Slicing every plate and pricing it. Started <When at={running.startedAt} />, running for <span data-testid="quote-elapsed">{elapsed(running.startedAt, now)}</span>…
            </span>
          )}
          {/* No longer followed, so a failure would never show: the user may stop waiting. */}
          {running.detached && (
            <Button type="button" size="xs" variant="ghost" onClick={slice.dismiss}>
              Stop waiting
            </Button>
          )}
        </p>
      )}
      {slice?.run?.error && (
        <p role="alert" className="flex flex-wrap items-center gap-2 text-[13px] text-warn" data-testid="quote-error">
          <span>
            The slice quote failed: {slice.run.error}
          </span>
          <span className="text-xs text-muted-foreground" data-testid="quote-error-times">
            Started <When at={slice.run.startedAt} />
            {slice.run.finishedAt !== undefined && (
              <>
                , ended <When at={slice.run.finishedAt} />
              </>
            )}
          </span>
          <Button type="button" size="xs" variant="ghost" onClick={slice.dismiss}>
            Dismiss
          </Button>
        </p>
      )}
      {editable && item && slice && (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy || !(item.output_id || item.library_file) || (slice.run !== undefined && !slice.run.error)}
            onClick={() => setSlicing(true)}
            data-testid="quote-from-slice"
          >
            Quote from a slice…
          </Button>
          {(item.origin_archive_ids ?? []).length > 0 && (
            <Button type="button" size="sm" variant="outline" disabled={busy || fromPrint} onClick={() => setFromPrint(true)} data-testid="quote-from-print">
              Quote from the print…
            </Button>
          )}
          {!item.output_id && !item.library_file && (
            <span className="text-xs text-muted-foreground">Render the item first: a slice quote slices its output.</span>
          )}
        </div>
      )}
      {editable && item && fromPrint && (
        <ArchiveQuote
          orderId={orderId}
          itemId={itemId}
          colours={item.colours}
          onCancel={() => setFromPrint(false)}
          onQuoted={() => {
            setFromPrint(false)
            onChanged()
          }}
        />
      )}
      {item && slice && slicing && sliceSource && (
        <PrintPicker
          open
          source={sliceSource}
          onClose={() => setSlicing(false)}
          onRan={() => undefined}
          quote={{
            orderId,
            itemId,
            filaments: Object.fromEntries(item.colours.map((colour) => [String(colour.slot), colour.filament_id ?? null])),
            ...(lastSlice ? { partSettings: (lastSlice.part_settings ?? null) as PartSettingsMap | null } : {}),
            start: slice.start,
          }}
        />
      )}
      {editable && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            void record(true)
          }}
        >
          <div className="flex flex-col gap-1">
            <Label htmlFor={`price-${itemId}`}>Price per unit ({currency})</Label>
            <Input id={`price-${itemId}`} inputMode="decimal" className="w-32" value={price} onChange={(e) => setPrice(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor={`price-label-${itemId}`}>Label</Label>
            <Input id={`price-label-${itemId}`} className="w-40" placeholder="Quoted by hand" value={label} onChange={(e) => setLabel(e.target.value)} />
          </div>
          <Button type="submit" size="sm" disabled={busy}>
            Record and accept
          </Button>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void record(false)}>
            Record only
          </Button>
        </form>
      )}
      {editable && current !== null && (
        <QuoteWarning history={history.data} unitPrice={current} currency={currency} exponent={exponent} onUse={(minor) => setPrice(toMajor(minor, exponent))} />
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
