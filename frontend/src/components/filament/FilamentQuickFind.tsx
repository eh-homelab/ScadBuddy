import { SearchIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { ApiError } from '../../api/client'
import { getInventory, listCatalog, listTypes, type SpoolType } from '../../api/filament'
import type { FilamentInventorySpool } from '../../api/types'
import { trayPlace } from '../../lib/filaments'
import { spoolColour, spoolName } from '../../lib/filamentColour'
import { readPickerFilters, writePickerFilters, type PickerFilters } from '../../lib/filamentPickerFilters'
import { loadTypeSkus, matchesSearch, namesNumber, parseSearch, spoolSearchDoc, traysBySpool, typeSearchDoc } from '../../lib/filamentSearch'
import { ClearSwatch, Swatch } from '../print/Swatch'
import { spoolPath } from './spoolsQuery'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'

/** The rows each group shows; typing more narrows the rest in. */
export const QUICK_FIND_LIMIT = 50

interface Loaded {
  spools: FilamentInventorySpool[]
  types: SpoolType[]
  names: Map<string, string>
  trays: Map<string, string>
  skus: Map<string, string[]>
}

type State = { kind: 'loading' } | { kind: 'failed'; message: string } | { kind: 'open'; data: Loaded }

/** Ctrl+K, or ⌘K on a Mac. */
const isQuickFindKey = (event: KeyboardEvent) =>
  event.key.toLowerCase() === 'k' && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey

const onMac = () => typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.userAgent)

/** Loaded spools first, then the fullest, as the colour picker orders them. */
function order(a: FilamentInventorySpool, b: FilamentInventorySpool): number {
  if (!!a.loaded !== !!b.loaded) return a.loaded ? -1 : 1
  return (b.spool.grams_left ?? 0) - (a.spool.grams_left ?? 0)
}

async function load(): Promise<Loaded> {
  const [view, types, materials, subtypes, brands] = await Promise.all([
    getInventory(),
    listTypes(),
    listCatalog('materials'),
    listCatalog('subtypes'),
    listCatalog('brands'),
  ])
  // Without the suppliers' products, everything but the SKU is still searched.
  const skus = await loadTypeSkus().catch(() => new Map<string, string[]>())
  return {
    spools: (view.spools ?? []).filter((row) => !row.spool.archived_at).sort(order),
    types: types.filter((type) => !type.archived_at).sort((a, b) => a.code.localeCompare(b.code)),
    names: new Map([...materials, ...subtypes, ...brands].map((row) => [row.id, row.name])),
    trays: traysBySpool(view),
    skus,
  }
}

/**
 * #6894 — one type-to-search list over the spools and the types together, on every Filament
 * tab: a button, or Ctrl+K (⌘K). Each row looks as in the colour picker (`FilamentColourPicker`):
 * the swatch, the name, then the number, the place and what is left; it shares the picker's
 * remembered material and "Loaded in a printer" filters (a type counts as loaded when one of
 * its spools is). The search is the tabs' (`lib/filamentSearch.ts`); Enter opens the spool or
 * the type. The lists are read each time it opens.
 */
export function FilamentQuickFind() {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (!isQuickFindKey(event) || event.defaultPrevented) return
      event.preventDefault()
      setOpen(true)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  return (
    <>
      <Button type="button" size="sm" variant="outline" onClick={() => setOpen(true)} aria-keyshortcuts="Control+K Meta+K">
        <SearchIcon /> Find filament
        <kbd className="ml-1 rounded border bg-muted px-1 font-sans text-[11px] text-muted-foreground">{onMac() ? '⌘K' : 'Ctrl K'}</kbd>
      </Button>
      <CommandDialog open={open} onOpenChange={setOpen} title="Find filament" description="Search the spools and the types; Enter opens one." className="top-[12vh] sm:max-w-xl">
        {open && <QuickFindList onDone={() => setOpen(false)} />}
      </CommandDialog>
    </>
  )
}

function QuickFindList({ onDone }: { onDone: () => void }) {
  const navigate = useNavigate()
  const location = useLocation()
  const [state, setState] = useState<State>({ kind: 'loading' })
  const [text, setText] = useState('')
  const [filters, setFilters] = useState<PickerFilters>(readPickerFilters)
  const setFilter = (next: Partial<PickerFilters>) => {
    const merged = { ...filters, ...next }
    setFilters(merged)
    writePickerFilters(merged)
  }

  useEffect(() => {
    let live = true
    load().then(
      (data) => live && setState({ kind: 'open', data }),
      (cause: unknown) => live && setState({ kind: 'failed', message: cause instanceof ApiError ? cause.detail : 'The spools and types could not be read.' }),
    )
    return () => {
      live = false
    }
  }, [])

  const data = state.kind === 'open' ? state.data : null
  const tokens = useMemo(() => parseSearch(text), [text])
  const view = useMemo(() => {
    if (!data) return null
    const typeCodes = new Map(data.types.map((type) => [type.id, type.code]))
    const materialOfType = (type: SpoolType) => data.names.get(type.material_id) ?? null
    const materials = [
      ...new Set([...data.spools.map((row) => row.spool.names?.material ?? null), ...data.types.map(materialOfType)].filter((m): m is string => m !== null)),
    ].sort((a, b) => a.localeCompare(b))
    const material = filters.material !== null && materials.includes(filters.material) ? filters.material : null
    const anyLoaded = data.spools.some((row) => row.loaded)
    const loadedOnly = anyLoaded && filters.loadedOnly
    const loadedTypes = new Set(data.spools.flatMap((row) => (row.loaded && row.spool.type_id ? [row.spool.type_id] : [])))

    const spools = data.spools.filter((row) => {
      const { spool } = row
      if (loadedOnly && !row.loaded) return false
      if (material !== null && spool.names?.material !== material) return false
      const doc = spoolSearchDoc(spool, {
        typeCode: spool.type_id ? typeCodes.get(spool.type_id) : null,
        skus: spool.type_id ? data.skus.get(spool.type_id) : undefined,
        tray: data.trays.get(spool.id),
      })
      return matchesSearch(doc, tokens)
    })
    // "#87" or "87": that spool first.
    const exact = spools.findIndex((row) => namesNumber(tokens, row.spool.number))
    if (exact > 0) spools.unshift(...spools.splice(exact, 1))
    const types = data.types.filter(
      (type) =>
        (!loadedOnly || loadedTypes.has(type.id)) &&
        (material === null || materialOfType(type) === material) &&
        matchesSearch(typeSearchDoc(type, data.names, data.skus.get(type.id)), tokens),
    )
    return { materials, material, anyLoaded, loadedOnly, spools, types }
  }, [data, tokens, filters])

  const go = (path: string) => {
    onDone()
    void navigate(path, { state: { back: location.search } })
  }

  return (
    <Command shouldFilter={false} label="Find filament" className="rounded-none! p-0">
      <CommandInput placeholder="Search: #87, brand, colour, hex, SKU, place" value={text} onValueChange={setText} />
      {view && (view.materials.length > 1 || view.anyLoaded) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b px-2 py-1.5">
          {view.materials.length > 1 && (
            <div role="group" aria-label="Material" className="flex flex-wrap gap-1">
              {[null, ...view.materials].map((choice) => (
                <Button
                  key={choice ?? ''}
                  type="button"
                  size="xs"
                  variant={choice === view.material ? 'secondary' : 'ghost'}
                  aria-pressed={choice === view.material}
                  onClick={() => setFilter({ material: choice })}
                >
                  {choice ?? 'All'}
                </Button>
              ))}
            </div>
          )}
          {view.anyLoaded && (
            <Label className="ml-auto cursor-pointer text-xs font-normal text-muted-foreground">
              <Checkbox checked={view.loadedOnly} onCheckedChange={(checked) => setFilter({ loadedOnly: checked === true })} />
              Loaded in a printer
            </Label>
          )}
        </div>
      )}
      {state.kind === 'loading' && (
        <div role="status" aria-label="Reading the spools and types" className="flex flex-col gap-1.5 p-2">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      )}
      {state.kind === 'failed' && (
        <p role="alert" className="px-3 py-4 text-[13px] text-destructive">
          {state.message}
        </p>
      )}
      {view && data && (
        <CommandList label="Spools and types" className="max-h-[min(26rem,60vh)]">
          <CommandEmpty>{text.trim() ? `No spool or type matches “${text.trim()}”.` : 'No spool or type matches these filters.'}</CommandEmpty>
          {view.spools.length > 0 && (
            <CommandGroup heading={more('Spools', view.spools.length)}>
              {view.spools.slice(0, QUICK_FIND_LIMIT).map((row) => {
                const { spool } = row
                const colour = spoolColour({ rgba: spool.effective?.rgba ?? spool.rgba })
                const where = row.loaded ? trayPlace(row.loaded, { printer: true }) : (spool.names?.location ?? null)
                const grams = spool.grams_left == null ? null : `${Math.round(spool.grams_left)} g`
                const detail = [`#${spool.number}`, colour === null ? 'clear' : colour.translucent ? 'translucent' : null, where, grams].filter(Boolean).join(' · ')
                return (
                  <CommandItem key={spool.id} value={`spool:${spool.id}`} onSelect={() => go(spoolPath(spool))} className="text-xs">
                    {colour === null ? <ClearSwatch size="sm" /> : <Swatch colour={colour.hex} size="sm" />}
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate">{spoolName(row)}</span>
                      <span className="sb-num truncate text-muted-foreground">{detail}</span>
                    </span>
                  </CommandItem>
                )
              })}
            </CommandGroup>
          )}
          {view.types.length > 0 && (
            <CommandGroup heading={more('Types', view.types.length)}>
              {view.types.slice(0, QUICK_FIND_LIMIT).map((type) => {
                const colour = spoolColour(type)
                const product = [type.brand_id, type.material_id, type.subtype_id].map((id) => (id ? data.names.get(id) : null)).filter(Boolean).join(' ')
                const name = [product, type.colour_name].filter(Boolean).join(' — ') || type.code
                const spools = data.spools.filter((row) => row.spool.type_id === type.id).length
                const detail = [type.code, colour === null ? 'clear' : colour.translucent ? 'translucent' : null, `${spools} ${spools === 1 ? 'spool' : 'spools'}`].filter(Boolean).join(' · ')
                return (
                  <CommandItem key={type.id} value={`type:${type.id}`} onSelect={() => go(`/filament/types/${encodeURIComponent(type.id)}`)} className="text-xs">
                    {colour === null ? <ClearSwatch size="sm" /> : <Swatch colour={colour.hex} size="sm" />}
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate">{name}</span>
                      <span className="sb-num truncate text-muted-foreground">{detail}</span>
                    </span>
                  </CommandItem>
                )
              })}
            </CommandGroup>
          )}
        </CommandList>
      )}
    </Command>
  )
}

/** "Spools", or "Spools: 50 of 212" when the list is cut. */
const more = (title: string, count: number) => (count > QUICK_FIND_LIMIT ? `${title}: ${QUICK_FIND_LIMIT} of ${count}, type more to narrow` : title)
