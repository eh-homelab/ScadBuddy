import { useState } from 'react'
import { api } from '../../api/client'
import type { OrderItem, Quote, SliceQuoteRequest } from '../../api/types'
import { formatMoney } from '../../lib/money'
import { PrintPicker } from '../PrintPicker'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { parseUnitPrice, recordQuote, UNIT_PRICE_HINT } from './price'
import { refusalText } from './refusal'
import type { SliceQuoteRun } from './useSliceQuotes'

function startTime(at: number): string {
  return new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

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
 * one marked. `editable` while the order is draft or quoted. The slice quote runs on the
 * page (`slice`, #4693), so it outlives the dialog and this drawer.
 */
export function QuoteEditor({
  orderId,
  itemId,
  currency,
  exponent,
  quotes,
  editable,
  onChanged,
  item,
  slice,
}: {
  orderId: string
  itemId: string
  currency: string
  /** The order's `minor_exponent`. */
  exponent: number
  quotes: Quote[]
  editable: boolean
  onChanged: () => void
  /** What a slice quote slices: the item's output and its colours' filaments. */
  item?: Pick<OrderItem, 'output_id' | 'reference' | 'colours'>
  /** The page's slice quote of this item (`useSliceQuotes`): its run, and how to start one. */
  slice?: { run?: SliceQuoteRun; start: (body: SliceQuoteRequest) => void; dismiss: () => void }
}) {
  const [slicing, setSlicing] = useState(false)
  const [price, setPrice] = useState('')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
                  {quote.source === 'slice' ? 'From a slice' : (quote.lines[0]?.label ?? quote.source)}
                  {quote.sliced_sha256 ? <span className="font-mono text-xs"> · {quote.sliced_sha256.slice(0, 12)}</span> : null}
                </span>
                {quote.accepted ? (
                  <span className="text-xs text-primary">Accepted</span>
                ) : (
                  editable && (
                    <Button size="xs" variant="outline" disabled={busy} onClick={() => void accept(quote.id)}>
                      Accept
                    </Button>
                  )
                )}
              </span>
              {quote.source === 'slice' && (
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
      {slice?.run && !slice.run.error && (
        <p role="status" className="flex items-center gap-2 text-[13px] text-muted-foreground" data-testid="quote-running">
          <Spinner />
          {slice.run.detached
            ? `Still slicing and pricing, since ${startTime(slice.run.startedAt)}: the quote shows here when it lands.`
            : `Slicing every plate and pricing it, since ${startTime(slice.run.startedAt)}…`}
        </p>
      )}
      {slice?.run?.error && (
        <p role="alert" className="flex flex-wrap items-center gap-2 text-[13px] text-warn" data-testid="quote-error">
          The slice quote failed: {slice.run.error}
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
            disabled={busy || !item.output_id || (slice.run !== undefined && !slice.run.error)}
            onClick={() => setSlicing(true)}
            data-testid="quote-from-slice"
          >
            Quote from a slice…
          </Button>
          {!item.output_id && (
            <span className="text-xs text-muted-foreground">Render the item first: a slice quote slices its output.</span>
          )}
        </div>
      )}
      {item?.output_id && slice && slicing && (
        <PrintPicker
          open
          source={{ kind: 'output', output: { id: item.output_id, slug: item.reference.slug } }}
          onClose={() => setSlicing(false)}
          onRan={() => undefined}
          quote={{
            orderId,
            itemId,
            filaments: Object.fromEntries(item.colours.map((colour) => [String(colour.slot), colour.filament_id ?? null])),
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
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
