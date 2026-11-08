import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { DepositRule } from '../../api/types'
import { toMajor, toMinor } from '../../lib/money'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { refusalText } from '../orders/refusal'

type Criterion = DepositRule['criterion']
type Row = { criterion: Criterion; above: string; type: 'percent' | 'amount'; value: string }

const CRITERIA: { value: Criterion; label: string }[] = [
  { value: 'has_request', label: 'Has a design request' },
  { value: 'total_above', label: 'Total above' },
  { value: 'first_order', label: "Customer's first order" },
]
const PERCENT = /^\d+(\.\d+)?$/
const REQUEST_NOTE = 'A design request has no estimate, so a percent counts it as zero: use a fixed amount'

function rowOf(rule: DepositRule, exponent: number): Row {
  return {
    criterion: rule.criterion,
    above: rule.above_minor != null ? toMajor(rule.above_minor, exponent) : '',
    type: rule.percent != null ? 'percent' : 'amount',
    value: rule.percent ?? (rule.amount_minor != null ? toMajor(rule.amount_minor, exponent) : ''),
  }
}

/** The rule a row makes, or what to fix in it. */
function ruleOf(row: Row, n: number, exponent: number): DepositRule | string {
  const rule: DepositRule = { criterion: row.criterion }
  if (row.criterion === 'total_above') {
    const above = toMinor(row.above, exponent)
    if (above === null || above < 0) return `Rule ${n}: enter the total it is above, such as 100.00.`
    rule.above_minor = above
  }
  if (row.type === 'percent') {
    const text = row.value.trim()
    if (!PERCENT.test(text) || Number(text) <= 0 || Number(text) > 100) return `Rule ${n}: enter a percent above 0 and at most 100, such as 50.`
    rule.percent = text
  } else {
    const amount = toMinor(row.value, exponent)
    if (amount === null || amount < 1) return `Rule ${n}: enter a fixed amount, such as 20.00.`
    rule.amount_minor = amount
  }
  return rule
}

/**
 * #5281 (storefront spec §8) — Settings → Orders' deposit policy (`orders_deposit_policy`):
 * rules of a criterion (a design request, a total above a threshold, the customer's first
 * order) and a percent of the order's total or a fixed amount in the shop's currency. The
 * largest amount of the rules that apply is the deposit. Saved on its own.
 */
export function DepositPolicyEditor() {
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
    if (loaded && rows === null) setRows((settings.data?.orders_deposit_policy ?? []).map((rule) => rowOf(rule, exponent)))
  }, [loaded, rows, settings.data, exponent])
  const shown = rows ?? []

  const edit = (index: number, patch: Partial<Row>) => {
    setSaved(false)
    setRows(shown.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  return (
    <section aria-label="Deposit policy" className="space-y-2 rounded-md border p-3">
      <h4 className="text-sm font-medium">Deposit policy</h4>
      <p className="text-xs text-muted-foreground">
        What an order asks before production. Of the rules that apply, the largest amount is the deposit. A percent is of the order's total; a fixed amount and a threshold are in{' '}
        {currency} and apply only to an order in it. "Use the policy" on an order sets its deposit from these rules.
      </p>
      <form
        className="flex flex-col gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          const rules: DepositRule[] = []
          for (const [index, row] of shown.entries()) {
            const rule = ruleOf(row, index + 1, exponent)
            if (typeof rule === 'string') {
              setError(rule)
              return
            }
            rules.push(rule)
          }
          setBusy(true)
          setError(null)
          api
            .putSettings({ orders_deposit_policy: rules })
            .then((next) => {
              setRows((next.orders_deposit_policy ?? []).map((rule) => rowOf(rule, exponent)))
              setSaved(true)
            })
            .catch((err: unknown) => setError(refusalText(err)))
            .finally(() => setBusy(false))
        }}
      >
        {shown.length === 0 && <p className="text-[13px] text-muted-foreground">No rules: an order asks no deposit unless one is set on it.</p>}
        {shown.map((row, index) => {
          const n = index + 1
          return (
            <div key={index} className="flex flex-col gap-1">
              <div className="flex flex-wrap items-end gap-2">
                <Select value={row.criterion} onValueChange={(value) => edit(index, { criterion: value as Criterion })}>
                  <SelectTrigger aria-label={`Rule ${n} criterion`} className="w-48">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent position="popper">
                    {CRITERIA.map((c) => (
                      <SelectItem key={c.value} value={c.value}>
                        {c.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {row.criterion === 'total_above' && (
                  <div className="flex flex-col gap-1">
                    <Label htmlFor={`deposit-rule-${n}-above`}>{`Rule ${n} above (${currency})`}</Label>
                    <Input id={`deposit-rule-${n}-above`} inputMode="decimal" className="w-28" value={row.above} onChange={(e) => edit(index, { above: e.target.value })} />
                  </div>
                )}
                <Select value={row.type} onValueChange={(value) => edit(index, { type: value as Row['type'] })}>
                  <SelectTrigger aria-label={`Rule ${n} amount type`} className="w-36">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent position="popper">
                    <SelectItem value="percent">Percent</SelectItem>
                    <SelectItem value="amount">Fixed amount</SelectItem>
                  </SelectContent>
                </Select>
                <div className="flex flex-col gap-1">
                  <Label htmlFor={`deposit-rule-${n}-value`}>{row.type === 'percent' ? `Rule ${n} percent` : `Rule ${n} amount (${currency})`}</Label>
                  <Input id={`deposit-rule-${n}-value`} inputMode="decimal" className="w-28" value={row.value} onChange={(e) => edit(index, { value: e.target.value })} />
                </div>
                <Button
                  type="button"
                  size="xs"
                  variant="ghost"
                  aria-label={`Remove rule ${n}`}
                  onClick={() => {
                    setSaved(false)
                    setRows(shown.filter((_, i) => i !== index))
                  }}
                >
                  Remove
                </Button>
              </div>
              {row.criterion === 'has_request' && <p className="text-xs text-muted-foreground">{REQUEST_NOTE}</p>}
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
              setRows([...shown, { criterion: 'has_request', above: '', type: 'amount', value: '' }])
            }}
          >
            Add rule
          </Button>
          <Button type="submit" size="sm" disabled={busy || !loaded}>
            Save deposit policy
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
