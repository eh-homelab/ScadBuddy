import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../api/client'
import type { ShopMoney } from '../../components/filament/purchasing/useMoney'
import { DataTable, type Column } from '../../components/records/DataTable'
import { parseDecimal } from '../../lib/decimal'
import { inventoryAnchors, inventoryPaths } from '../../lib/inventoryLinks'
import { useAsync } from '../../lib/useAsync'
import { useHashTarget } from '../../lib/useHashTarget'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { inventoryApi, type Item, type Lot, type Move, type Offer, type RecostResult, type Unit, type UnitStatus } from './api'
import { PlaceSelect, PlacesError } from './PlaceSelect'
import { placeName, usePlaces, type Place } from './places'
import { dayText, dec, foreignCostText, majorToMinor, minorText, minorToMajor, packCost, pricedOffer, qtyText, STATUS_LABELS, unitLabel, valueText } from './format'

const failed = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : fallback)

/** A unit cost may keep four places past the currency's own, as `formatUnitCost` shows it. */
const UNIT_COST_PLACES = 4

const STATUSES = Object.keys(STATUS_LABELS) as UnitStatus[]

/** One form of the stock actions, under the buttons; `name` is its accessible name. */
function ActionForm({ name, onSubmit, children, error }: { name: string; onSubmit: () => void; children: ReactNode; error: string | null }) {
  return (
    <form
      aria-label={name}
      onSubmit={(event: FormEvent) => {
        event.preventDefault()
        onSubmit()
      }}
      className="flex flex-col gap-2 rounded-md border p-3 text-[13px]"
    >
      <div className="flex flex-wrap items-end gap-3">{children}</div>
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
    </form>
  )
}

function Field({ label, children, id }: { label: string; children: ReactNode; id: string }) {
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id}>{label}</Label>
      {children}
    </div>
  )
}

/** Runs a write once at a time, keeping its failure to show. */
function useWrite(onDone: () => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return {
    busy,
    error,
    fail: setError,
    async run(write: () => Promise<unknown>, fallback: string) {
      setBusy(true)
      setError(null)
      try {
        await write()
        onDone()
      } catch (cause) {
        setError(failed(cause, fallback))
      } finally {
        setBusy(false)
      }
    },
  }
}

type Open = 'receive' | 'use' | 'count' | null

/** Reads again in the background each time the page's `version` moves (after every change it makes). */
function useRefreshOn(version: number, refresh: () => void) {
  const seen = useRef(version)
  useEffect(() => {
    if (seen.current === version) return
    seen.current = version
    refresh()
  }, [version, refresh])
}

/** The anchor id's lot, or null. */
const lotAnchored = (hashId: string | null) => (hashId?.startsWith('lot-') ? hashId.slice('lot-'.length) : null)

/** A lot something has taken from or put back: its stock moved past the receive, or a listed move names it. */
const hasUses = (lot: Lot, moved: ReadonlySet<string>) => moved.has(lot.id) || dec(lot.on_hand) !== dec(lot.received_qty)

/**
 * #7067 — a lot item's stock: its lots in FIFO order, "Receive", "Use" and "Count", a lot's
 * cost and "Re-cost past uses" (on every lot with uses). A use past the stock on hand goes
 * below 0 and is flagged for review; stock never blocks (spec §6.6). `offers` is null until
 * they are read (or when the read failed: the page says so). `#lot-<id>` scrolls once the
 * lots have rendered.
 */
export function LotStock({ item, offers, money, moved, version, onChanged }: { item: Item; offers: Offer[] | null; money: ShopMoney; moved: ReadonlySet<string>; version: number; onChanged: () => void }) {
  const lots = useAsync(() => inventoryApi.lots(item.id), [item.id])
  useRefreshOn(version, lots.refresh)
  const named = lotAnchored(useHashTarget(!!lots.data))
  const places = usePlaces()
  const [placing, setPlacing] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<Open>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [costed, setCosted] = useState<string | null>(null)
  const [recost, setRecost] = useState<RecostResult | null>(null)
  const [recosting, setRecosting] = useState<string | null>(null)
  const priced = offers ? pricedOffer(offers, money.currency) : undefined

  async function recostLot(lotId: string) {
    if (recosting) return
    setRecosting(lotId)
    setError(null)
    try {
      setRecost(await inventoryApi.recostLot(lotId))
      onChanged()
    } catch (cause) {
      setError(failed(cause, 'The past uses could not be re-costed. Try again.'))
    } finally {
      setRecosting(null)
    }
  }

  function done() {
    setOpen(null)
    onChanged()
  }

  const columns: Column<Lot>[] = [
    { key: 'received', header: 'Received', cell: (lot) => dayText(lot.received_at) },
    { key: 'qty', header: 'Quantity', className: 'sb-num', cell: (lot) => qtyText(lot.received_qty, item.uom) },
    { key: 'on_hand', header: 'On hand', className: 'sb-num', cell: (lot) => <span className={Number(dec(lot.on_hand)) < 0 ? 'text-warn' : undefined}>{qtyText(lot.on_hand, item.uom)}</span> },
    {
      key: 'cost',
      header: 'Unit cost',
      className: 'sb-num',
      cell: (lot) =>
        editing === lot.id ? (
          <LotCostEditor
            lot={lot}
            money={money}
            onCancel={() => setEditing(null)}
            onSaved={() => {
              setEditing(null)
              setCosted(lot.id)
              setRecost(null)
              onChanged()
            }}
          />
        ) : (
          <span>
            {minorText(lot.unit_cost_minor, money, lot.cost_unknown)}
            {lot.cost_currency && lot.foreign_unit_cost_minor && (
              <span className="block text-[12px] text-muted-foreground">bought at {foreignCostText(lot.foreign_unit_cost_minor, lot.cost_currency, money)} a unit</span>
            )}
          </span>
        ),
    },
    {
      key: 'place',
      header: 'Place',
      className: 'break-words',
      cell: (lot) =>
        placing === lot.id ? (
          <LotPlaceEditor
            lot={lot}
            places={places.data ?? []}
            onCancel={() => setPlacing(null)}
            onSaved={() => {
              setPlacing(null)
              onChanged()
            }}
          />
        ) : (
          <span className="flex flex-wrap items-center gap-1">
            {placeName(places.data, lot.location_id)}
            <Button size="xs" variant="ghost" onClick={() => setPlacing(lot.id)}>
              Edit place
            </Button>
          </span>
        ),
    },
    { key: 'note', header: 'Note', className: 'break-words text-muted-foreground', cell: (lot) => lot.note || (lot.origin === 'adjustment' ? 'Opened by a count or a shortfall' : '') },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      cell: (lot) =>
        editing === lot.id ? null : (
          <span className="flex flex-wrap gap-1">
            <Button size="xs" variant="ghost" disabled={!money.currency} onClick={() => setEditing(lot.id)}>
              Edit cost
            </Button>
            {hasUses(lot, moved) && (
              <Button size="xs" variant={costed === lot.id ? 'outline' : 'ghost'} disabled={recosting !== null} aria-busy={recosting === lot.id} onClick={() => void recostLot(lot.id)}>
                {recosting === lot.id ? 'Re-costing…' : 'Re-cost past uses'}
              </Button>
            )}
          </span>
        ),
    },
  ]

  return (
    <section aria-label="Stock" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="mr-auto text-sm font-semibold tracking-tight">Stock</h2>
        {(['receive', 'use', 'count'] as const).map((action) => (
          <Button key={action} size="sm" variant={action === 'receive' ? 'default' : 'outline'} aria-expanded={open === action} onClick={() => setOpen(open === action ? null : action)}>
            {action === 'receive' ? 'Receive' : action === 'use' ? 'Use' : 'Count'}
          </Button>
        ))}
      </div>
      {offers && !priced && money.currency && <p className="text-[13px] text-muted-foreground">No supplier offer gives a price in {money.currency ?? 'the shop currency'}: enter what a lot cost when you receive it, or leave it empty and set the cost later.</p>}
      {open === 'receive' && <ReceiveLotForm item={item} offer={priced} money={money} places={places.data ?? []} onDone={done} />}
      {open === 'use' && <UseForm item={item} onDone={done} />}
      {open === 'count' && <CountForm item={item} onDone={done} />}
      {costed && !recost && <p className="text-[13px] text-muted-foreground">Past uses keep the cost they were taken at. "Re-cost past uses" gives them this lot's cost now, and changes the parts cost of the orders that used them.</p>}
      {recost && <RecostSummary result={recost} money={money} />}
      <PlacesError error={places.error} />
      {(error ?? lots.error) && (
        <p role="alert" className="text-[13px] text-warn">
          {error ?? `The lots could not be loaded: ${lots.error!.message}`}
        </p>
      )}
      {lots.data && (
        <DataTable
          label="Lots"
          columns={columns}
          rows={lots.data}
          rowKey={(lot) => lot.id}
          rowId={(lot) => inventoryAnchors.lot(lot.id)}
          rowTestId={(lot) => `lot-row-${lot.id}`}
          selected={named}
          empty="No lots yet. Receive one when it arrives."
        />
      )}
    </section>
  )
}

function LotCostEditor({ lot, money, onCancel, onSaved }: { lot: Lot; money: ShopMoney; onCancel: () => void; onSaved: () => void }) {
  const id = useId()
  const [cost, setCost] = useState(lot.cost_unknown ? '' : minorToMajor(dec(lot.unit_cost_minor), money.exponent))
  const write = useWrite(onSaved)

  function save() {
    if (!money.currency) return
    const minor = majorToMinor(cost, money.exponent, money.exponent + UNIT_COST_PLACES)
    if (minor === null) return write.fail(`Enter the cost of one unit, such as 0.25, with up to ${money.exponent + UNIT_COST_PLACES} decimals.`)
    void write.run(() => inventoryApi.patchLot(lot.id, { unit_cost_minor: minor }), 'The cost could not be saved. Try again.')
  }

  return (
    <span className="flex flex-wrap items-center justify-end gap-1">
      <label htmlFor={`${id}-cost`} className="sr-only">
        Unit cost
      </label>
      <Input id={`${id}-cost`} inputMode="decimal" className="h-7 w-24 text-right tabular-nums" value={cost} disabled={!money.currency} onChange={(e) => setCost(e.target.value)} />
      <Button size="xs" disabled={write.busy || !money.currency} onClick={save}>
        Save cost
      </Button>
      <Button size="xs" variant="ghost" onClick={onCancel}>
        Cancel
      </Button>
      {write.error && (
        <span role="alert" className="w-full text-left text-warn">
          {write.error}
        </span>
      )}
    </span>
  )
}

/** R-P2-38 — a lot's place (`PATCH /inventory/lots/{id}` `location_id`; null clears it). */
function LotPlaceEditor({ lot, places, onCancel, onSaved }: { lot: Lot; places: Place[]; onCancel: () => void; onSaved: () => void }) {
  const [place, setPlace] = useState<string | null>(lot.location_id)
  const write = useWrite(onSaved)
  return (
    <span className="flex flex-wrap items-center gap-1">
      <PlaceSelect label="Place of the lot" places={places} value={place} onChange={setPlace} />
      <Button size="xs" disabled={write.busy} onClick={() => void write.run(() => inventoryApi.patchLot(lot.id, { location_id: place }), 'The place could not be saved. Try again.')}>
        Save place
      </Button>
      <Button size="xs" variant="ghost" onClick={onCancel}>
        Cancel
      </Button>
      {write.error && (
        <span role="alert" className="w-full text-warn">
          {write.error}
        </span>
      )}
    </span>
  )
}

/** What "Re-cost past uses" changed: the moves, and each order item's parts cost before and after. */
function RecostSummary({ result, money }: { result: RecostResult; money: ShopMoney }) {
  return (
    <section aria-label="Re-cost result" className="flex flex-col gap-1 rounded-md border p-3 text-[13px]">
      <p>
        {result.moves} {result.moves === 1 ? 'move' : 'moves'} now at {minorText(result.unit_cost_minor, money, result.cost_unknown)} a unit.
        {result.orders.length === 0 && ' No order changed.'}
      </p>
      {result.orders.length > 0 && (
        <ul className="flex flex-col gap-1">
          {result.orders.map((row) => (
            <li key={row.order_item_id}>
              {row.order_id ? (
                <Link to={`/orders/${encodeURIComponent(row.order_id)}`} className="underline underline-offset-4">
                  Order {row.order_id.slice(0, 8)}
                </Link>
              ) : (
                <span>An order that is gone</span>
              )}
              : parts {valueText(row.before_minor, row.before_unknown, money)} before, {valueText(row.after_minor, row.after_unknown, money)} now
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function ReceiveLotForm({ item, offer, money, places, onDone }: { item: Item; offer: Offer | undefined; money: ShopMoney; places: Place[]; onDone: () => void }) {
  const id = useId()
  const [place, setPlace] = useState<string | null>(null)
  const [qty, setQty] = useState(offer ? offer.pack_size : '')
  const [cost, setCost] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const write = useWrite(onDone)
  // Until the person types a cost, it follows the quantity: the newest pack price × qty / pack size.
  const parsedQty = parseDecimal(qty)
  const prefill = offer && parsedQty && money.currency ? packCost(offer.latest_price!.price, parsedQty, offer.pack_size, money.exponent) : ''
  const shown = cost ?? prefill

  function submit() {
    if (parsedQty === null || Number(parsedQty) <= 0) return write.fail('Enter how many arrived, such as 100.')
    let total: string | null = null
    if (shown.trim() && money.currency) {
      total = majorToMinor(shown, money.exponent)
      if (total === null) return write.fail(`Enter the total cost, such as 12.50, with up to ${money.exponent} decimals, or leave it empty.`)
    }
    void write.run(() => inventoryApi.receiveLot(item.id, { qty: parsedQty, ...(total !== null ? { total_cost_minor: total } : {}), ...(place ? { location_id: place } : {}), note: note.trim() }), 'The lot could not be received. Try again.')
  }

  return (
    <ActionForm name="Receive a lot" onSubmit={submit} error={write.error}>
      <Field label="Quantity" id={`${id}-qty`}>
        <Input id={`${id}-qty`} inputMode="decimal" className="w-24" value={qty} onChange={(e) => setQty(e.target.value)} />
      </Field>
      <Field label={`Total cost (${money.currency ?? 'shop currency'})`} id={`${id}-cost`}>
        <Input id={`${id}-cost`} inputMode="decimal" className="w-28 text-right tabular-nums" value={shown} placeholder="unknown" disabled={!money.currency} onChange={(e) => setCost(e.target.value)} />
      </Field>
      <Field label="Place" id={`${id}-place`}>
        <PlaceSelect id={`${id}-place`} label="Place" places={places} value={place} onChange={setPlace} />
      </Field>
      <Field label="Note" id={`${id}-note`}>
        <Input id={`${id}-note`} className="w-56" value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
      <Button type="submit" size="sm" disabled={write.busy}>
        Receive lot
      </Button>
      {offer && (
        <p className="w-full text-muted-foreground">
          The cost starts at {offer.supplier_name}'s newest price for {offer.pack_size} {offer.pack_uom}. Change it to what you paid.
        </p>
      )}
    </ActionForm>
  )
}

function UseForm({ item, onDone }: { item: Item; onDone: () => void }) {
  const id = useId()
  const [qty, setQty] = useState('')
  const [reason, setReason] = useState('')
  const write = useWrite(onDone)

  function submit() {
    const parsed = parseDecimal(qty)
    if (parsed === null || Number(parsed) <= 0) return write.fail('Enter how many you used, such as 2.')
    void write.run(() => inventoryApi.use(item.id, { qty: parsed, reason: reason.trim() }), 'The use could not be saved. Try again.')
  }

  return (
    <ActionForm name="Use some" onSubmit={submit} error={write.error}>
      <Field label="Quantity" id={`${id}-qty`}>
        <Input id={`${id}-qty`} inputMode="decimal" className="w-24" value={qty} onChange={(e) => setQty(e.target.value)} />
      </Field>
      <Field label="Reason" id={`${id}-reason`}>
        <Input id={`${id}-reason`} className="w-56" value={reason} onChange={(e) => setReason(e.target.value)} />
      </Field>
      <Button type="submit" size="sm" disabled={write.busy}>
        Use
      </Button>
      <p className="w-full text-muted-foreground">Taken from the oldest lot first. A use past what is on hand goes below 0 and is marked for review.</p>
    </ActionForm>
  )
}

function CountForm({ item, onDone }: { item: Item; onDone: () => void }) {
  const id = useId()
  const [count, setCount] = useState('')
  const [note, setNote] = useState('')
  const write = useWrite(onDone)

  function submit() {
    const parsed = parseDecimal(count)
    if (parsed === null) return write.fail('Enter the count on the shelf, 0 or more.')
    if (!note.trim()) return write.fail('Say why the count changed.')
    void write.run(() => inventoryApi.count(item.id, { on_hand: parsed, note: note.trim() }), 'The count could not be saved. Try again.')
  }

  return (
    <ActionForm name="Count the item" onSubmit={submit} error={write.error}>
      <Field label="Count on hand" id={`${id}-count`}>
        <Input id={`${id}-count`} inputMode="decimal" className="w-24" value={count} onChange={(e) => setCount(e.target.value)} />
      </Field>
      <Field label="Why it changed" id={`${id}-note`}>
        <Input id={`${id}-note`} className="w-56" value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
      <Button type="submit" size="sm" disabled={write.busy}>
        Save count
      </Button>
    </ActionForm>
  )
}

/** #7067 — a unit item's stock: one row per unit, with its status; "Receive units". */
export function UnitStock({ item, money, version, onChanged }: { item: Item; money: ShopMoney; version: number; onChanged: () => void }) {
  const units = useAsync(() => inventoryApi.units(item.id), [item.id])
  useRefreshOn(version, units.refresh)
  const places = usePlaces()
  const [error, setError] = useState<string | null>(null)
  const [receiving, setReceiving] = useState(false)

  async function status(unit: Unit, next: UnitStatus) {
    setError(null)
    try {
      await inventoryApi.setUnitStatus(unit.id, { status: next })
      onChanged()
    } catch (cause) {
      setError(failed(cause, 'The status could not be changed. Try again.'))
    }
  }

  const rows = units.data ?? []
  const columns: Column<Unit>[] = [
    {
      key: 'unit',
      header: 'Unit',
      className: 'break-words',
      cell: (unit) => (
        <Link to={inventoryPaths.unit(unit.id)} className="font-medium underline-offset-4 hover:underline">
          {unitLabel(unit, rows.indexOf(unit))}
        </Link>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      cell: (unit) => (
        <Select value={unit.status} onValueChange={(next) => void status(unit, next as UnitStatus)}>
          <SelectTrigger size="sm" aria-label={`Status of ${unitLabel(unit, rows.indexOf(unit))}`} className="w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            {STATUSES.map((value) => (
              <SelectItem key={value} value={value}>
                {STATUS_LABELS[value]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ),
    },
    { key: 'place', header: 'Place', className: 'break-words', cell: (unit) => placeName(places.data, unit.location_id) },
    { key: 'cost', header: 'Cost', className: 'sb-num', cell: (unit) => minorText(unit.cost_minor, money, unit.cost_unknown) },
    { key: 'received', header: 'Received', cell: (unit) => dayText(unit.received_at) },
  ]

  return (
    <section aria-label="Stock" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="mr-auto text-sm font-semibold tracking-tight">Units</h2>
        <Button size="sm" aria-expanded={receiving} onClick={() => setReceiving((open) => !open)}>
          Receive
        </Button>
      </div>
      {receiving && (
        <ReceiveUnitsForm
          item={item}
          money={money}
          places={places.data ?? []}
          onDone={() => {
            setReceiving(false)
            onChanged()
          }}
        />
      )}
      <PlacesError error={places.error} />
      {(error ?? units.error) && (
        <p role="alert" className="text-[13px] text-warn">
          {error ?? `The units could not be loaded: ${units.error!.message}`}
        </p>
      )}
      {units.data && <DataTable label="Units" columns={columns} rows={rows} rowKey={(unit) => unit.id} empty="No units yet. Receive them when they arrive." />}
    </section>
  )
}

function ReceiveUnitsForm({ item, money, places, onDone }: { item: Item; money: ShopMoney; places: Place[]; onDone: () => void }) {
  const id = useId()
  const [place, setPlace] = useState<string | null>(null)
  const [count, setCount] = useState('1')
  const [cost, setCost] = useState('')
  const [serials, setSerials] = useState('')
  const write = useWrite(onDone)

  function submit() {
    const n = Number(count)
    if (!Number.isInteger(n) || n < 1 || n > 100) return write.fail('Enter how many arrived, 1 to 100.')
    let each: string | null = null
    if (cost.trim() && money.currency) {
      each = majorToMinor(cost, money.exponent)
      if (each === null) return write.fail(`Enter the cost of one unit, such as 19.99, with up to ${money.exponent} decimals, or leave it empty.`)
    }
    const listed = serials.split('\n').map((line) => line.trim())
    const named = listed.some((line) => line !== '')
    if (named && listed.length !== n) return write.fail(`Give one serial a line for each of the ${n} units, or none.`)
    void write.run(() => inventoryApi.receiveUnits(item.id, { count: n, ...(each !== null ? { cost_minor: each } : {}), ...(named ? { serials: listed } : {}), ...(place ? { location_id: place } : {}) }), 'The units could not be received. Try again.')
  }

  return (
    <ActionForm name="Receive units" onSubmit={submit} error={write.error}>
      <Field label="Count" id={`${id}-count`}>
        <Input id={`${id}-count`} inputMode="numeric" className="w-20" value={count} onChange={(e) => setCount(e.target.value)} />
      </Field>
      <Field label={`Cost of one (${money.currency ?? 'shop currency'})`} id={`${id}-cost`}>
        <Input id={`${id}-cost`} inputMode="decimal" className="w-28 text-right tabular-nums" placeholder="unknown" value={cost} disabled={!money.currency} onChange={(e) => setCost(e.target.value)} />
      </Field>
      <Field label="Serials, one a line" id={`${id}-serials`}>
        <textarea id={`${id}-serials`} rows={Math.min(Math.max(Number(count) || 1, 1), 6)} className="w-48 rounded-md border bg-transparent px-2 py-1" value={serials} onChange={(e) => setSerials(e.target.value)} />
      </Field>
      <Field label="Place" id={`${id}-place`}>
        <PlaceSelect id={`${id}-place`} label="Place" places={places} value={place} onChange={setPlace} />
      </Field>
      <Button type="submit" size="sm" disabled={write.busy}>
        Receive units
      </Button>
    </ActionForm>
  )
}

const MOVE_LABELS: Record<Move['kind'], string> = {
  receive: 'Received',
  use: 'Used',
  return: 'Returned',
  adjust: 'Counted',
  produce: 'Made',
  transfer: 'Moved',
  sell: 'Sold',
  retire: 'Retired',
  scrap: 'Scrapped',
}

/** The item's newest stock moves, each with the cost it was written at (a snapshot, D14). */
export function MovesTable({ moves, uom, money }: { moves: Move[]; uom: string; money: ShopMoney }) {
  const columns: Column<Move>[] = [
    { key: 'at', header: 'When', cell: (m) => dayText(m.at) },
    {
      key: 'kind',
      header: 'Move',
      cell: (m) => (
        <span className="flex flex-wrap items-center gap-1">
          {MOVE_LABELS[m.kind]}
          {m.needs_review && <Badge variant="destructive">Check</Badge>}
        </span>
      ),
    },
    { key: 'qty', header: 'Quantity', className: 'sb-num', cell: (m) => qtyText(m.qty, uom) },
    { key: 'cost', header: 'Unit cost', className: 'sb-num', cell: (m) => minorText(m.unit_cost_minor, money, m.cost_unknown) },
    { key: 'amount', header: 'Amount', className: 'sb-num', cell: (m) => minorText(m.amount_minor, money, m.cost_unknown) },
    {
      key: 'why',
      header: 'Why',
      className: 'break-words text-muted-foreground',
      cell: (m) =>
        m.order_id ? (
          <Link to={`/orders/${encodeURIComponent(m.order_id)}`} className="underline underline-offset-4">
            Order {m.order_id.slice(0, 8)}
          </Link>
        ) : (
          m.reason
        ),
    },
  ]
  return (
    <section aria-label="Stock moves" className="flex flex-col gap-2">
      <h2 className="text-sm font-semibold tracking-tight">Stock moves</h2>
      <p className="text-[12px] text-muted-foreground">Each move keeps the cost it was taken at. "Check" marks a use past what was on hand.</p>
      <DataTable label="Moves" columns={columns} rows={moves} rowKey={(m) => m.id} empty="No moves yet." />
    </section>
  )
}
