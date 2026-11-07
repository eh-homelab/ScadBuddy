import { useId, useState, type FormEvent, type ReactNode } from 'react'
import { api } from '../../api/client'
import type { InventoryPart } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { formatMoney, formatUnitCost, toMinor } from '../../lib/money'
import { useAsync } from '../../lib/useAsync'
import { refusalText } from './refusal'

/** A whole count typed in a field, or null. */
function count(text: string, min: number): number | null {
  const value = text.trim()
  if (!/^-?\d+$/.test(value)) return null
  const n = Number(value)
  return n >= min ? n : null
}

function Field({ label, children, id }: { label: string; id: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id}>{label}</Label>
      {children}
    </div>
  )
}

/** "Add part" (#5543): priced in the shop's currency; a count on the shelf is an `adjust` line. */
export function PartForm({ currency, exponent, onDone, onCancel }: { currency: string; exponent: number; onDone: () => void; onCancel: () => void }) {
  const id = useId()
  const [name, setName] = useState('')
  const [supplier, setSupplier] = useState('')
  const [link, setLink] = useState('')
  const [packSize, setPackSize] = useState('')
  const [packPrice, setPackPrice] = useState('')
  const [low, setLow] = useState('')
  const [onHand, setOnHand] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const size = count(packSize, 1)
    const price = toMinor(packPrice, exponent)
    const lowStock = low.trim() ? count(low, 0) : 0
    const shelf = onHand.trim() ? count(onHand, 0) : 0
    if (!name.trim()) return setError('Give the part a name.')
    if (size === null) return setError('The pack size is a whole number, at least 1.')
    if (price === null || price < 0) return setError(`The pack price is an amount in ${currency}, such as 5.99.`)
    if (lowStock === null || shelf === null) return setError('The low-stock level and the count on hand are whole numbers, 0 or more.')
    setBusy(true)
    setError(null)
    try {
      await api.createPart({ name: name.trim(), supplier: supplier.trim(), link: link.trim(), pack_size: size, pack_price_minor: price, low_stock: lowStock, on_hand: shelf })
      onDone()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} aria-label="Add part" className="grid gap-3 rounded-md border p-3 sm:grid-cols-2">
      <Field label="Name" id={`${id}-name`}>
        <Input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} placeholder="Silver hook" />
      </Field>
      <Field label="Supplier" id={`${id}-supplier`}>
        <Input id={`${id}-supplier`} value={supplier} onChange={(e) => setSupplier(e.target.value)} />
      </Field>
      <Field label="Link" id={`${id}-link`}>
        <Input id={`${id}-link`} value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://…" />
      </Field>
      <Field label="Pack size" id={`${id}-size`}>
        <Input id={`${id}-size`} inputMode="numeric" value={packSize} onChange={(e) => setPackSize(e.target.value)} />
      </Field>
      <Field label={`Pack price with tax (${currency})`} id={`${id}-price`}>
        <Input id={`${id}-price`} inputMode="decimal" value={packPrice} onChange={(e) => setPackPrice(e.target.value)} />
      </Field>
      <Field label="Low-stock level (0: never)" id={`${id}-low`}>
        <Input id={`${id}-low`} inputMode="numeric" value={low} onChange={(e) => setLow(e.target.value)} placeholder="0" />
      </Field>
      <Field label="On hand now" id={`${id}-hand`}>
        <Input id={`${id}-hand`} inputMode="numeric" value={onHand} onChange={(e) => setOnHand(e.target.value)} placeholder="0" />
      </Field>
      {error && (
        <p role="alert" className="text-[13px] text-warn sm:col-span-2">
          {error}
        </p>
      )}
      <div className="flex gap-2 sm:col-span-2">
        <Button type="submit" disabled={busy}>
          Save part
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

/** One part's "Add stock", "Adjust" (a note is required) or stock line. */
export function PartStockPanel({ part, mode, onChanged, onClose }: { part: InventoryPart; mode: 'stock' | 'adjust' | 'moves'; onChanged: () => void; onClose: () => void }) {
  const id = useId()
  const [packs, setPacks] = useState('1')
  const [price, setPrice] = useState('')
  const [onHand, setOnHand] = useState(String(part.on_hand))
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const moves = useAsync(() => (mode === 'moves' ? api.partMoves(part.id) : Promise.resolve([])), [mode, part.id, part.updated_at])
  const money = (minor: number) => formatMoney(minor, part.currency, part.minor_exponent)

  async function run(call: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await call()
      onChanged()
      onClose()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  function addStock(e: FormEvent) {
    e.preventDefault()
    const n = count(packs, 1)
    const each = price.trim() ? toMinor(price, part.minor_exponent) : undefined
    if (n === null) return setError('Packs is a whole number, at least 1.')
    if (each === null || (each !== undefined && each < 0)) return setError(`The pack price is an amount in ${part.currency}.`)
    void run(() => api.addPartStock(part.id, { packs: n, ...(each !== undefined ? { pack_price_minor: each } : {}), note: note.trim() }))
  }

  function adjust(e: FormEvent) {
    e.preventDefault()
    const n = count(onHand, -1_000_000)
    if (n === null) return setError('The count is a whole number.')
    if (!note.trim()) return setError('Say why the count changed.')
    void run(() => api.adjustPartStock(part.id, { on_hand: n, note: note.trim() }))
  }

  return (
    <section aria-label={`${part.name}: ${mode === 'stock' ? 'add stock' : mode === 'adjust' ? 'adjust' : 'stock line'}`} className="flex flex-col gap-3 rounded-md border p-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="font-medium">
          {part.name}: {mode === 'stock' ? 'add stock' : mode === 'adjust' ? 'adjust the count' : 'stock line'}
        </h2>
        <Button size="xs" variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
      {mode === 'stock' && (
        <form onSubmit={addStock} className="grid gap-3 sm:grid-cols-3">
          <Field label={`Packs of ${part.pack_size}`} id={`${id}-packs`}>
            <Input id={`${id}-packs`} inputMode="numeric" value={packs} onChange={(e) => setPacks(e.target.value)} />
          </Field>
          <Field label={`Price a pack (now ${money(part.pack_price_minor)})`} id={`${id}-price`}>
            <Input id={`${id}-price`} inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} />
          </Field>
          <Field label="Note" id={`${id}-note`}>
            <Input id={`${id}-note`} value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
          <p className="text-[13px] text-muted-foreground sm:col-span-3">The unit cost becomes the average of the stock on hand and the new packs. Orders already fulfilled keep their cost.</p>
          <div className="sm:col-span-3">
            <Button type="submit" disabled={busy}>
              Add stock
            </Button>
          </div>
        </form>
      )}
      {mode === 'adjust' && (
        <form onSubmit={adjust} className="grid gap-3 sm:grid-cols-2">
          <Field label="Count on the shelf" id={`${id}-count`}>
            <Input id={`${id}-count`} inputMode="numeric" value={onHand} onChange={(e) => setOnHand(e.target.value)} />
          </Field>
          <Field label="Why it changed" id={`${id}-why`}>
            <Input id={`${id}-why`} value={note} onChange={(e) => setNote(e.target.value)} aria-required="true" />
          </Field>
          <div className="sm:col-span-2">
            <Button type="submit" disabled={busy}>
              Save count
            </Button>
          </div>
        </form>
      )}
      {mode === 'moves' &&
        (moves.error ? (
          <p role="alert" className="text-[13px] text-warn">
            Could not load the stock line: {moves.error.message}
          </p>
        ) : (
          <ul className="flex flex-col gap-1 text-[13px]" aria-label="Stock line">
            {(moves.data ?? []).length === 0 && !moves.loading && <li className="text-muted-foreground">No stock changes yet.</li>}
            {(moves.data ?? []).map((m) => (
              <li key={m.id} className="flex flex-wrap gap-2">
                <span className="text-muted-foreground">{new Date(m.created_at).toLocaleString()}</span>
                <span className="font-medium">{m.kind}</span>
                <span className="sb-num">
                  {m.delta > 0 ? `+${m.delta}` : m.delta} → {m.on_hand_after}
                </span>
                <span className="sb-num text-muted-foreground">at {formatUnitCost(m.unit_cost_after_minor, part.currency, part.minor_exponent)}</span>
                {m.amount_minor != null && <span className="sb-num">{money(m.amount_minor)}</span>}
                {m.note && <span>{m.note}</span>}
              </li>
            ))}
          </ul>
        ))}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
