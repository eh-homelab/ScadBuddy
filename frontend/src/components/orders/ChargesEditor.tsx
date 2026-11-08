import { useEffect, useRef, useState } from 'react'
import type { Order, OrderCharge } from '../../api/types'
import { formatMoney, toMajor, toMinor } from '../../lib/money'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { refusalText } from './refusal'
import { chargesWarning, saveCharges, type AddMode } from './afterAcceptance'

const KINDS: OrderCharge['kind'][] = ['shipping', 'discount', 'fee', 'tax']
/** An editable charge; `shipping_option_id` is kept so a save does not drop the option (#5281). */
type Row = { kind: OrderCharge['kind']; label: string; amount: string; shipping_option_id?: string | null }

/** The order's charges (§3.2: shipping, discount, fee, tax), replaced whole while draft or
 * quoted, and (#5351) on an accepted, in-production or ready order not on hold, through
 * `PUT …/charges/after-acceptance` after a confirm that names the order's status. The auto
 * tax row (#5281), which ScadBuddy computes from `orders_tax_percent`, is shown read-only
 * with an "auto" tag and never sent: the backend keeps and recomputes it. */
export function ChargesEditor({
  order,
  charges,
  mode,
  onChanged,
  onDirtyChange,
}: {
  order: Pick<Order, 'id' | 'number' | 'status' | 'currency' | 'minor_exponent'>
  charges: OrderCharge[]
  /** `addMode(order)`: `open` saves with the order's PATCH, `after_acceptance` on its own
   * route after a confirm, null shows the charges read-only. */
  mode: AddMode | null
  onChanged: () => void
  /** Told when the editor gets unsaved rows and when it loses them (#5281: the shipping
   * option picker writes the same list, so it waits while they stand). */
  onDirtyChange?: (dirty: boolean) => void
}) {
  const { currency, minor_exponent: exponent } = order
  const editable = mode !== null
  const [rows, setRows] = useState<Row[]>([])
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // #5351: the save to an accepted order waits for a second click on the confirm.
  const [confirming, setConfirming] = useState(false)
  const auto = charges.find((c) => c.auto)
  useEffect(() => {
    if (!dirty) setRows(charges.filter((c) => !c.auto).map((c) => ({ kind: c.kind, label: c.label, amount: toMajor(c.amount_minor, exponent), shipping_option_id: c.shipping_option_id ?? null })))
  }, [charges, exponent, dirty])
  const told = useRef(onDirtyChange)
  useEffect(() => {
    told.current = onDirtyChange
  })
  useEffect(() => {
    told.current?.(dirty)
  }, [dirty])
  // The backend takes a `tax` row with the auto row's label for the auto row sent back as
  // read, and drops it (`Lifecycle._typed`): such a row needs another label.
  const clashes = (row: Row) => auto !== undefined && row.kind === 'tax' && row.label.trim() === auto.label
  const clash = rows.some(clashes)
  const autoRow = auto && (
    <p className="flex justify-between gap-2 text-[13px]" data-testid="auto-tax">
      <span>
        {auto.label} <Badge variant="secondary">auto</Badge>
      </span>
      <span className="sb-num">{formatMoney(auto.amount_minor, currency, exponent)}</span>
    </p>
  )

  if (!editable) {
    return (
      <section className="flex flex-col gap-1" aria-label="Charges">
        <h3 className="text-xs text-muted-foreground">Charges</h3>
        {charges.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">None.</p>
        ) : (
          <ul className="flex flex-col gap-0.5 text-[13px]">
            {charges.map((c) => (
              <li key={c.id} className="flex justify-between gap-2">
                <span>
                  {c.label} <span className="text-muted-foreground">({c.kind})</span> {c.auto && <Badge variant="secondary">auto</Badge>}
                </span>
                <span className="sb-num">{formatMoney(c.amount_minor, currency, exponent)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    )
  }

  const edit = (index: number, patch: Partial<Row>) => {
    setDirty(true)
    setConfirming(false)
    // A charge edited away from its option's label or price no longer claims the option.
    const unclaim = 'label' in patch || 'amount' in patch ? { shipping_option_id: null } : {}
    setRows((was) => was.map((row, i) => (i === index ? { ...row, ...patch, ...unclaim } : row)))
  }

  return (
    <section className="flex flex-col gap-2" aria-label="Charges">
      <h3 className="text-xs text-muted-foreground">Charges</h3>
      <form
        className="flex flex-col gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          if (clash) return
          const body = []
          for (const row of rows) {
            const minor = toMinor(row.amount, exponent)
            if (minor === null || !row.label.trim()) {
              setError('Each charge needs a label and an amount; a discount is negative.')
              return
            }
            // Only a shipping charge names an option; another kind drops it.
            const option = row.kind === 'shipping' ? row.shipping_option_id : null
            body.push({ kind: row.kind, label: row.label.trim(), amount_minor: minor, ...(option ? { shipping_option_id: option } : {}) })
          }
          if (mode === 'after_acceptance' && !confirming) {
            setConfirming(true)
            return
          }
          setBusy(true)
          setError(null)
          saveCharges(order.id, body, mode)
            .then(() => {
              setDirty(false)
              setConfirming(false)
              onChanged()
            })
            .catch((err: unknown) => {
              setError(refusalText(err))
              setConfirming(false)
            })
            .finally(() => setBusy(false))
        }}
      >
        {rows.map((row, index) => (
          <div key={index} className="flex flex-wrap items-center gap-2">
            <Select value={row.kind} onValueChange={(kind) => edit(index, { kind: kind as OrderCharge['kind'] })}>
              <SelectTrigger aria-label={`Charge ${index + 1} kind`} className="w-28">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                {KINDS.map((k) => (
                  <SelectItem key={k} value={k}>
                    {k}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input aria-label={`Charge ${index + 1} label`} className="w-40" value={row.label} onChange={(e) => edit(index, { label: e.target.value })} />
            <Input aria-label={`Charge ${index + 1} amount`} inputMode="decimal" className="w-24" value={row.amount} onChange={(e) => edit(index, { amount: e.target.value })} />
            <Button
              type="button"
              size="xs"
              variant="ghost"
              aria-label={`Remove charge ${index + 1}`}
              onClick={() => {
                setDirty(true)
                setConfirming(false)
                setRows((was) => was.filter((_, i) => i !== index))
              }}
            >
              Remove
            </Button>
            {clashes(row) && auto && (
              <p role="alert" aria-label={`Charge ${index + 1} label problem`} className="w-full text-xs text-warn">
                {`A typed tax needs a label other than “${auto.label}”, the automatic tax; this tax then replaces it.`}
              </p>
            )}
          </div>
        ))}
        {autoRow}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              setDirty(true)
              setConfirming(false)
              setRows((was) => [...was, { kind: 'shipping', label: '', amount: '' }])
            }}
          >
            Add charge
          </Button>
          {confirming && (
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setConfirming(false)}>
              Back
            </Button>
          )}
          {dirty && !confirming && (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setDirty(false)
                setError(null)
              }}
            >
              Discard
            </Button>
          )}
          <Button type="submit" size="sm" disabled={busy || !dirty || clash}>
            {confirming ? 'Change the accepted order' : 'Save charges'}
          </Button>
        </div>
        {confirming && (
          <p role="alert" aria-label="Confirm the change" className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px]">
            {chargesWarning(order)}
          </p>
        )}
      </form>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
