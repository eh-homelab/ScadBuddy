import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../../api/client'
import { invoiceConflictId, patchInvoice, type InvoiceDetail, type InvoicePatch } from '../../../api/filamentPurchasing'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cmpDecimals, parseDecimal } from '../../../lib/decimal'

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
 * note. Each field saves on blur. The supplier is the invoice's own and cannot change. A
 * confirmed invoice is frozen but for its note (`editable` false).
 */
export function InvoiceHeaderForm({
  invoice,
  supplierName,
  editable,
  onSaved,
}: {
  invoice: InvoiceDetail
  supplierName: string
  editable: boolean
  onSaved: (invoice: InvoiceDetail) => void
}) {
  const [draft, setDraft] = useState(() => draftOf(invoice))
  const [problems, setProblems] = useState<Partial<Record<keyof Draft, string>>>({})
  const [refused, setRefused] = useState<{ detail: string; existing: string | null } | null>(null)

  // The server's answer (a money amount comes back with its own digits) replaces the text.
  const { number, invoice_date, currency, subtotal, shipping, tax, total, note } = invoice
  useEffect(() => {
    setDraft(draftOf({ number, invoice_date, currency, subtotal, shipping, tax, total, note } as InvoiceDetail))
  }, [number, invoice_date, currency, subtotal, shipping, tax, total, note])

  const set = (field: keyof Draft, value: string) => {
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
    try {
      onSaved(await patchInvoice(invoice.id, patch))
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
      <p className="text-[13px]">
        <span className="text-muted-foreground">Supplier </span>
        <Link to={`/filament/suppliers/${invoice.supplier_id}`} className="font-medium underline-offset-4 hover:underline">
          {supplierName || 'Unknown supplier'}
        </Link>
      </p>
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
              <Link to={`/filament/invoices/${refused.existing}`} className="underline underline-offset-4">
                Open that invoice
              </Link>
            </>
          )}
        </p>
      )}
    </form>
  )
}
