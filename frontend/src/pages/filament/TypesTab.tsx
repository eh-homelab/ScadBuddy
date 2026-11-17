import { PlusIcon } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import { listCatalog, listSpools, listTypes, type CatalogRows, type Spool, type SpoolType } from '../../api/filament'
import { RgbaSwatch } from '../../components/filament/RgbaSwatch'
import { FilamentsEditor } from '../../components/orders/FilamentsEditor'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'

interface Loaded {
  types: SpoolType[]
  names: Map<string, string>
  spoolsOf: Map<string, number>
}

const nameMap = (rows: { id: string; name: string }[]) => new Map(rows.map((row) => [row.id, row.name]))

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The types could not be loaded. Try again.')

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
  const [data, setData] = useState<Loaded | null>(null)
  const [error, setError] = useState<string | null>(null)

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

  // Every type is loaded: a price book row linked to an archived type still names it.
  const shown = data?.types.filter((type) => archived || !type.archived_at) ?? []
  return (
    <div className="flex flex-col gap-6">
      <section aria-label="Types" className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
            <Checkbox
              checked={archived}
              aria-label="Show archived types"
              onCheckedChange={(on) => {
                const next = new URLSearchParams(params)
                if (on === true) next.set('archived', 'true')
                else next.delete('archived')
                setParams(next, { replace: true })
              }}
            />
            Show archived
          </label>
          <Button asChild size="sm">
            <Link to="/filament/types/new">
              <PlusIcon /> New type
            </Link>
          </Button>
        </div>
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
        {!data && !error && <p className="text-[13px] text-muted-foreground">Loading types…</p>}
        {data && (
          <div className="overflow-x-auto rounded-md border">
            <table aria-label="Types" className="w-full text-left text-[13px]">
              <thead className="bg-muted/50 text-muted-foreground">
                <tr>
                  <th className="w-7 px-1 py-1.5">
                    <span className="sr-only">Colour chip</span>
                  </th>
                  <th scope="col" className="px-2 py-1.5 font-medium">Code</th>
                  <th scope="col" className="px-2 py-1.5 font-medium">Product</th>
                  <th scope="col" className="px-2 py-1.5 font-medium">Colour</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Label g</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Price per kg</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Spools</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((type) => (
                  <tr key={type.id} data-type={type.id} className="border-t">
                    <td className="px-1 py-1.5">
                      <RgbaSwatch rgba={type.rgba} size="md" />
                    </td>
                    <td className="px-2 py-1.5">
                      <Link to={`/filament/types/${type.id}`} state={{ back: location.search }} className="font-medium underline-offset-4 hover:underline">
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
                    <td className="px-2 py-1.5 text-right tabular-nums">{data.spoolsOf.get(type.id) ?? 0}</td>
                  </tr>
                ))}
                {shown.length === 0 && (
                  <tr className="border-t">
                    <td colSpan={7} className="px-2 py-3 text-muted-foreground">
                      No types yet. Add one, or make one from a price book row below.
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
