import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { ShippingOption } from '../../api/types'
import { toMajor, toMinor } from '../../lib/money'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { refusalText } from '../orders/refusal'

type Row = { id: string; label: string; price: string; ships: boolean }

/** `money.py` `ShippingOption.label`'s bound. */
const LABEL_MAX = 200
/** `money.py` `ShippingOption.id`. */
const OPTION_ID = /^[a-z0-9_-]{1,40}$/

function rowOf(o: ShippingOption, exponent: number): Row {
  return { id: o.id, label: o.label, price: toMajor(o.amount_minor, exponent), ships: o.ships }
}

function optionOf(row: Row, n: number, exponent: number): ShippingOption | string {
  const id = row.id.trim()
  if (!OPTION_ID.test(id)) return `Option ${n}: enter an id of 1 to 40 lowercase letters, digits, - or _, such as post.`
  const label = row.label.trim()
  if (!label) return `Option ${n}: enter the label the customer sees, such as Tracked post.`
  if ([...label].length > LABEL_MAX) return `Option ${n}: a label is at most ${LABEL_MAX} characters.`
  const price = toMinor(row.price, exponent)
  if (price === null || price < 0) return `Option ${n}: enter the price, such as 6.50, or 0.`
  return { id, label, amount_minor: price, ships: row.ships }
}

/**
 * #5281 (storefront spec §8) — Settings → Orders' shipping options
 * (`storefront_shipping_options`): an id, the label the customer sees, a fixed price in the
 * shop's currency, and whether it ships. One that ships needs the order's shipping address; a
 * pickup does not. Saved on its own.
 */
export function ShippingOptionsEditor() {
  const settings = useAsync(() => api.getSettings(), [])
  // Read again on `settings.changed`, so a new `orders_currency` relabels the amounts.
  const book = useAsync(() => api.priceBook(), [], ['settings'])
  const currency = book.data?.currency ?? 'USD'
  const exponent = book.data?.minor_exponent ?? 2
  const [rows, setRows] = useState<Row[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const loaded = settings.data !== undefined && book.data !== undefined
  useEffect(() => {
    if (loaded && rows === null) setRows((settings.data?.storefront_shipping_options ?? []).map((o) => rowOf(o, exponent)))
  }, [loaded, rows, settings.data, exponent])
  const shown = rows ?? []

  const edit = (index: number, patch: Partial<Row>) => {
    setSaved(false)
    setRows(shown.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  return (
    <section aria-label="Shipping options" className="space-y-2 rounded-md border p-3">
      <h4 className="text-sm font-medium">Shipping options</h4>
      <p className="text-xs text-muted-foreground">
        How an order reaches its customer, at a fixed price in {currency}. Choosing one on an order writes its shipping charge. An option that ships needs the order's shipping address; a pickup does
        not.
      </p>
      <form
        className="flex flex-col gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          const options: ShippingOption[] = []
          for (const [index, row] of shown.entries()) {
            const option = optionOf(row, index + 1, exponent)
            if (typeof option === 'string') {
              setError(option)
              return
            }
            // `settings_store.py` `_unique_option_ids`.
            const first = options.findIndex((o) => o.id === option.id)
            if (first >= 0) {
              setError(`Option ${index + 1}: the id ${option.id} is already used by option ${first + 1}.`)
              return
            }
            options.push(option)
          }
          setBusy(true)
          setError(null)
          api
            .putSettings({ storefront_shipping_options: options })
            .then((next) => {
              setRows((next.storefront_shipping_options ?? []).map((o) => rowOf(o, exponent)))
              setSaved(true)
            })
            .catch((err: unknown) => setError(refusalText(err)))
            .finally(() => setBusy(false))
        }}
      >
        {shown.length === 0 && <p className="text-[13px] text-muted-foreground">No shipping options.</p>}
        {shown.map((row, index) => {
          const n = index + 1
          return (
            <div key={index} className="flex flex-wrap items-end gap-2">
              <div className="flex flex-col gap-1">
                <Label htmlFor={`shipping-option-${n}-id`}>{`Option ${n} id`}</Label>
                <Input id={`shipping-option-${n}-id`} className="w-28" value={row.id} onChange={(e) => edit(index, { id: e.target.value })} />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor={`shipping-option-${n}-label`}>{`Option ${n} label`}</Label>
                <Input id={`shipping-option-${n}-label`} className="w-48" value={row.label} onChange={(e) => edit(index, { label: e.target.value })} />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor={`shipping-option-${n}-price`}>{`Option ${n} price (${currency})`}</Label>
                <Input id={`shipping-option-${n}-price`} inputMode="decimal" className="w-24" value={row.price} onChange={(e) => edit(index, { price: e.target.value })} />
              </div>
              <div className="flex items-center gap-2 pb-2">
                <Checkbox id={`shipping-option-${n}-ships`} aria-label={`Option ${n} ships`} checked={row.ships} onCheckedChange={(checked) => edit(index, { ships: checked === true })} />
                <Label htmlFor={`shipping-option-${n}-ships`}>Ships</Label>
              </div>
              <Button
                type="button"
                size="xs"
                variant="ghost"
                aria-label={`Remove option ${n}`}
                onClick={() => {
                  setSaved(false)
                  setRows(shown.filter((_, i) => i !== index))
                }}
              >
                Remove
              </Button>
            </div>
          )
        })}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!loaded}
            onClick={() => {
              setSaved(false)
              setRows([...shown, { id: '', label: '', price: '', ships: true }])
            }}
          >
            Add option
          </Button>
          <Button type="submit" size="sm" disabled={busy || !loaded}>
            Save shipping options
          </Button>
          {saved && <span className="text-xs text-muted-foreground">Saved.</span>}
        </div>
      </form>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
