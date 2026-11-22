import { PaletteIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { ApiError } from '../../api/client'
import { listCatalog, type CatalogRows } from '../../api/filament'
import { alphaOf, CLEAR_MAX_ALPHA } from '../../lib/rgba'
import { useDebounced } from '../../lib/useDebounced'
import { RgbaSwatch } from './RgbaSwatch'
import { Button } from '@/components/ui/button'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Skeleton } from '@/components/ui/skeleton'

type CatalogColour = CatalogRows['colours']

/** The rows the list draws; typing more narrows the rest in. */
export const CATALOG_PICK_LIMIT = 50

/** The pause after the last key before the catalog is asked. */
const SEARCH_DEBOUNCE_MS = 200

type State =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'failed'; message: string }
  | { kind: 'open'; query: string; rows: CatalogColour[] }

/**
 * #6912 — pick a colour of the colour catalog (#6907) by name. The catalog holds thousands
 * of synced rows, so the search is the server's (`GET /filament/catalog/colours?q=`, as the
 * Colours tab, Decision 215): every word must match the name, the maker, the maker's
 * material label or the `#RRGGBBAA`, in any case. Archived rows are never listed. Each row
 * shows the swatch (its alpha over the checkerboard, and its extra colours), the name, the
 * maker and the material. `onPick` gets the row; the field takes its `#RRGGBBAA` whole.
 */
export function CatalogColourPick({ onPick }: { onPick: (colour: CatalogColour) => void }) {
  const [open, setOpen] = useState(false)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button type="button" size="xs" variant="outline">
          <PaletteIcon /> From the catalog
        </Button>
      </PopoverTrigger>
      {/* As the colour parameter's popover: wide enough for a maker and material on one line, inside a phone's viewport. */}
      <PopoverContent align="start" className="w-[min(26rem,calc(100vw-2rem))] p-0">
        {open && (
          <CatalogColourList
            onPick={(colour) => {
              onPick(colour)
              setOpen(false)
            }}
          />
        )}
      </PopoverContent>
    </Popover>
  )
}

function CatalogColourList({ onPick }: { onPick: (colour: CatalogColour) => void }) {
  const [text, setText] = useState('')
  const query = useDebounced(text.trim(), SEARCH_DEBOUNCE_MS)
  const [state, setState] = useState<State>({ kind: 'idle' })
  const [materials, setMaterials] = useState<Map<string, string>>(new Map())

  // The material of a row whose maker gives no label is the materials catalog's name.
  useEffect(() => {
    let live = true
    listCatalog('materials', { includeArchived: true }).then(
      (rows) => live && setMaterials(new Map(rows.map((row) => [row.id, row.name]))),
      () => {},
    )
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    if (query === '') {
      setState({ kind: 'idle' })
      return
    }
    // A late answer to an older query never replaces a newer one.
    let live = true
    setState({ kind: 'loading' })
    listCatalog('colours', { q: query }).then(
      (rows) => live && setState({ kind: 'open', query, rows: rows.filter((row) => !row.archived_at) }),
      (cause: unknown) =>
        live && setState({ kind: 'failed', message: cause instanceof ApiError ? cause.detail : 'The colour catalog could not be read.' }),
    )
    return () => {
      live = false
    }
  }, [query])

  const rows = state.kind === 'open' ? state.rows : []
  return (
    <Command shouldFilter={false} label="Search the colour catalog" className="rounded-lg!">
      <CommandInput placeholder="Search: name, maker, material, hex" value={text} onValueChange={setText} />
      {state.kind === 'idle' && <p className="px-3 py-4 text-xs text-muted-foreground">Type a colour name, maker, material or hex.</p>}
      {state.kind === 'loading' && (
        <div role="status" aria-label="Searching the colour catalog" className="flex flex-col gap-1.5 p-2">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      )}
      {state.kind === 'failed' && (
        <p role="alert" className="px-3 py-4 text-xs text-destructive">
          {state.message}
        </p>
      )}
      {state.kind === 'open' && (
        <CommandList label="Catalog colours" className="max-h-72">
          <CommandEmpty>{`No catalog colour matches “${state.query}”.`}</CommandEmpty>
          {rows.length > 0 && (
            <CommandGroup heading={rows.length > CATALOG_PICK_LIMIT ? `${CATALOG_PICK_LIMIT} of ${rows.length}, type more to narrow` : undefined}>
              {rows.slice(0, CATALOG_PICK_LIMIT).map((colour) => {
                const material = colour.material_name ?? (colour.material_id ? materials.get(colour.material_id) : null) ?? null
                const alpha = alphaOf(colour.rgba)
                const detail = [colour.manufacturer, material, alpha === 0xff ? null : alpha <= CLEAR_MAX_ALPHA ? 'clear' : 'translucent'].filter(Boolean).join(' · ')
                return (
                  <CommandItem key={colour.id} value={`colour:${colour.id}`} onSelect={() => onPick(colour)} className="text-xs">
                    <span className="flex shrink-0 items-center">
                      <RgbaSwatch rgba={colour.rgba} size="md" />
                      {/* A multi-colour filament's other colours, overlapping its first. */}
                      {(colour.extra_colours ?? []).slice(0, 3).map((extra, index) => (
                        <RgbaSwatch key={index} rgba={extra} size="sm" data-testid="rgba-extra" className="-ml-1.5 ring-2 ring-popover" />
                      ))}
                    </span>
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate">{colour.name}</span>
                      {detail && <span className="truncate text-muted-foreground">{detail}</span>}
                    </span>
                    <span className="sb-num shrink-0 text-muted-foreground">{colour.rgba}</span>
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
