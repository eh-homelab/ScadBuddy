import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { OrderCharge } from '../../api/types'
import { formatMoney, toMajor, toMinor } from '../../lib/money'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { refusalText } from './refusal'

const KINDS: OrderCharge['kind'][] = ['shipping', 'discount', 'fee', 'tax']
type Row = { kind: OrderCharge['kind']; label: string; amount: string }

/** The order's charges (§3.2: shipping, discount, fee, tax), replaced whole while draft or quoted. */
export function ChargesEditor({
  orderId,
  currency,
  exponent,
  charges,
  editable,
  onChanged,
}: {
  orderId: string
  currency: string
  /** The order's `minor_exponent`: its minor unit's decimal places. */
  exponent: number
  charges: OrderCharge[]
  editable: boolean
  onChanged: () => void
}) {
  const [rows, setRows] = useState<Row[]>([])
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!dirty) setRows(charges.map((c) => ({ kind: c.kind, label: c.label, amount: toMajor(c.amount_minor, exponent) })))
  }, [charges, exponent, dirty])

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
                  {c.label} <span className="text-muted-foreground">({c.kind})</span>
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
    setRows((was) => was.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  return (
    <section className="flex flex-col gap-2" aria-label="Charges">
      <h3 className="text-xs text-muted-foreground">Charges</h3>
      <form
        className="flex flex-col gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          const body = []
          for (const row of rows) {
            const minor = toMinor(row.amount, exponent)
            if (minor === null || !row.label.trim()) {
              setError('Each charge needs a label and an amount; a discount is negative.')
              return
            }
            body.push({ kind: row.kind, label: row.label.trim(), amount_minor: minor })
          }
          setBusy(true)
          setError(null)
          api
            .updateOrder(orderId, { charges: body })
            .then(() => {
              setDirty(false)
              onChanged()
            })
            .catch((err: unknown) => setError(refusalText(err)))
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
                setRows((was) => was.filter((_, i) => i !== index))
              }}
            >
              Remove
            </Button>
          </div>
        ))}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              setDirty(true)
              setRows((was) => [...was, { kind: 'shipping', label: '', amount: '' }])
            }}
          >
            Add charge
          </Button>
          <Button type="submit" size="sm" disabled={busy || !dirty}>
            Save charges
          </Button>
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
