import { Link, useSearchParams } from 'react-router'
import { api } from '../api/client'
import type { ProfitRow } from '../api/types'
import { marginTint } from '../components/orders/OrderCosts'
import { OrdersTabs } from '../components/orders/OrdersTabs'
import { lastDay, periodOf, rowLink } from '../components/orders/profit'
import { DataTable, type Column } from '../components/records/DataTable'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { formatDueDate } from '../lib/format'
import { formatMoney } from '../lib/money'
import { useAsync } from '../lib/useAsync'

function productName(row: ProfitRow) {
  switch (row.group) {
    case 'model':
      return <span className="font-medium">{row.key}</span>
    case 'sku':
      return (
        <span>
          <span className="text-muted-foreground">SKU</span> <span className="font-medium">{row.key}</span>
        </span>
      )
    case 'custom':
      return <span>Custom, no SKU</span>
    case 'order':
      return <span className="text-muted-foreground">Shipping, charges and order costs</span>
    default:
      return <span className="font-semibold">Total {row.currency}</span>
  }
}

function money(row: ProfitRow, minor: number | null | undefined) {
  return minor == null ? '—' : formatMoney(minor, row.currency, row.minor_exponent)
}

/**
 * `/orders/profit` (job cost spec §6.4, #5329): the orders accepted in a period, by model,
 * SKU and custom item, per currency. A row links to the orders behind it. Loss rows are
 * red and thin ones amber, as on the orders list.
 */
export function ProfitPage() {
  const [params, setParams] = useSearchParams()
  const from = params.get('from') ?? ''
  const to = params.get('to') ?? ''
  const report = useAsync(() => api.profitReport(periodOf(from, to)), [from, to], ['orders'])

  function setPeriod(next: { from?: string; to?: string }) {
    const out = new URLSearchParams()
    const value = { from, to, ...next }
    if (value.from) out.set('from', value.from)
    if (value.to) out.set('to', value.to)
    setParams(out, { replace: true })
  }

  const data = report.data
  const columns: Column<ProfitRow>[] = [
    { key: 'product', header: 'Product', className: 'break-words', cell: productName },
    {
      key: 'orders',
      header: 'Orders',
      className: 'text-right tabular-nums',
      headerClassName: 'text-right',
      cell: (row) =>
        data ? (
          <Link to={rowLink(data, row)} className="underline-offset-4 hover:underline" aria-label={`The ${row.orders} orders of this row`}>
            {row.orders}
          </Link>
        ) : (
          row.orders
        ),
    },
    { key: 'units', header: 'Units', className: 'text-right tabular-nums', headerClassName: 'text-right', cell: (row) => (row.group === 'order' ? '' : row.units) },
    {
      key: 'charged',
      header: 'Charged',
      className: 'whitespace-nowrap text-right tabular-nums',
      headerClassName: 'text-right',
      cell: (row) => (
        <span title={row.uncharged_items ? `${row.uncharged_items} items not yet charged are in no sum.` : undefined}>
          {money(row, row.revenue_minor)}
          {row.uncharged_items > 0 && <span className="ml-1 text-warn">(+{row.uncharged_items} not charged)</span>}
        </span>
      ),
    },
    {
      key: 'cost',
      header: 'Real cost',
      className: 'whitespace-nowrap text-right tabular-nums',
      headerClassName: 'text-right',
      cell: (row) => (
        <span title={row.unknown.length ? `Some costs are unknown (${row.unknown.join(', ')}).` : row.estimated ? 'A part of the cost is estimated.' : undefined}>
          {row.estimated ? '≈ ' : ''}
          {money(row, row.cost_minor)}
        </span>
      ),
    },
    { key: 'profit', header: 'Profit', className: 'whitespace-nowrap text-right tabular-nums', headerClassName: 'text-right', cell: (row) => money(row, row.margin_minor) },
    {
      key: 'margin',
      header: 'Margin',
      className: 'whitespace-nowrap text-right tabular-nums',
      headerClassName: 'text-right',
      cell: (row) => (
        <span data-testid="profit-margin">
          {row.margin_percent == null ? '—' : `${row.margin_percent}%`}
          {row.margin_state === 'unknown' && <span className="ml-1 text-warn">(incomplete)</span>}
        </span>
      ),
    },
    {
      key: 'waste',
      header: 'Waste',
      className: 'whitespace-nowrap text-right tabular-nums',
      headerClassName: 'text-right',
      cell: (row) => (
        <span title={`Test and failed prints: ${money(row, row.waste_minor)}`}>{row.waste_percent == null ? '—' : `${row.waste_percent}%`}</span>
      ),
    },
  ]

  return (
    <div className="h-full overflow-y-auto" data-testid="profit-page">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-xl font-semibold tracking-tight">Profit by model</h1>
          <OrdersTabs />
        </div>
        <p className="text-[13px] text-muted-foreground">
          The orders accepted in the period, cancelled ones too. Each currency has its own rows. Waste is the share of the real cost spent on test and failed prints.
        </p>
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="profit-from" className="text-xs font-normal text-muted-foreground">
              Accepted from
            </Label>
            <Input id="profit-from" type="date" className="w-40" value={from || (data ? data.period_from.slice(0, 10) : '')} onChange={(e) => setPeriod({ from: e.target.value })} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="profit-to" className="text-xs font-normal text-muted-foreground">
              to
            </Label>
            <Input id="profit-to" type="date" className="w-40" value={to || (data ? lastDay(data.period_to) : '')} onChange={(e) => setPeriod({ to: e.target.value })} />
          </div>
        </div>
        {report.error ? (
          <p role="alert" className="text-[13px] text-warn">
            Could not load the report: {report.error.message}
          </p>
        ) : report.loading || !data ? (
          <div role="status" aria-label="Loading the report" className="flex flex-col gap-2">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : (
          <>
            {!data.complete && data.read_from && (
              <p role="alert" className="text-[13px] text-warn">
                The report reads at most {data.max_orders} orders. It covers only the orders accepted from {formatDueDate(data.read_from)}. Make the period shorter to see all of it.
              </p>
            )}
            <DataTable
              label="Profit by model"
              columns={columns}
              rows={data.rows}
              rowKey={(row) => `${row.currency}:${row.group}:${row.key ?? ''}`}
              rowTestId={(row) => `profit-row-${row.currency}-${row.group}${row.key ? `-${row.key}` : ''}`}
              rowClassName={(row) => `${marginTint(row.margin_state)} ${row.group === 'total' ? 'font-semibold' : ''}`.trim()}
              empty="No orders were accepted in this period."
            />
          </>
        )}
      </div>
    </div>
  )
}
