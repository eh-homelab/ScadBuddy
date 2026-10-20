import { useCallback, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { Loader2Icon } from 'lucide-react'
import { api } from '../api/client'
import type { Customer, Order, OrderStatus } from '../api/types'
import { OrderStatusBadge } from '../components/orders/OrderStatusBadge'
import { refusalText } from '../components/orders/refusal'
import { DataTable, type Column } from '../components/records/DataTable'
import { FilterBar } from '../components/records/FilterBar'
import { statusLabel } from '../components/prints/status'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { timeAgo } from '../lib/format'
import { DEFAULT_ORDERS_QUERY, isOrdersFiltered, parseOrdersQuery, toOrdersParams, type OrdersQuery } from '../lib/ordersQuery'
import { useAsync } from '../lib/useAsync'

const PAGE = 50
const STATUSES: OrderStatus[] = ['draft', 'quoted', 'accepted', 'in_production', 'ready', 'shipped', 'closed', 'cancelled']
const ANY = 'any'

/** `/orders` (spec 2026-10-18 §9): the list with its filters, newest first, paged by cursor. */
export function OrdersPage() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const query = parseOrdersQuery(params)
  const key = toOrdersParams(query).toString()
  const customers = useAsync(() => api.customers({ includeArchived: true, limit: 500 }), [])
  const list = useAsync(() => api.orders(query, { limit: PAGE }), [key], ['orders'])
  const [older, setOlder] = useState<{ key: string; items: Order[]; cursor: string | null } | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreFailed, setMoreFailed] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [newCustomer, setNewCustomer] = useState('')
  const [createError, setCreateError] = useState<string | null>(null)

  const onChange = useCallback(
    (next: OrdersQuery, options?: { typing?: boolean }) => setParams(toOrdersParams(next), { replace: options?.typing === true }),
    [setParams],
  )
  const held = older?.key === key ? older : null
  const base = list.data?.items ?? []
  const rows = [...base, ...(held?.items ?? []).filter((o) => !base.some((b) => b.id === o.id))]
  const cursor = held ? held.cursor : (list.data?.next_cursor ?? null)
  const names = new Map((customers.data ?? []).map((c) => [c.id, c.display_name]))

  function loadMore() {
    if (!cursor) return
    setLoadingMore(true)
    setMoreFailed(null)
    const before = held
    api.orders(query, { cursor, limit: PAGE }).then(
      (page) => {
        setOlder({ key, items: [...(before?.items ?? []), ...page.items], cursor: page.next_cursor })
        setLoadingMore(false)
      },
      () => {
        setLoadingMore(false)
        setMoreFailed(key)
      },
    )
  }

  async function create() {
    if (!newCustomer) {
      setCreateError('Pick a customer.')
      return
    }
    try {
      const order = await api.createOrder({ customer_id: newCustomer })
      setCreating(false)
      void navigate(`/orders/${order.id}`)
    } catch (err) {
      setCreateError(refusalText(err))
    }
  }

  const columns: Column<Order>[] = [
    {
      key: 'number',
      header: 'Order',
      cell: (order) => (
        <Link to={`/orders/${order.id}`} className="font-medium text-foreground underline-offset-4 hover:underline">
          {order.number}
        </Link>
      ),
    },
    { key: 'customer', header: 'Customer', className: 'break-words', cell: (order) => names.get(order.customer_id) ?? order.customer_id },
    { key: 'status', header: 'Status', cell: (order) => <OrderStatusBadge status={order.status} onHold={order.on_hold} /> },
    { key: 'created', header: 'Made', className: 'whitespace-nowrap text-muted-foreground', cell: (order) => timeAgo(order.created_at) },
    { key: 'due', header: 'Due', className: 'whitespace-nowrap text-muted-foreground', cell: (order) => (order.due_at ? new Date(order.due_at).toLocaleDateString() : '') },
  ]

  return (
    <div className="h-full overflow-y-auto" data-testid="orders-page">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-xl font-semibold tracking-tight">Orders</h1>
          <Button onClick={() => setCreating(true)}>New order</Button>
        </div>
        <FilterBar
          label="Order filters"
          search={{ value: query.q, onChange: (q) => onChange({ ...query, q }, { typing: true }), placeholder: 'Search orders' }}
          selects={[
            {
              key: 'customer',
              label: 'Customer',
              value: query.customer || ANY,
              options: [{ value: ANY, label: 'Any customer' }, ...(customers.data ?? []).map((c: Customer) => ({ value: c.id, label: c.display_name }))],
              onChange: (customer) => onChange({ ...query, customer: customer === ANY ? '' : customer }),
            },
          ]}
          chips={[
            {
              key: 'status',
              label: 'Status',
              chips: STATUSES.map((s) => ({ value: s, label: statusLabel(s) })),
              selected: query.status ? [query.status] : [],
              onChange: ([status]) => onChange({ ...query, status: status ?? '' }),
            },
          ]}
          filtered={isOrdersFiltered(query)}
          onClear={() => onChange(DEFAULT_ORDERS_QUERY)}
        >
          <div className="flex flex-wrap items-end gap-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor="orders-from" className="text-xs font-normal text-muted-foreground">
                Made from
              </Label>
              <Input id="orders-from" type="date" className="w-40" value={query.from.slice(0, 10)} onChange={(e) => onChange({ ...query, from: e.target.value ? `${e.target.value}T00:00:00Z` : '' })} />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="orders-to" className="text-xs font-normal text-muted-foreground">
                to
              </Label>
              <Input id="orders-to" type="date" className="w-40" value={query.to.slice(0, 10)} onChange={(e) => onChange({ ...query, to: e.target.value ? `${e.target.value}T23:59:59Z` : '' })} />
            </div>
          </div>
        </FilterBar>
        {list.error ? (
          <p role="alert" className="text-[13px] text-warn">
            Could not load orders: {list.error.message}
          </p>
        ) : list.loading ? (
          <div role="status" aria-label="Loading orders" className="flex flex-col gap-2">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : (
          <DataTable label="Orders" columns={columns} rows={rows} rowKey={(o) => o.id} rowTestId={(o) => `order-row-${o.id}`} empty="No orders match." />
        )}
        {moreFailed === key && (
          <p role="alert" className="text-[13px] text-warn">
            Could not load more. Try again.
          </p>
        )}
        {cursor && (
          <div className="flex justify-center">
            <Button variant="outline" onClick={loadMore} disabled={loadingMore}>
              {loadingMore && <Loader2Icon className="animate-spin" />}
              Load more
            </Button>
          </div>
        )}
      </div>
      <Dialog open={creating} onOpenChange={(open) => !open && setCreating(false)}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>New order</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-1">
            <Label htmlFor="new-order-customer">Customer</Label>
            <Select value={newCustomer} onValueChange={setNewCustomer}>
              <SelectTrigger id="new-order-customer" aria-label="Customer for the new order">
                <SelectValue placeholder="Pick a customer" />
              </SelectTrigger>
              <SelectContent position="popper">
                {(customers.data ?? [])
                  .filter((c) => !c.archived_at)
                  .map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.display_name}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              No customer yet? <Link to="/customers" className="text-primary underline-offset-4 hover:underline">Add one</Link> first.
            </p>
          </div>
          {createError && (
            <p role="alert" className="text-[13px] text-warn">
              {createError}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreating(false)}>
              Cancel
            </Button>
            <Button onClick={() => void create()}>Create order</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
