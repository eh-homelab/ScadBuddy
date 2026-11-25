import { useState } from 'react'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { ItemPart, ItemPartRow, OrderItem, OrderParts } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { inventoryPaths } from '../../lib/inventoryLinks'
import { usePartItems } from './partItems'
import { PartSelect } from './PartRules'
import { refusalText } from './refusal'

/**
 * #5543, job cost spec §11: what the order's open items need beyond the stock on hand,
 * one "short: 3 Silver hook" per part. Advice only: stock never blocks.
 */
export function PartsShort({ parts }: { parts: OrderParts }) {
  if (parts.short.length === 0) return null
  return (
    <p role="alert" data-testid="parts-short" className="rounded-md border border-warn/40 p-2 text-[13px] text-warn">
      {parts.short.map((n, at) => (
        <span key={n.item_id}>
          {at > 0 && '; '}
          short: {n.short}{' '}
          <Link to={inventoryPaths.item(n.item_id)} className="underline underline-offset-4">
            {n.name}
          </Link>
        </span>
      ))}
    </p>
  )
}

/**
 * An item's parts per unit (#5543): filled from its model's rule when it was added, changed
 * here by hand. They leave the inventory lots when the item is fulfilled, and job cost
 * counts them as its `parts_minor` (#7067), so a fulfilled item's are fixed (unfulfil it to
 * change them).
 */
export function ItemPartsEditor({ orderId, item, rows, editable, onChanged }: { orderId: string; item: Pick<OrderItem, 'id' | 'quantity' | 'status'>; rows: ItemPartRow[]; editable: boolean; onChanged: () => void }) {
  const partItems = usePartItems()
  const all = partItems.data ?? []
  const parts = all.filter((p) => !p.archived)
  const [draft, setDraft] = useState<ItemPart[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const name = (id: string) => all.find((p) => p.id === id)?.name ?? id

  async function save() {
    if (!draft) return
    setBusy(true)
    setError(null)
    try {
      await api.setItemParts(orderId, item.id, draft)
      setDraft(null)
      onChanged()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-label="Parts" className="flex flex-col gap-2 text-[13px]">
      <h3 className="text-sm font-medium">Parts</h3>
      {draft === null ? (
        <>
          {rows.length === 0 ? (
            <p className="text-muted-foreground">No parts.</p>
          ) : (
            <ul className="flex flex-col gap-1" aria-label="The item's parts">
              {rows.map((r) => (
                <li key={r.item_id}>
                  {r.per_unit} ×{' '}
                  <Link to={inventoryPaths.item(r.item_id)} className="underline-offset-4 hover:underline">
                    {name(r.item_id)}
                  </Link>{' '}
                  a unit
                  {r.used > 0 ? <span className="text-muted-foreground"> ({r.used} taken from stock)</span> : <span className="text-muted-foreground"> ({r.per_unit * item.quantity} for this item)</span>}
                </li>
              ))}
            </ul>
          )}
          {editable ? (
            <div>
              <Button size="xs" variant="outline" onClick={() => setDraft(rows.map(({ item_id, per_unit }) => ({ item_id, per_unit })))}>
                Edit parts
              </Button>
            </div>
          ) : (
            item.status === 'fulfilled' && <p className="text-muted-foreground">Its parts are out of stock: unfulfil it to change them.</p>
          )}
        </>
      ) : (
        <div className="flex flex-col gap-2">
          {draft.map((row, index) => (
            <div key={index} className="flex flex-wrap items-center gap-2">
              <Input
                aria-label={`Part ${index + 1} per unit`}
                inputMode="numeric"
                className="w-20"
                value={String(row.per_unit)}
                onChange={(e) => setDraft(draft.map((r, i) => (i === index ? { ...r, per_unit: Math.max(1, Number(e.target.value) || 1) } : r)))}
              />
              <PartSelect parts={all} value={row.item_id || null} onChange={(id) => id && setDraft(draft.map((r, i) => (i === index ? { ...r, item_id: id } : r)))} label={`Part ${index + 1}`} />
              <Button size="xs" variant="ghost" aria-label={`Remove part ${index + 1}`} onClick={() => setDraft(draft.filter((_, i) => i !== index))}>
                Remove
              </Button>
            </div>
          ))}
          <div className="flex flex-wrap gap-2">
            <Button
              size="xs"
              variant="outline"
              disabled={parts.every((p) => draft.some((r) => r.item_id === p.id))}
              onClick={() => {
                const free = parts.find((p) => !draft.some((r) => r.item_id === p.id))
                if (free) setDraft([...draft, { item_id: free.id, per_unit: 1 }])
              }}
            >
              Add part
            </Button>
            <Button size="xs" disabled={busy} onClick={() => void save()}>
              Save parts
            </Button>
            <Button size="xs" variant="ghost" onClick={() => setDraft(null)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
      {partItems.error && (
        <p role="alert" className="text-warn">
          The parts could not be loaded: {partItems.error.message}
        </p>
      )}
    </section>
  )
}
