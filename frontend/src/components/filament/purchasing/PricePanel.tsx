import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import {
  listProducts,
  listSuppliers,
  listTypePrices,
  type Supplier,
  type SupplierProduct,
  type TypePricePoint,
} from '../../../api/filamentPurchasing'
import { formatCost } from '../../../lib/decimal'
import { Button } from '@/components/ui/button'
import { TD, TH } from '../formClasses'
import { AddPriceForm } from './AddPriceForm'
import { AddProductSheet } from './AddProductSheet'
import { Sparkline } from './Sparkline'
import { message } from './priceFormat'


const day = (iso: string) => new Date(`${iso}T00:00`).toLocaleDateString(undefined, { dateStyle: 'medium' })

interface Loaded {
  points: TypePricePoint[]
  products: SupplierProduct[]
  suppliers: Supplier[]
}

export interface PricePanelProps {
  typeId: string
  /** The shop's currency, `null` until it is read. */
  currency: string | null
  /** Its minor-unit digits. */
  exponent: number
  /** Puts a per-kg price in the type's cost field as an unsaved edit, and `from` names where it came from; the panel never saves it. */
  onUseAsCost: (pricePerKg: string, from: string) => void
}

/**
 * #6266 — what a saved type costs to buy: each supplier product of it with its price history
 * (newest first, per pack and per kg) and a line of per-kg over time. "Use as default cost"
 * hands the newest per-kg price to the page, which fills its cost field and waits for Save
 * (Decision 1). "Add price" is the Suppliers page's form.
 */
export function PricePanel({ typeId, currency, exponent, onUseAsCost }: PricePanelProps) {
  const [data, setData] = useState<Loaded | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState<string | null>(null)
  const [sheet, setSheet] = useState(false)

  /** Each load takes a number; only the newest one's answer is used. */
  const latest = useRef(0)
  const load = useCallback(async () => {
    const mine = ++latest.current
    try {
      const [points, suppliers] = await Promise.all([listTypePrices(typeId), listSuppliers({ includeArchived: true })])
      const lists = await Promise.all(suppliers.map((supplier) => listProducts(supplier.id, { includeArchived: true })))
      if (mine !== latest.current) return
      setData({ points, suppliers, products: lists.flat().filter((product) => product.type_id === typeId) })
      setError(null)
    } catch (cause) {
      if (mine === latest.current) setError(message(cause, 'The prices could not be loaded. Try again.'))
    }
  }, [typeId])

  useEffect(() => {
    void load()
  }, [load])

  const money = (amount: string, code: string) => formatCost(amount, code, code === currency ? exponent : 2)

  return (
    <section aria-label="Prices" className="mt-6 flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Prices</h2>
        <Button type="button" variant="outline" size="sm" onClick={() => setSheet(true)} disabled={!data}>
          Add supplier product
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {!data && !error && <p className="text-[13px] text-muted-foreground">Loading prices…</p>}
      {data && data.points.length === 0 && (
        <p className="text-[13px] text-muted-foreground">No prices yet. {data.products.length === 0 ? 'Add a supplier product, then its first price.' : 'Add a price to a product below.'}</p>
      )}
      {data?.products.map((product) => {
        const supplier = data.suppliers.find((one) => one.id === product.supplier_id)
        const archived = !!product.archived_at || !!supplier?.archived_at
        const history = data.points.filter((point) => point.product_id === product.id)
        const newest = history[0]
        const perKg = history.filter((point) => point.price_per_kg).map((point) => point.price_per_kg!).reverse()
        const name = `${supplier?.name ?? 'Supplier'} ${product.sku}`
        return (
          <article key={product.id} data-product={product.id} aria-label={name} className={`rounded-md border ${archived ? 'text-muted-foreground' : ''}`}>
            <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b bg-muted/40 px-3 py-2">
              <div className="min-w-0 flex-1">
                <p className="text-[13px] font-medium">
                  <Link to={`/filament/suppliers/${product.supplier_id}`} className="underline-offset-4 hover:underline">
                    {supplier?.name ?? 'Supplier'}
                  </Link>
                  <span className="ml-2 font-mono text-[12px] font-normal">{product.sku}</span>
                  {archived && <span className="ml-2 text-[12px] font-normal">Archived</span>}
                </p>
                <p className="text-[12px] text-muted-foreground">Pack of {product.pack_size}</p>
              </div>
              <Sparkline values={perKg} label={`Price per kg of ${name} over time`} />
              <div className="flex gap-1">
                {!archived && (
                  <Button type="button" variant="ghost" size="sm" aria-label={`Add price for ${name}`} onClick={() => setAdding(adding === product.id ? null : product.id)}>
                    Add price
                  </Button>
                )}
                {newest?.price_per_kg && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    aria-label={`Use as default cost from ${name}`}
                    disabled={currency !== null && newest.currency !== currency}
                    title={currency !== null && newest.currency !== currency ? `This price is in ${newest.currency}; the shop's currency is ${currency}` : undefined}
                    onClick={() => onUseAsCost(newest.price_per_kg!, name)}
                  >
                    Use as default cost
                  </Button>
                )}
              </div>
            </header>
            {adding === product.id && (
              <div className="border-b bg-muted/30 px-3 py-2">
                <AddPriceForm
                  productId={product.id}
                  currency={currency}
                  label={name}
                  onAdded={() => {
                    setAdding(null)
                    void load()
                  }}
                />
              </div>
            )}
            <div className="relative overflow-x-auto">
              <table aria-label={`Price history of ${name}`} className="w-full text-left text-[13px]">
                <thead className="text-muted-foreground">
                  <tr>
                    <th scope="col" className={TH}>Date</th>
                    <th scope="col" className={`${TH} text-right`}>Per pack</th>
                    <th scope="col" className={`${TH} text-right`}>Per kg</th>
                    <th scope="col" className={TH}>Source</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((point) => (
                    <tr key={point.id} className="border-t">
                      <td className={TD}>
                        <time dateTime={point.on_date}>{day(point.on_date)}</time>
                      </td>
                      <td className={`${TD} text-right tabular-nums`}>{money(point.price, point.currency)}</td>
                      <td className={`${TD} text-right tabular-nums`}>{point.price_per_kg ? money(point.price_per_kg, point.currency) : <span className="text-muted-foreground">No weight</span>}</td>
                      <td className={TD}>{point.source === 'invoice' ? 'Invoice' : 'Added by hand'}</td>
                    </tr>
                  ))}
                  {history.length === 0 && (
                    <tr className="border-t">
                      <td colSpan={4} className={`${TD} text-muted-foreground`}>
                        No prices for this product yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </article>
        )
      })}
      {sheet && data && (
        <AddProductSheet
          title="Add supplier product"
          fixed={{ typeId }}
          pick={{ label: 'Supplier', placeholder: 'Pick a supplier', refusal: 'Pick the supplier that sells it.', options: data.suppliers.filter((supplier) => !supplier.archived_at).map((supplier) => ({ id: supplier.id, label: supplier.name })) }}
          onClose={() => setSheet(false)}
          onAdded={() => {
            setSheet(false)
            void load()
          }}
        />
      )}
    </section>
  )
}
