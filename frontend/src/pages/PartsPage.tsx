import { useState } from 'react'
import { api } from '../api/client'
import type { InventoryPart } from '../api/types'
import { OrdersTabs } from '../components/orders/OrdersTabs'
import { PartForm, PartStockPanel } from '../components/orders/PartForms'
import { PartRules } from '../components/orders/PartRules'
import { DataTable, type Column } from '../components/records/DataTable'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { formatMoney, formatUnitCost } from '../lib/money'
import { useAsync } from '../lib/useAsync'

type Open = { id: string; mode: 'stock' | 'adjust' | 'moves' }

/**
 * `/orders/parts` (#5543, job cost spec §11): the parts an item uses beside its filament
 * (hooks, rings, magnets), their stock and cost, and the rule a model gives its items.
 * Stock never blocks: it can go below zero, and a part at or below its low-stock level
 * says "Low".
 */
export function PartsPage() {
  const list = useAsync(() => api.parts(), [])
  const book = useAsync(() => api.priceBook(), [])
  const [adding, setAdding] = useState(false)
  const [open, setOpen] = useState<Open | null>(null)
  const parts = list.data ?? []
  const selected = open ? parts.find((p) => p.id === open.id) : undefined

  function changed() {
    list.reload()
  }

  const columns: Column<InventoryPart>[] = [
    {
      key: 'name',
      header: 'Part',
      className: 'break-words',
      cell: (p) => (
        <span className="flex flex-wrap items-center gap-1">
          <span className="font-medium">{p.name}</span>
          {p.low && <Badge variant="destructive">Low</Badge>}
        </span>
      ),
    },
    {
      key: 'supplier',
      header: 'Supplier',
      className: 'break-words text-muted-foreground',
      cell: (p) =>
        p.link ? (
          <a href={p.link} target="_blank" rel="noopener noreferrer" className="underline-offset-4 hover:underline">
            {p.supplier || p.link}
          </a>
        ) : (
          p.supplier
        ),
    },
    { key: 'pack', header: 'Pack', className: 'sb-num', cell: (p) => `${p.pack_size} for ${formatMoney(p.pack_price_minor, p.currency, p.minor_exponent)}` },
    { key: 'unit', header: 'Unit cost', className: 'sb-num', cell: (p) => formatUnitCost(p.unit_cost_minor, p.currency, p.minor_exponent) },
    { key: 'on_hand', header: 'On hand', className: 'sb-num', cell: (p) => <span className={p.on_hand < 0 || p.low ? 'text-warn' : undefined}>{p.on_hand}</span> },
    { key: 'low_stock', header: 'Low at', className: 'sb-num text-muted-foreground', cell: (p) => (p.low_stock > 0 ? p.low_stock : '—') },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      cell: (p) => (
        <span className="flex flex-wrap gap-1">
          <Button size="xs" variant="outline" onClick={() => setOpen({ id: p.id, mode: 'stock' })} aria-label={`Add stock of ${p.name}`}>
            Add stock
          </Button>
          <Button size="xs" variant="ghost" onClick={() => setOpen({ id: p.id, mode: 'adjust' })} aria-label={`Adjust ${p.name}`}>
            Adjust
          </Button>
          <Button size="xs" variant="ghost" onClick={() => setOpen({ id: p.id, mode: 'moves' })} aria-label={`Stock line of ${p.name}`}>
            Stock line
          </Button>
        </span>
      ),
    },
  ]

  return (
    <div className="h-full overflow-y-auto" data-testid="parts-page">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-xl font-semibold tracking-tight">Parts</h1>
            <OrdersTabs />
          </div>
          <Button onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
            Add part
          </Button>
        </div>
        {adding && book.data && (
          <PartForm
            currency={book.data.currency}
            exponent={book.data.minor_exponent}
            onDone={() => {
              setAdding(false)
              changed()
            }}
            onCancel={() => setAdding(false)}
          />
        )}
        {list.error ? (
          <p role="alert" className="text-[13px] text-warn">
            Could not load the parts: {list.error.message}
          </p>
        ) : list.loading && !list.data ? (
          <div role="status" aria-label="Loading parts" className="flex flex-col gap-2">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : (
          <DataTable label="Parts" columns={columns} rows={parts} rowKey={(p) => p.id} rowTestId={(p) => `part-row-${p.id}`} empty="No parts yet. Add the hooks, rings or magnets your items use." />
        )}
        {open && selected && <PartStockPanel key={`${open.id}-${open.mode}`} part={selected} mode={open.mode} onChanged={changed} onClose={() => setOpen(null)} />}
        <PartRules parts={parts} />
      </div>
    </div>
  )
}
