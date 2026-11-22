import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../../api/client'
import { invoiceConflictId, type InvoiceDetail, type InvoicePatch, type Supplier } from '../../../api/filamentPurchasing'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cmpDecimals, parseDecimal } from '../../../lib/decimal'
import { filamentLinks } from '../../../lib/filamentLinks'

type MoneyField = 'subtotal' | 'shipping' | 'tax' | 'total'
const MONEY: { field: MoneyField; label: string }[] = [
  { field: 'subtotal', label: 'Subtotal' },
  { field: 'shipping', label: 'Shipping' },
  { field: 'tax', label: 'Tax' },
  { field: 'total', label: 'Total' },
]

interface Draft {
  number: string
  invoice_date: string
  currency: string
  subtotal: string
  shipping: string
  tax: string
  total: string
  note: string
}

const draftOf = (invoice: InvoiceDetail): Draft => ({
  number: invoice.number ?? '',
  invoice_date: invoice.invoice_date,
  currency: invoice.currency,
  subtotal: invoice.subtotal,
  shipping: invoice.shipping,
  tax: invoice.tax,
  total: invoice.total,
  note: invoice.note ?? '',
})

/**
 * #6266 — an invoice's header: number, date, currency, the four amounts from the receipt
 * (typed as decimal text, read through `parseDecimal`, a bad number an inline error) and a
 * note. Each field saves on blur; the supplier is a choice that saves at once (a draft can move
 * to another supplier). A confirmed invoice is frozen but for its note (`editable` false). A
 * field with unsent text is never overwritten by an answer.
 */
export function InvoiceHeaderForm({
  invoice,
  suppliers,
  editable,
  onSave,
}: {
  invoice: InvoiceDetail
  suppliers: Supplier[]
  editable: boolean
  /** Sends one change; rejects with the server's refusal. The page orders the answers. */
  onSave: (patch: InvoicePatch) => Promise<unknown>
}) {
  const [draft, setDraft] = useState(() => draftOf(invoice))
  const [problems, setProblems] = useState<Partial<Record<keyof Draft, string>>>({})
  const [refused, setRefused] = useState<{ detail: string; existing: string | null } | null>(null)

  // Fields with text the server has not been sent, which an answer must not overwrite.
  const edited = useRef(new Set<keyof Draft>())

  // The server's answer (a money amount comes back with its own digits) replaces the fields
  // the person is not editing.
  const { number, invoice_date, currency, subtotal, shipping, tax, total, note } = invoice
  useEffect(() => {
    const server = draftOf({ number, invoice_date, currency, subtotal, shipping, tax, total, note } as InvoiceDetail)
    setDraft((now) => {
      const next = { ...now }
      for (const field of Object.keys(server) as (keyof Draft)[]) if (!edited.current.has(field)) next[field] = server[field]
      return next
    })
  }, [number, invoice_date, currency, subtotal, shipping, tax, total, note])

  const set = (field: keyof Draft, value: string) => {
    edited.current.add(field)
    setDraft((now) => ({ ...now, [field]: value }))
    setProblems((now) => ({ ...now, [field]: undefined }))
  }

  async function save(field: keyof Draft) {
    const text = draft[field].trim()
    let patch: InvoicePatch
    if (field === 'subtotal' || field === 'shipping' || field === 'tax' || field === 'total') {
      const parsed = parseDecimal(text)
      if (parsed === null) {
        setProblems((now) => ({ ...now, [field]: 'Enter an amount such as 12.50.' }))
        return
      }
      if (cmpDecimals(parsed, invoice[field]) === 0) return
      patch = { [field]: parsed }
    } else if (field === 'invoice_date') {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) {
        setProblems((now) => ({ ...now, invoice_date: 'Enter the date as year-month-day.' }))
        return
      }
      if (text === invoice.invoice_date) return
      patch = { invoice_date: text }
    } else if (field === 'currency') {
      const code = text.toUpperCase()
      if (!/^[A-Z]{3}$/.test(code)) {
        setProblems((now) => ({ ...now, currency: 'Enter a three-letter currency code, such as USD.' }))
        return
      }
      if (code === invoice.currency) return
      patch = { currency: code }
    } else if (field === 'number') {
      if (text === (invoice.number ?? '')) return
      patch = { number: text || null }
    } else {
      if (text === (invoice.note ?? '')) return
      patch = { note: text || null }
    }
    setRefused(null)
    // Sent: what is typed from here on is new text, and the answer may replace this.
    edited.current.delete(field)
    try {
      await onSave(patch)
    } catch (cause) {
      setRefused({ detail: cause instanceof ApiError ? cause.detail : 'The change could not be saved. Try again.', existing: invoiceConflictId(cause) })
    }
  }

  async function moveTo(supplierId: string) {
    if (supplierId === invoice.supplier_id) return
    setRefused(null)
    try {
      await onSave({ supplier_id: supplierId })
    } catch (cause) {
      setRefused({ detail: cause instanceof ApiError ? cause.detail : 'The change could not be saved. Try again.', existing: invoiceConflictId(cause) })
    }
  }

  const input = (field: keyof Draft, label: string, extra: { type?: string; inputMode?: 'decimal'; className?: string } = {}) => (
    <div className={`flex min-w-0 flex-col gap-1 ${extra.className ?? ''}`}>
      <Label htmlFor={`invoice-${field}`} className="text-[13px]">
        {label}
      </Label>
      <Input
        id={`invoice-${field}`}
        type={extra.type}
        inputMode={extra.inputMode}
        value={draft[field]}
        disabled={!editable && field !== 'note'}
        aria-invalid={problems[field] ? true : undefined}
        aria-describedby={problems[field] ? `invoice-${field}-problem` : undefined}
        onChange={(event) => set(field, event.target.value)}
        onBlur={() => void save(field)}
      />
      {problems[field] && (
        <p id={`invoice-${field}-problem`} className="text-[12px] text-warn">
          {problems[field]}
        </p>
      )}
    </div>
  )

  return (
    <form className="flex flex-col gap-3" onSubmit={(event) => event.preventDefault()} noValidate>
      {editable ? (
        <div className="flex min-w-0 flex-col gap-1">
          <Label htmlFor="invoice-supplier" className="text-[13px]">
            Supplier
          </Label>
          <Select value={invoice.supplier_id} onValueChange={(next) => void moveTo(next)}>
            <SelectTrigger id="invoice-supplier" aria-label="Supplier" className="w-full sm:w-64">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {suppliers
                .filter((one) => !one.archived_at || one.id === invoice.supplier_id)
                .map((one) => (
                  <SelectItem key={one.id} value={one.id}>
                    {one.name}
                  </SelectItem>
                ))}
            </SelectContent>
          </Select>
        </div>
      ) : (
        <p className="text-[13px]">
          <span className="text-muted-foreground">Supplier </span>
          <Link to={filamentLinks.supplier(invoice.supplier_id)} className="font-medium underline-offset-4 hover:underline">
            {suppliers.find((one) => one.id === invoice.supplier_id)?.name ?? 'Unknown supplier'}
          </Link>
        </p>
      )}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {input('number', 'Number')}
        {input('invoice_date', 'Date', { type: 'date' })}
        {input('currency', 'Currency')}
        {MONEY.map(({ field, label }) => input(field, label, { inputMode: 'decimal' }))}
        {input('note', 'Note', { className: 'col-span-2 sm:col-span-3' })}
      </div>
      {refused && (
        <p role="alert" className="text-[13px] text-warn">
          {refused.detail}
          {refused.existing && (
            <>
              {' '}
              <Link to={filamentLinks.invoice(refused.existing)} className="underline underline-offset-4">
                Open that invoice
              </Link>
            </>
          )}
        </p>
      )}
    </form>
  )
}
