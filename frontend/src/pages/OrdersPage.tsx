import { useCallback, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { ArrowDownIcon, ArrowUpDownIcon, ArrowUpIcon, Loader2Icon } from 'lucide-react'
import { api } from '../api/client'
import type { Customer, InventoryPart, OrderRow, OrderStatus } from '../api/types'
import { CustomerPicker } from '../components/orders/CustomerPicker'
import { NEW_CUSTOMER, useCustomerPicker } from '../components/orders/useCustomerPicker'
import { NEXT_SORT, sortByMargin, type MarginSort } from '../components/orders/marginSort'
import { marginTint } from '../components/orders/OrderCosts'
import { OrderStatusBadge } from '../components/orders/OrderStatusBadge'
import { OrdersTabs } from '../components/orders/OrdersTabs'
import { refusalText } from '../components/orders/refusal'
import { DataTable, type Column } from '../components/records/DataTable'
import { FilterBar } from '../components/records/FilterBar'
import { statusLabel } from '../components/prints/status'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { dueAt, formatDueDate, timeAgo } from '../lib/format'
import { formatMoney } from '../lib/money'
import { DEFAULT_ORDERS_QUERY, isOrdersFiltered, isReportFiltered, LOSS_OR_THIN, parseOrdersQuery, REPORT_KEYS, toOrdersParams, type OrdersQuery } from '../lib/ordersQuery'
import { useAsync } from '../lib/useAsync'

const PAGE = 50
const STATUSES: OrderStatus[] = ['draft', 'quoted', 'accepted', 'in_production', 'ready', 'shipped', 'closed', 'cancelled']
const ANY = 'any'
/** #5282: where an order was made. A storefront order is a cart until it is submitted. */
const SOURCES = [
  { value: 'back_office', label: 'Back office' },
  { value: 'storefront', label: 'Storefront' },
]
const UNSUBMITTED = 'unsubmitted'
/** #6133 (spec 2026-11-13 §11): a storefront customer's own models and things to buy. */
const MODEL_FILTERS = [
  { value: 'own_model', label: 'Own model' },
  { value: 'purchase_pending', label: 'Purchase pending' },
  { value: 'import_stuck', label: 'Import stuck' },
] as const

/**
 * #5327 (job cost spec §6.2): the margin as a percent, the money in the tooltip; `≈` when a
 * part is estimated, "—" with no charged figure. A job charged nothing shows its money.
 */
function MarginCell({ order }: { order: OrderRow }) {
  if (order.margin_minor == null) return <span className="text-muted-foreground">—</span>
  const money = formatMoney(order.margin_minor, order.currency, order.minor_exponent)
  const approx = order.margin_estimated ? '≈ ' : ''
  const incomplete = order.margin_state === 'unknown' ? ' Some costs are unknown.' : ''
  return (
    <span title={`Margin ${approx}${money}.${incomplete}`} data-testid="order-margin">
      {approx}
      {order.margin_percent != null ? `${order.margin_percent}%` : money}
      {order.margin_state === 'unknown' && <span className="ml-1 text-warn">(incomplete)</span>}
    </span>
  )
}

/** #5329 — the profit report row's filters, in words. */
function reportFilterText(query: OrdersQuery): string {
  const parts: string[] = []
  if (query.product === 'custom') parts.push('custom items with no SKU')
  else if (query.product.startsWith('sku:')) parts.push(`SKU ${query.product.slice(4)}`)
  else if (query.product.startsWith('model:')) parts.push(`model ${query.product.slice(6)}`)
  else if (query.product) parts.push(query.product)
  // The `total` row has no product: its link lists every order in the currency (#5546).
  else parts.push('every order')
  if (query.accepted_from || query.accepted_to) {
    // The report's bounds are the browser's local midnights (#5546): its days are local ones.
    const from = query.accepted_from ? new Date(query.accepted_from).toLocaleDateString() : 'any time'
    const to = query.accepted_to ? new Date(Date.parse(query.accepted_to) - 1).toLocaleDateString() : 'now'
    parts.push(`accepted ${from} to ${to}`)
  }
  if (query.currency) parts.push(`in ${query.currency}`)
  return parts.join(', ')
}

/** #5543, job cost spec §11: the parts at or below their low-stock level, linked to the Parts page. */
function LowStock({ parts }: { parts: InventoryPart[] }) {
  if (parts.length === 0) return null
  return (
    <p role="status" data-testid="low-stock" className="rounded-md border border-warn/40 p-2 text-[13px] text-warn">
      Low stock: {parts.map((p) => `${p.name} (${p.on_hand})`).join(', ')}.{' '}
      <Link to="/orders/parts" className="underline underline-offset-4">
        Parts
      </Link>
    </p>
  )
}

/** `/orders` (spec 2026-10-18 §9): the list with its filters, newest first, paged by cursor. */
export function OrdersPage() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const query = parseOrdersQuery(params)
  const key = toOrdersParams(query).toString()
  const customers = useAsync(() => api.customers({ includeArchived: true, limit: 500 }), [])
  const list = useAsync(() => api.orders(query, { limit: PAGE }), [key], ['orders'])
  // #5543: a fulfilment takes parts out of stock, so the low parts are read again on an order's change.
  const lowParts = useAsync(() => api.parts(true), [], ['orders'])
  const [older, setOlder] = useState<{ key: string; items: OrderRow[]; cursor: string | null } | null>(null)
  const [sort, setSort] = useState<MarginSort>('none')
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreFailed, setMoreFailed] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const picker = useCustomerPicker(customers.data)
  const [newNotes, setNewNotes] = useState('')
  const [newDue, setNewDue] = useState('')
  const [createBusy, setCreateBusy] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  // A customer was made in the dialog now open.
  const madeCustomer = useRef(false)

  const onChange = useCallback(
    (next: OrdersQuery, options?: { typing?: boolean }) => setParams(toOrdersParams(next), { replace: options?.typing === true }),
    [setParams],
  )
  const held = older?.key === key ? older : null
  const base = list.data?.items ?? []
  // Sorting by margin orders the rows loaded so far; "Load more" adds the next page.
  const rows = sortByMargin([...base, ...(held?.items ?? []).filter((o) => !base.some((b) => b.id === o.id))], sort)
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

  function closeCreate() {
    setCreating(false)
    // The list's names and Customer filter read `customers`: take in a customer made in
    // the dialog once it closes, never under it (the picker already offers the new one).
    if (madeCustomer.current) {
      madeCustomer.current = false
      customers.reload()
    }
  }

  async function create() {
    setCreateBusy(true)
    setCreateError(null)
    try {
      // A customer made here stays picked when the order then fails, so a retry reuses it.
      const making = picker.value === NEW_CUSTOMER
      const customerId = await picker.resolve()
      if (making) madeCustomer.current = true
      const order = await api.createOrder({ customer_id: customerId, notes: newNotes, due_at: newDue ? dueAt(newDue) : null })
      closeCreate()
      void navigate(`/orders/${order.id}`)
    } catch (err) {
      setCreateError(refusalText(err))
    } finally {
      setCreateBusy(false)
    }
  }

  const SortIcon = sort === 'ascending' ? ArrowUpIcon : sort === 'descending' ? ArrowDownIcon : ArrowUpDownIcon
  const columns: Column<OrderRow>[] = [
    {
      key: 'number',
      header: 'Order',
      cell: (order) => (
        <Link to={`/orders/${order.id}`} className="font-medium text-foreground underline-offset-4 hover:underline">
          {order.number ?? 'Cart'}
        </Link>
      ),
    },
    {
      key: 'customer',
      header: 'Customer',
      className: 'break-words',
      // #5282: a storefront cart has no customer until it is submitted.
      cell: (order) => (order.customer_id ? (names.get(order.customer_id) ?? order.customer_id) : <span className="text-muted-foreground">Visitor</span>),
    },
    { key: 'status', header: 'Status', cell: (order) => <OrderStatusBadge status={order.status} onHold={order.on_hold} /> },
    { key: 'created', header: 'Made', className: 'whitespace-nowrap text-muted-foreground', cell: (order) => timeAgo(order.created_at) },
    { key: 'due', header: 'Due', className: 'whitespace-nowrap text-muted-foreground', cell: (order) => (order.due_at ? formatDueDate(order.due_at) : '') },
    {
      key: 'margin',
      header: (
        <button type="button" className="inline-flex items-center gap-1 hover:text-foreground" onClick={() => setSort(NEXT_SORT[sort])}>
          Margin
          <SortIcon className="size-3" aria-hidden />
        </button>
      ),
      sort,
      className: 'whitespace-nowrap text-right tabular-nums',
      headerClassName: 'text-right',
      cell: (order) => <MarginCell order={order} />,
    },
  ]

  return (
    <div className="h-full overflow-y-auto" data-testid="orders-page">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-xl font-semibold tracking-tight">Orders</h1>
            <OrdersTabs />
          </div>
          <Button
            onClick={() => {
              // Each New order starts empty: a cancelled attempt's choices do not carry over.
              picker.reset()
              setNewDue('')
              setNewNotes('')
              setCreateError(null)
              setCreating(true)
            }}
          >
            New order
          </Button>
        </div>
        <LowStock parts={lowParts.data ?? []} />
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
            {
              key: 'margin',
              label: 'Margin',
              chips: [{ value: LOSS_OR_THIN, label: 'Loss or thin' }],
              selected: query.margin ? [query.margin] : [],
              onChange: ([margin]) => onChange({ ...query, margin: margin ?? '' }),
            },
            {
              key: 'source',
              label: 'Source',
              chips: SOURCES,
              selected: query.source ? [query.source] : [],
              onChange: ([source]) => onChange({ ...query, source: source ?? '' }),
            },
            {
              key: 'unsubmitted',
              label: 'Carts',
              chips: [{ value: UNSUBMITTED, label: 'Unsubmitted carts' }],
              selected: query.unsubmitted ? [UNSUBMITTED] : [],
              onChange: ([on]) => onChange({ ...query, unsubmitted: on === UNSUBMITTED }),
            },
            {
              key: 'models',
              label: "Customer's models",
              multiple: true,
              chips: [...MODEL_FILTERS],
              selected: MODEL_FILTERS.filter((f) => query[f.value]).map((f) => f.value),
              onChange: (on) => onChange({ ...query, ...Object.fromEntries(MODEL_FILTERS.map((f) => [f.value, on.includes(f.value)])) }),
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
        {isReportFiltered(query) && (
          <div role="status" data-testid="orders-report-filter" className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-[13px]">
            <span>The orders behind a profit report row: {reportFilterText(query)}.</span>
            <Button variant="link" size="sm" className="h-auto p-0" onClick={() => onChange({ ...query, ...Object.fromEntries(REPORT_KEYS.map((k) => [k, ''])) })}>
              Show every order
            </Button>
          </div>
        )}
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
          <DataTable
            label="Orders"
            columns={columns}
            rows={rows}
            rowKey={(o) => o.id}
            rowTestId={(o) => `order-row-${o.id}`}
            rowClassName={(o) => marginTint(o.margin_state)}
            // #5515: a margin filter reads a bounded number of orders a request, so a page
            // can be empty while older orders are still to search.
            empty={cursor ? 'No matches yet. Load more to search older orders.' : 'No orders match.'}
          />
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
      <Dialog open={creating} onOpenChange={(open) => !open && !createBusy && closeCreate()}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>New order</DialogTitle>
          </DialogHeader>
          <CustomerPicker picker={picker} id="new-order-customer" label="Customer for the new order" loading={customers.loading} disabled={createBusy} />
          <div className="flex flex-col gap-1">
            <Label htmlFor="new-order-due">Due</Label>
            <Input id="new-order-due" type="date" className="w-40" disabled={createBusy} value={newDue} onChange={(e) => setNewDue(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="new-order-notes">Notes</Label>
            <Textarea id="new-order-notes" rows={2} disabled={createBusy} value={newNotes} onChange={(e) => setNewNotes(e.target.value)} />
          </div>
          {createError && (
            <p role="alert" className="text-[13px] text-warn">
              {createError}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" disabled={createBusy} onClick={closeCreate}>
              Cancel
            </Button>
            <Button disabled={createBusy} onClick={() => void create()}>
              {createBusy && <Loader2Icon className="animate-spin" />}
              Create order
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
