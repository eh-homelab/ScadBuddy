import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { api } from '../api/client'
import type { Order } from '../api/types'
import { CustomerForm } from '../components/orders/CustomerForm'
import { OrderStatusBadge } from '../components/orders/OrderStatusBadge'
import { refusalText } from '../components/orders/refusal'
import { DataTable, type Column } from '../components/records/DataTable'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { timeAgo } from '../lib/format'
import { DEFAULT_ORDERS_QUERY } from '../lib/ordersQuery'
import { useAsync } from '../lib/useAsync'

/** `/customers/:id` (spec 2026-10-18 §3.1): the record, editable, and the customer's orders. */
export function CustomerPage() {
  const { id = '' } = useParams()
  const view = useAsync(() => api.customer(id), [id])
  const orders = useAsync(() => api.orders({ ...DEFAULT_ORDERS_QUERY, customer: id }, { limit: 100 }), [id], ['orders'])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [savedAt, setSavedAt] = useState<string | null>(null)
  const customer = view.data

  if (view.error) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-6">
        <h1 className="text-xl font-semibold tracking-tight">Customer</h1>
        <p role="alert" className="mt-2 text-[13px] text-warn">
          Could not load this customer: {view.error.message}
        </p>
      </div>
    )
  }
  if (!customer) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-6" role="status" aria-label="Loading customer">
        <h1 className="text-xl font-semibold tracking-tight">Customer</h1>
        <Skeleton className="mt-3 h-24 w-full" />
      </div>
    )
  }

  const columns: Column<Order>[] = [
    {
      key: 'number',
      header: 'Order',
      cell: (o) => (
        <Link to={`/orders/${o.id}`} className="font-medium text-foreground underline-offset-4 hover:underline">
          {o.number}
        </Link>
      ),
    },
    { key: 'status', header: 'Status', cell: (o) => <OrderStatusBadge status={o.status} onHold={o.on_hold} /> },
    { key: 'made', header: 'Made', className: 'whitespace-nowrap text-muted-foreground', cell: (o) => timeAgo(o.created_at) },
  ]

  return (
    <div className="h-full overflow-y-auto" data-testid="customer-page">
      <div className="mx-auto flex max-w-5xl flex-col gap-4 px-4 py-6">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold tracking-tight">{customer.display_name}</h1>
          {customer.archived_at ? (
            <span className="text-sm text-muted-foreground">Archived {timeAgo(customer.archived_at)}</span>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => {
                setBusy(true)
                api
                  .archiveCustomer(customer.id)
                  .then((next) => view.setData(next))
                  .catch((err: unknown) => setError(refusalText(err)))
                  .finally(() => setBusy(false))
              }}
            >
              Archive
            </Button>
          )}
          {savedAt && <span className="text-xs text-muted-foreground">Saved {savedAt}</span>}
        </div>
        <Card size="sm">
          <CardContent>
            <CustomerForm
              key={customer.updated_at}
              customer={customer}
              submitLabel="Save customer"
              busy={busy}
              error={error}
              onSubmit={(value) => {
                setBusy(true)
                setError(null)
                api
                  .updateCustomer(customer.id, value)
                  .then((next) => {
                    view.setData(next)
                    setSavedAt(new Date().toLocaleTimeString())
                  })
                  .catch((err: unknown) => setError(refusalText(err)))
                  .finally(() => setBusy(false))
              }}
            />
          </CardContent>
        </Card>
        <section className="flex flex-col gap-2" aria-label="Orders">
          <h2 className="text-sm font-medium">Orders</h2>
          {orders.loading ? (
            <Skeleton className="h-9 w-full" />
          ) : (
            <DataTable label="Customer orders" columns={columns} rows={orders.data?.items ?? []} rowKey={(o) => o.id} empty="No orders yet." />
          )}
        </section>
      </div>
    </div>
  )
}
