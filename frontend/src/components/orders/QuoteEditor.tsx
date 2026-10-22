import { useState } from 'react'
import { api } from '../../api/client'
import type { Quote } from '../../api/types'
import { formatMoney, toMinor } from '../../lib/money'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { refusalText } from './refusal'

/**
 * A typed price for a line item (§3.4's `manual` quote, plan Ruling 1), and the item's
 * quotes with the accepted one marked. `editable` while the order is draft or quoted.
 */
export function QuoteEditor({
  orderId,
  itemId,
  currency,
  exponent,
  quotes,
  editable,
  onChanged,
}: {
  orderId: string
  itemId: string
  currency: string
  /** The order's `minor_exponent`. */
  exponent: number
  quotes: Quote[]
  editable: boolean
  onChanged: () => void
}) {
  const [price, setPrice] = useState('')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function record(accept: boolean) {
    const minor = toMinor(price, exponent)
    if (minor === null || minor < 0) {
      setError('Enter a price per unit, such as 12.50.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const quote = await api.quoteOrderItem(orderId, itemId, { unit_price_minor: minor, ...(label.trim() ? { label: label.trim() } : {}) })
      if (accept) await api.acceptOrderQuote(orderId, itemId, quote.id)
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
            <li key={quote.id} className="flex flex-wrap items-center gap-2">
              <span className="sb-num">{formatMoney(quote.unit_price_minor, currency, exponent)} × {quote.quantity} = {formatMoney(quote.total_minor, currency, exponent)}</span>
              <span className="text-muted-foreground">{quote.lines[0]?.label ?? quote.source}</span>
              {quote.accepted ? (
                <span className="text-xs text-primary">Accepted</span>
              ) : (
                editable && (
                  <Button size="xs" variant="outline" disabled={busy} onClick={() => void accept(quote.id)}>
                    Accept
                  </Button>
                )
              )}
            </li>
          ))}
        </ul>
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
