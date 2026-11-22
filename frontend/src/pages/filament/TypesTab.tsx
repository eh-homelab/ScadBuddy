import { PlusIcon, ShoppingCartIcon, SlidersHorizontalIcon } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import { fillTypesFromProfile, listCatalog, listSpools, listTypes, type CatalogRows, type Spool, type SpoolType, type TypeFill } from '../../api/filament'
import { addShopping, getPriceSheet, listSuppliers, type PricePoint } from '../../api/filamentPurchasing'
import { FilamentSearchBox } from '../../components/filament/FilamentSearchBox'
import { FILL_LABELS } from '../../components/filament/profileFill'
import { RgbaSwatch } from '../../components/filament/RgbaSwatch'
import { filamentLinks } from '../../lib/filamentLinks'
import { useMoney } from '../../components/filament/purchasing/useMoney'
import { FilamentsEditor } from '../../components/orders/FilamentsEditor'
import { addSku, matchesSearch, parseSearch, typeSearchDoc } from '../../lib/filamentSearch'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'

interface Loaded {
  types: SpoolType[]
  names: Map<string, string>
  spoolsOf: Map<string, number>
}

const stamp = (point: PricePoint) => `${point.on_date}|${point.created_at}`

const nameMap = (rows: { id: string; name: string }[]) => new Map(rows.map((row) => [row.id, row.name]))

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The types could not be loaded. Try again.')

/**
 * Each type's newest per-kg price from the suppliers' price sheets, and its SKUs for the
 * search (#6894). A sheet that cannot be read leaves its prices out and sets `failed`, so a
 * type with no price is not called unpriced.
 */
async function newestPrices(): Promise<{ prices: Map<string, PricePoint>; skus: Map<string, string[]>; failed: boolean }> {
  const prices = new Map<string, PricePoint>()
  const skus = new Map<string, string[]>()
  let suppliers
  try {
    suppliers = (await listSuppliers()).filter((supplier) => !supplier.archived_at)
  } catch {
    return { prices, skus, failed: true }
  }
  const sheets = await Promise.allSettled(suppliers.map((supplier) => getPriceSheet(supplier.id)))
  for (const sheet of sheets) {
    if (sheet.status === 'rejected') continue
    for (const row of sheet.value) {
      if (!row.product.archived_at) addSku(skus, row.product.type_id, row.product.sku)
      const point = row.newest
      if (!point?.price_per_kg || row.product.archived_at) continue
      const have = prices.get(row.product.type_id)
      if (!have || stamp(point) > stamp(have)) prices.set(row.product.type_id, point)
    }
  }
  return { prices, skus, failed: sheets.some((sheet) => sheet.status === 'rejected') }
}

/** The type's name as a person says it: brand, material, subtype. */
function product(type: SpoolType, names: Map<string, string>): string {
  return [type.brand_id, type.material_id, type.subtype_id].map((id) => (id ? names.get(id) : null)).filter(Boolean).join(' ')
}

/** #6262 — the Types tab: every type as a dense table, then the price book it prices from. */
export function TypesTab() {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  // In the URL, so a reload and a shared link keep it, as the Spools tab's filters are.
  const archived = params.get('archived') === 'true'
  const search = params.get('q') ?? ''
  const [data, setData] = useState<Loaded | null>(null)
  const [error, setError] = useState<string | null>(null)
  const money = useMoney()
  /** The newest per-kg price point per type, read beside the types so a slow supplier call never holds the table. */
  const [newest, setNewest] = useState<Map<string, PricePoint>>(new Map())
  const [pricesFailed, setPricesFailed] = useState(false)
  const [skus, setSkus] = useState<Map<string, string[]>>(new Map())
  const [notice, setNotice] = useState<string | null>(null)
  // Types whose add is in flight: a second click meanwhile would make a second pending item.
  const [adding, setAdding] = useState<ReadonlySet<string>>(new Set())
  /** #6714: the bulk fill's answer, per type; `null` before one ran. */
  const [filled, setFilled] = useState<TypeFill[] | null>(null)
  const [filling, setFilling] = useState(false)

  /** Each load takes a number; only the newest one's answer is used. */
  const latest = useRef(0)
  const load = useCallback(async () => {
    const mine = ++latest.current
    try {
      const [types, spools, materials, subtypes, brands] = await Promise.all([
        listTypes({ includeArchived: true }),
        listSpools({ includeArchived: false }),
        listCatalog('materials'),
        listCatalog('subtypes'),
        listCatalog('brands'),
      ])
      if (mine !== latest.current) return
      const count = new Map<string, number>()
      for (const spool of spools as Spool[]) if (spool.type_id) count.set(spool.type_id, (count.get(spool.type_id) ?? 0) + 1)
      const names = nameMap([...(materials as CatalogRows['materials'][]), ...subtypes, ...brands])
      setData({ types, names, spoolsOf: count })
      setError(null)
    } catch (cause) {
      if (mine === latest.current) setError(message(cause))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    let live = true
    void newestPrices().then(({ prices, skus: found, failed }) => {
      if (!live) return
      setNewest(prices)
      setSkus(found)
      setPricesFailed(failed)
    })
    return () => {
      live = false
    }
  }, [])

  async function addToShopping(type: SpoolType) {
    if (adding.has(type.id)) return
    setAdding((was) => new Set(was).add(type.id))
    setError(null)
    setNotice(null)
    try {
      await addShopping({ type_id: type.id, quantity_spools: 1 })
      setNotice(`Added ${type.code} to the shopping list.`)
    } catch (cause) {
      setError(message(cause))
    } finally {
      setAdding((was) => {
        const next = new Set(was)
        next.delete(type.id)
        return next
      })
    }
  }

  async function fillAll() {
    setFilling(true)
    setError(null)
    setNotice(null)
    try {
      setFilled(await fillTypesFromProfile())
      await load()
    } catch (cause) {
      setError(message(cause))
    } finally {
      setFilling(false)
    }
  }

  // Every type is loaded: a price book row linked to an archived type still names it.
  const tokens = parseSearch(search)
  const kept = data?.types.filter((type) => archived || !type.archived_at) ?? []
  const shown = tokens.length && data ? kept.filter((type) => matchesSearch(typeSearchDoc(type, data.names, skus.get(type.id)), tokens)) : kept
  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next, { replace: true })
  }
  return (
    <div className="flex flex-col gap-6">
      <section aria-label="Types" className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <FilamentSearchBox
              label="Search types"
              placeholder="Search: code, brand, colour, hex, SKU  /"
              value={search}
              onChange={(text) => setParam('q', text || null)}
              className="w-full sm:w-80"
            />
            <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
              <Checkbox checked={archived} aria-label="Show archived types" onCheckedChange={(on) => setParam('archived', on === true ? 'true' : null)} />
              Show archived
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" size="sm" disabled={filling} title="Fills each type's empty temperatures, density and K values from the slicer preset it prints with." onClick={() => void fillAll()}>
            <SlidersHorizontalIcon /> {filling ? 'Filling…' : 'Fill from slicer profiles'}
          </Button>
          <Button asChild size="sm">
            <Link to={filamentLinks.newType}>
              <PlusIcon /> New type
            </Link>
          </Button>
          </div>
        </div>
        {filled && <FillReport results={filled} back={location.search} onClose={() => setFilled(null)} />}
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
        {notice && (
          <p role="status" className="text-[13px] text-muted-foreground">
            {notice}
          </p>
        )}
        {!data && !error && <p className="text-[13px] text-muted-foreground">Loading types…</p>}
        {data && (
          <div className="relative overflow-x-auto rounded-md border">
            <table aria-label="Types" className="w-full text-left text-[13px]">
              <thead className="bg-muted/50 text-muted-foreground">
                <tr>
                  <th scope="col" className="w-7 px-1 py-1.5">
                    <span className="sr-only">Colour chip</span>
                  </th>
                  <th scope="col" className="px-2 py-1.5 font-medium">Code</th>
                  <th scope="col" className="px-2 py-1.5 font-medium">Product</th>
                  <th scope="col" className="px-2 py-1.5 font-medium">Colour</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Label g</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Price per kg</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Newest price</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Spools</th>
                  <th scope="col" className="px-2 py-1.5">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {shown.map((type) => (
                  <tr key={type.id} data-type={type.id} className="border-t">
                    <td className="px-1 py-1.5">
                      <RgbaSwatch rgba={type.rgba} size="md" />
                    </td>
                    <td className="px-2 py-1.5">
                      <Link to={filamentLinks.type(type.id)} state={{ back: location.search }} className="font-medium underline-offset-4 hover:underline">
                        {type.code}
                      </Link>
                      {type.archived_at && <span className="ml-2 text-[12px] text-muted-foreground">Archived</span>}
                    </td>
                    <td className="px-2 py-1.5">{product(type, data.names)}</td>
                    <td className="px-2 py-1.5">{type.colour_name}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{type.label_weight_g}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">
                      {type.cost_per_kg ?? <span className="text-muted-foreground">No price</span>}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums">
                      {(() => {
                        const point = newest.get(type.id)
                        if (!point?.price_per_kg) {
                          return pricesFailed ? (
                            <span className="text-muted-foreground" title="A supplier's prices could not be loaded">Not loaded</span>
                          ) : (
                            <span className="text-muted-foreground" aria-label="No supplier price">—</span>
                          )
                        }
                        return <span title={`From ${point.on_date}`}>{money.format(point.price_per_kg, point.currency)}/kg</span>
                      })()}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{data.spoolsOf.get(type.id) ?? 0}</td>
                    <td className="px-2 py-1.5 text-right">
                      <Button type="button" variant="ghost" size="sm" disabled={adding.has(type.id)} aria-label={`Buy ${type.code}: add it to the shopping list`} onClick={() => void addToShopping(type)}>
                        <ShoppingCartIcon /> Buy
                      </Button>
                    </td>
                  </tr>
                ))}
                {shown.length === 0 && (
                  <tr className="border-t">
                    <td colSpan={9} className="px-2 py-3 text-muted-foreground">
                      {!data?.types.length ? 'No types yet. Add one, or make one from a price book row below.' : tokens.length ? `No type matches “${search}”.` : 'No types shown. Tick Show archived types.'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section aria-label="Price book" className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Price book</h2>
        <FilamentsEditor types={data?.types} onTypesChanged={() => void load()} />
      </section>
    </div>
  )
}

/**
 * #6714 — what "Fill from slicer profiles" did: the totals, then each type it changed (the
 * fields and the preset) and each it could not read (why). Types with nothing to fill are
 * only counted.
 */
function FillReport({ results, back, onClose }: { results: TypeFill[]; back: string; onClose: () => void }) {
  const changed = results.filter((one) => one.written)
  const fields = changed.reduce((sum, one) => sum + Object.keys(one.filled).length, 0)
  const kValues = changed.reduce((sum, one) => sum + one.k_values.length, 0)
  const unread = results.filter((one) => one.reason && !one.written)
  const untouched = results.length - changed.length - unread.length
  const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`
  return (
    <section aria-label="Fill from slicer profiles" className="rounded-md border bg-muted/30 px-3 py-2 text-[13px]">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p role="status">
          Filled {plural(fields, 'field', 'fields')}
          {kValues > 0 && ` and ${plural(kValues, 'K value', 'K values')}`} on {plural(changed.length, 'type', 'types')}.
          {untouched > 0 && ` ${plural(untouched, 'type had', 'types had')} nothing empty to fill.`}
          {unread.length > 0 && ` ${plural(unread.length, 'type', 'types')} could not be read.`}
        </p>
        <Button type="button" variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      </div>
      {(changed.length > 0 || unread.length > 0) && (
        <ul className="mt-1 flex flex-col gap-1">
          {changed.map((one) => (
            <li key={one.type_id}>
              <Link to={filamentLinks.type(one.type_id)} state={{ back }} className="font-medium underline-offset-4 hover:underline">
                {one.code}
              </Link>
              <span className="text-muted-foreground">
                {': '}
                {[...Object.entries(one.filled).map(([field, value]) => `${FILL_LABELS[field as keyof typeof FILL_LABELS] ?? field} ${String(value)}`), ...(one.k_values.length > 0 ? [plural(one.k_values.length, 'K value', 'K values')] : [])].join(', ')}
                {one.source && `, from ${one.source.name}`}
              </span>
            </li>
          ))}
          {unread.map((one) => (
            <li key={one.type_id} className="text-muted-foreground">
              <Link to={filamentLinks.type(one.type_id)} state={{ back }} className="font-medium text-foreground underline-offset-4 hover:underline">
                {one.code}
              </Link>
              {`: ${one.reason ?? ''}`}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
