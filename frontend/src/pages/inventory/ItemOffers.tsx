import { useId, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../api/client'
import { listSuppliers, type Supplier } from '../../api/filamentPurchasing'
import { SafeLink } from '../../components/filament/purchasing/SafeLink'
import type { ShopMoney } from '../../components/filament/purchasing/useMoney'
import { DataTable, type Column } from '../../components/records/DataTable'
import { formatCost, parseDecimal } from '../../lib/decimal'
import { filamentLinks } from '../../lib/filamentLinks'
import { inventoryAnchors } from '../../lib/inventoryLinks'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { inventoryApi, type Item, type Offer } from './api'
import { currencyExponent, dayText } from './format'

const failed = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : fallback)

/** A pack price in its own currency, with the digits it holds. */
function priceText(offer: Offer, money: ShopMoney): string {
  const point = offer.latest_price
  if (!point) return 'no price yet'
  const exponent = point.currency === money.currency ? money.exponent : currencyExponent(point.currency)
  const price = exponent === null ? `${point.price} ${point.currency}` : formatCost(point.price, point.currency, exponent)
  return `${price} on ${dayText(point.on_date)}`
}

/**
 * #7067 — an item's supplier offers: the supplier's SKU, pack and link (opened in a new tab),
 * the newest pack price and "Add price". Nothing here reaches Bambuddy (D16). `named` is the
 * offer an address names by anchor (`inventoryPaths.offer`).
 */
export function ItemOffers({ item, offers, money, named, onChanged }: { item: Item; offers: Offer[]; money: ShopMoney; named: string | null; onChanged: () => void }) {
  const [pricing, setPricing] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function archive(offer: Offer) {
    setError(null)
    try {
      await inventoryApi.archiveOffer(offer.id)
      onChanged()
    } catch (cause) {
      setError(failed(cause, 'The offer could not be archived. Try again.'))
    }
  }

  const columns: Column<Offer>[] = [
    {
      key: 'supplier',
      header: 'Supplier',
      className: 'break-words',
      cell: (offer) => (
        <Link to={filamentLinks.supplier(offer.supplier_id)} className="underline-offset-4 hover:underline">
          {offer.supplier_name}
        </Link>
      ),
    },
    { key: 'sku', header: 'SKU', className: 'break-all', cell: (offer) => offer.sku },
    { key: 'pack', header: 'Pack', className: 'sb-num', cell: (offer) => `${offer.pack_size} ${offer.pack_uom}` },
    { key: 'price', header: 'Newest price', className: 'sb-num', cell: (offer) => priceText(offer, money) },
    { key: 'url', header: 'Link', cell: (offer) => <SafeLink url={offer.url} /> },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      cell: (offer) => (
        <span className="flex flex-wrap gap-1">
          <Button size="xs" variant="outline" aria-label={`Add price for ${offer.sku}`} aria-expanded={pricing === offer.id} onClick={() => setPricing(pricing === offer.id ? null : offer.id)}>
            Add price
          </Button>
          <Button size="xs" variant="ghost" aria-label={`Archive offer ${offer.sku}`} onClick={() => void archive(offer)}>
            Archive
          </Button>
        </span>
      ),
    },
  ]
  const priced = offers.find((offer) => offer.id === pricing)

  return (
    <section aria-label="Offers" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-sm font-semibold tracking-tight">Offers</h2>
        <Button size="sm" variant="outline" className="ml-auto" aria-expanded={adding} onClick={() => setAdding((open) => !open)}>
          Add offer
        </Button>
      </div>
      {adding && (
        <AddOfferForm
          item={item}
          onDone={() => {
            setAdding(false)
            onChanged()
          }}
          onCancel={() => setAdding(false)}
        />
      )}
      <DataTable
        label="Offers"
        columns={columns}
        rows={offers}
        rowKey={(offer) => offer.id}
        rowId={(offer) => inventoryAnchors.offer(offer.id)}
        selected={named}
        empty="No offers yet. Add one to keep the supplier's link and pack price."
      />
      {priced && (
        <AddOfferPrice
          key={priced.id}
          offer={priced}
          money={money}
          onDone={() => {
            setPricing(null)
            onChanged()
          }}
        />
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}

/** A `manual` pack price for today, in the shop's currency. */
function AddOfferPrice({ offer, money, onDone }: { offer: Offer; money: ShopMoney; onDone: () => void }) {
  const id = useId()
  const [price, setPrice] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!money.currency) return
    const parsed = parseDecimal(price)
    if (parsed === null) return setError('Enter the price of one pack, such as 5.99.')
    setBusy(true)
    setError(null)
    try {
      await inventoryApi.addPrice(offer.id, { price: parsed })
      onDone()
    } catch (cause) {
      setError(failed(cause, 'The price could not be added. Try again.'))
      setBusy(false)
    }
  }

  return (
    <form aria-label={`Add price for ${offer.sku}`} onSubmit={(event) => void submit(event)} className="flex flex-wrap items-end gap-2 text-[13px]">
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-price`}>
          Pack price of {offer.pack_size} {offer.pack_uom} ({money.currency ?? 'shop currency'})
        </Label>
        <Input id={`${id}-price`} inputMode="decimal" className="h-8 w-28 text-right tabular-nums" value={price} disabled={!money.currency} onChange={(e) => setPrice(e.target.value)} />
      </div>
      <Button type="submit" size="sm" disabled={busy || !money.currency}>
        Add price
      </Button>
      {error && (
        <p role="alert" className="w-full text-warn">
          {error}
        </p>
      )}
    </form>
  )
}

function AddOfferForm({ item, onDone, onCancel }: { item: Item; onDone: () => void; onCancel: () => void }) {
  const id = useId()
  const suppliers = useAsync(() => listSuppliers(), [])
  const [supplier, setSupplier] = useState('')
  const [sku, setSku] = useState('')
  const [url, setUrl] = useState('')
  const [packSize, setPackSize] = useState('1')
  const [packUom, setPackUom] = useState(item.uom)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!supplier) return setError('Pick the supplier.')
    if (!sku.trim()) return setError("Enter the supplier's SKU.")
    const size = parseDecimal(packSize)
    if (size === null || Number(size) <= 0) return setError('The pack size is a number above 0, such as 100.')
    setBusy(true)
    setError(null)
    try {
      await inventoryApi.createOffer({ supplier_id: supplier, item_id: item.id, sku: sku.trim(), url: url.trim() || null, pack_size: size, pack_uom: packUom.trim() || 'each' })
      onDone()
    } catch (cause) {
      setError(failed(cause, 'The offer could not be added. Try again.'))
      setBusy(false)
    }
  }

  const choices: Supplier[] = suppliers.data ?? []
  return (
    <form aria-label="Add offer" onSubmit={(event) => void submit(event)} className="flex flex-col gap-3 rounded-md border p-3 text-[13px]">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-supplier`}>Supplier</Label>
          <Select value={supplier} onValueChange={setSupplier}>
            <SelectTrigger id={`${id}-supplier`} aria-label="Supplier" className="w-48">
              <SelectValue placeholder="Pick a supplier" />
            </SelectTrigger>
            <SelectContent position="popper">
              {choices.map((row) => (
                <SelectItem key={row.id} value={row.id}>
                  {row.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-sku`}>SKU</Label>
          <Input id={`${id}-sku`} value={sku} onChange={(e) => setSku(e.target.value)} className="w-36" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-url`}>Link</Label>
          <Input id={`${id}-url`} type="url" value={url} onChange={(e) => setUrl(e.target.value)} className="w-64" placeholder="https://" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-size`}>Pack size</Label>
          <Input id={`${id}-size`} inputMode="decimal" value={packSize} onChange={(e) => setPackSize(e.target.value)} className="w-20" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-uom`}>Pack unit</Label>
          <Input id={`${id}-uom`} value={packUom} onChange={(e) => setPackUom(e.target.value)} className="w-20" />
        </div>
      </div>
      {suppliers.error && (
        <p role="alert" className="text-warn">
          The suppliers could not be loaded: {suppliers.error.message}
        </p>
      )}
      <p className="text-muted-foreground">
        Suppliers are kept on{' '}
        <Link to={filamentLinks.suppliers} className="underline underline-offset-4">
          Filament → Suppliers
        </Link>
        .
      </p>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          Add offer
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
    </form>
  )
}
