import { useEffect, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { InventorySpool } from '../../api/types'
import { trayPlace } from '../../lib/filaments'
import { spoolColour, spoolName } from '../../lib/filamentColour'
import { ClearSwatch, Swatch } from '../print/Swatch'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { Skeleton } from '@/components/ui/skeleton'

type State =
  | { kind: 'loading' }
  | { kind: 'failed'; message: string }
  | { kind: 'open'; spools: InventorySpool[] }

/** Loaded spools first, then the fullest. */
function order(a: InventorySpool, b: InventorySpool): number {
  if (!!a.loaded !== !!b.loaded) return a.loaded ? -1 : 1
  return b.remaining_g - a.remaining_g
}

/**
 * #1026 — take a colour parameter's hex from a filament in Bambuddy's inventory, so the
 * preview shows what will actually print. The inventory is read when the list is shown
 * (the colour popover opens), never on page load. A clear spool with no recorded tint
 * has no colour to take and is listed disabled; a translucent one gives its tint.
 */
export function FilamentColourPicker({
  label,
  value,
  onPick,
}: {
  label: string
  value: string
  onPick: (hex: string) => void
}) {
  const [state, setState] = useState<State>({ kind: 'loading' })
  // Each mount is one read; closing the popover unmounts the list, so a late answer
  // never lands anywhere.
  useEffect(() => {
    let live = true
    api.getSpoolInventory().then(
      (view) => {
        if (live) setState({ kind: 'open', spools: [...(view.spools ?? [])].filter((s) => !s.archived).sort(order) })
      },
      (cause: unknown) => {
        if (!live) return
        setState({
          kind: 'failed',
          message: cause instanceof ApiError ? cause.detail : "Bambuddy's filaments could not be read.",
        })
      },
    )
    return () => {
      live = false
    }
  }, [])

  if (state.kind === 'loading') {
    return (
      <div role="status" aria-label="Reading Bambuddy's filaments" className="flex flex-col gap-1.5 p-1">
        <Skeleton className="h-7 w-full" />
        <Skeleton className="h-7 w-full" />
      </div>
    )
  }
  if (state.kind === 'failed') {
    return (
      <p className="px-1 text-xs text-destructive" role="alert">
        {state.message}
      </p>
    )
  }
  if (state.spools.length === 0) return <p className="px-1 text-xs text-muted-foreground">Bambuddy has no spools.</p>

  return (
    <Command className="rounded-lg! border p-0">
      {state.spools.length > 6 && <CommandInput placeholder="Search filaments" />}
      <CommandList aria-label={`Filaments for ${label}`} className="max-h-60">
        <CommandEmpty>No filament matches.</CommandEmpty>
        <CommandGroup>
          {state.spools.map((spool) => {
            const colour = spoolColour(spool)
            const name = spoolName(spool)
            const chosen = colour !== null && colour.hex === value
            // The printer too: on a farm, two spools can both be in AMS-A slot 1.
            const where = spool.loaded ? trayPlace(spool.loaded, { printer: true }) : null
            const detail =
              colour === null
                ? 'clear, no tint recorded'
                : [colour.translucent ? 'translucent' : null, where, `${Math.round(spool.remaining_g)} g`]
                    .filter(Boolean)
                    .join(' · ')
            return (
              <CommandItem
                key={spool.id}
                value={`${spool.id} ${name} ${detail}`}
                disabled={colour === null}
                data-checked={chosen}
                onSelect={() => {
                  if (colour) onPick(colour.hex)
                }}
                className="text-xs"
              >
                {colour === null ? <ClearSwatch size="sm" /> : <Swatch colour={colour.hex} size="sm" />}
                <span className="min-w-0 flex-1 truncate">{name}</span>
                <span className="sb-num shrink-0 text-muted-foreground">{detail}</span>
                {chosen && <span className="sr-only">(the colour now)</span>}
              </CommandItem>
            )
          })}
        </CommandGroup>
      </CommandList>
    </Command>
  )
}
