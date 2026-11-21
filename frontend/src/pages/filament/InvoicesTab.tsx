import { PaperclipIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import { listInvoices, listSuppliers, type Invoice, type Supplier } from '../../api/filamentPurchasing'
import { TD, TH } from '../../components/filament/formClasses'
import { InvoiceUpload } from '../../components/filament/purchasing/InvoiceUpload'
import { readQuery, remember, rememberedQuery, remembering, setRemembering, writeQuery, type InvoicesQuery, type InvoiceStatusFilter } from '../../components/filament/purchasing/invoicesQuery'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Badge } from '@/components/ui/badge'
import { formatCost } from '../../lib/decimal'

const ALL = 'all'
const STATUSES: { value: InvoiceStatusFilter; label: string }[] = [
  { value: 'all', label: 'All statuses' },
  { value: 'draft', label: 'Drafts' },
  { value: 'confirmed', label: 'Confirmed' },
]

/**
 * #6266 — the Invoices tab: the file drop to start a draft, then every invoice, newest
 * first, filtered by status and supplier. The view is in the address; "Remember my view"
 * makes a visit with no query open the last one.
 */
export function InvoicesTab() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const [restore, setRestore] = useState(() => (params.size === 0 ? rememberedQuery() : null))
  const [keeping, setKeeping] = useState(remembering)
  const [suppliers, setSuppliers] = useState<Supplier[]>([])
  const [rows, setRows] = useState<Invoice[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const query: InvoicesQuery = params.size === 0 && restore ? restore : readQuery(params)

  useEffect(() => {
    if (restore === null) return
    setRestore(null)
    if (params.size === 0) setParams(writeQuery(restore), { replace: true })
  }, [restore, params, setParams])

  function change(next: InvoicesQuery) {
    setParams(writeQuery(next))
    remember(next)
  }

  useEffect(() => {
    let live = true
    listSuppliers({ includeArchived: true }).then(
      (found) => live && setSuppliers(found),
      () => undefined,
    )
    return () => {
      live = false
    }
  }, [])

  const { status, supplier } = query
  useEffect(() => {
    let live = true
    setRows(null)
    listInvoices({ status: status === 'all' ? undefined : status, supplierId: supplier }).then(
      (found) => {
        if (!live) return
        setRows(found)
        setError(null)
      },
      (cause) => live && setError(cause instanceof ApiError ? cause.detail : 'The invoices could not be loaded. Try again.'),
    )
    return () => {
      live = false
    }
  }, [status, supplier])

  const names = useMemo(() => new Map(suppliers.map((one) => [one.id, one.name])), [suppliers])

  return (
    <div className="flex flex-col gap-4">
      <InvoiceUpload onCreated={(id) => navigate(`/filament/invoices/${id}`)} />
      <div className="flex flex-wrap items-center gap-3 text-[13px]">
        <Select value={status} onValueChange={(next) => change({ ...query, status: next as InvoiceStatusFilter })}>
          <SelectTrigger size="sm" aria-label="Status" className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STATUSES.map((one) => (
              <SelectItem key={one.value} value={one.value}>
                {one.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={supplier ?? ALL} onValueChange={(next) => change({ status, ...(next === ALL ? {} : { supplier: next }) })}>
          <SelectTrigger size="sm" aria-label="Filter by supplier" className="w-48">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All suppliers</SelectItem>
            {suppliers.map((one) => (
              <SelectItem key={one.id} value={one.id}>
                {one.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <label className="ml-auto flex items-center gap-2 text-[12px] text-muted-foreground">
          <Switch
            size="sm"
            checked={keeping}
            onCheckedChange={(on) => {
              setKeeping(on)
              setRemembering(on, query)
            }}
          />
          Remember my view
        </label>
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {!rows && !error && <p className="text-[13px] text-muted-foreground">Loading invoices…</p>}
      {rows && rows.length === 0 && <p className="text-[13px] text-muted-foreground">{status === 'all' && !supplier ? 'No invoices yet. Drop an invoice file above to start one.' : 'No invoice matches this view.'}</p>}
      {rows && rows.length > 0 && (
        <div className="relative overflow-x-auto rounded-md border">
          <table aria-label="Invoices" className="w-full text-left text-[13px]">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th scope="col" className={TH}>Date</th>
                <th scope="col" className={TH}>Supplier</th>
                <th scope="col" className={TH}>Number</th>
                <th scope="col" className={TH}>Status</th>
                <th scope="col" className={`${TH} text-right`}>Total</th>
                <th scope="col" className={`${TH} text-right`}>Spools</th>
                <th scope="col" className={TH}>
                  <span className="sr-only">File</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((invoice) => (
                <tr key={invoice.id} data-invoice={invoice.id} className="border-t">
                  <td className={`${TD} whitespace-nowrap tabular-nums`}>{invoice.invoice_date}</td>
                  <td className={TD}>{names.get(invoice.supplier_id) ?? ''}</td>
                  <td className={TD}>
                    <Link to={`/filament/invoices/${invoice.id}`} className="font-medium underline-offset-4 hover:underline">
                      {invoice.number ?? 'No number'}
                    </Link>
                  </td>
                  <td className={TD}>
                    <Badge variant={invoice.status === 'draft' ? 'outline' : 'secondary'}>{invoice.status === 'draft' ? 'Draft' : 'Confirmed'}</Badge>
                  </td>
                  <td className={`${TD} text-right tabular-nums`}>{formatCost(invoice.total, invoice.currency, invoice.minor_exponent)}</td>
                  <td className={`${TD} text-right tabular-nums`} aria-label="Spools made or filled">
                    {invoice.spool_count}
                  </td>
                  <td className={`${TD} text-right`}>{invoice.file && <PaperclipIcon aria-label="Has a file" role="img" className="inline size-3.5 text-muted-foreground" />}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
