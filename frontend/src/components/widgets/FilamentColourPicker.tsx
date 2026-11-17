import { useEffect, useId, useState } from 'react'
import { ApiError } from '../../api/client'
import { getInventory } from '../../api/filament'
import type { FilamentInventorySpool } from '../../api/types'
import { trayPlace } from '../../lib/filaments'
import { spoolColour, spoolName, spoolValue } from '../../lib/filamentColour'
import { ClearSwatch, Swatch } from '../print/Swatch'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'

type State =
  | { kind: 'loading' }
  | { kind: 'failed'; message: string }
  | { kind: 'open'; spools: FilamentInventorySpool[] }

/** Loaded spools first, then the fullest. */
function order(a: FilamentInventorySpool, b: FilamentInventorySpool): number {
  if (!!a.loaded !== !!b.loaded) return a.loaded ? -1 : 1
  return (b.spool.grams_left ?? 0) - (a.spool.grams_left ?? 0)
}

/**
 * #1026 — take a colour parameter's hex from one of ScadBuddy's spools (#6262 Task 9: its
 * effective `#RRGGBBAA`), so the preview shows what will actually print. The inventory is read when the list is shown
 * (the colour popover opens), never on page load. A clear spool with no recorded tint
 * has no colour to take and is listed disabled; a translucent one gives its tint. `onPick`
 * gets the hex and the parameter value (`spoolValue`: with the alpha of a translucent spool).
 * With `alpha`, `value` is such a value, and the spool marked as the colour now is the one
 * whose value it is (#6243 review: a clear and an opaque spool of one tint are told
 * apart); without, the one whose hex it is.
 */
export function FilamentColourPicker({
  label,
  value,
  alpha = false,
  onPick,
}: {
  label: string
  value: string
  alpha?: boolean
  onPick: (hex: string, value: string) => void
}) {
  const [state, setState] = useState<State>({ kind: 'loading' })
  // Each mount is one read; closing the popover unmounts the list, so a late answer
  // never lands anywhere.
  useEffect(() => {
    let live = true
    getInventory().then(
      (view) => {
        if (live) setState({ kind: 'open', spools: [...(view.spools ?? [])].filter((row) => !row.spool.archived_at).sort(order) })
      },
      (cause: unknown) => {
        if (!live) return
        setState({
          kind: 'failed',
          message: cause instanceof ApiError ? cause.detail : "The spools could not be read.",
        })
      },
    )
    return () => {
      live = false
    }
  }, [])

  if (state.kind === 'loading') {
    return (
      <div role="status" aria-label="Reading the spools" className="flex flex-col gap-1.5 p-1">
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
  if (state.spools.length === 0) return <p className="px-1 text-xs text-muted-foreground">ScadBuddy has no spools.</p>

  return (
    <Command className="rounded-lg! border p-0">
      {state.spools.length > 6 && <CommandInput placeholder="Search filaments" />}
      <CommandList label={`Filaments for ${label}`} className="max-h-60">
        <CommandEmpty>No filament matches.</CommandEmpty>
        <CommandGroup>
          {state.spools.map((row) => {
            const { spool } = row
            // The effective colour: the spool's own, else its filament type's.
            const look = { rgba: spool.effective?.rgba ?? spool.rgba }
            const colour = spoolColour(look)
            const name = spoolName(row)
            const chosen = colour !== null && (alpha ? spoolValue(look) : colour.hex) === value
            // The printer too: on a farm, two spools can both be in AMS-A slot 1.
            const where = row.loaded ? trayPlace(row.loaded, { printer: true }) : null
            const grams = spool.grams_left === null ? null : `${Math.round(spool.grams_left)} g`
            const detail =
              colour === null
                ? `#${spool.number} · clear, no tint recorded`
                : [`#${spool.number}`, colour.translucent ? 'translucent' : null, where, grams].filter(Boolean).join(' · ')
            return (
              <CommandItem
                key={spool.id}
                value={`${spool.number} ${name} ${detail}`}
                disabled={colour === null}
                data-checked={chosen}
                onSelect={() => {
                  const picked = spoolValue(look)
                  if (colour && picked) onPick(colour.hex, picked)
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

/**
 * The list behind a "Choose from a filament" toggle, inline, for a place with no popover
 * to open it from (a copy's colours in Arrange). The list is read each time it opens.
 */
export function FilamentColourDisclosure({
  label,
  value,
  onPick,
}: {
  label: string
  value: string
  onPick: (hex: string) => void
}) {
  const [open, setOpen] = useState(false)
  const listId = useId()
  return (
    <div className="mt-1.5 flex flex-col gap-1.5 text-xs">
      <Button
        type="button"
        variant="link"
        size="xs"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        onClick={() => setOpen((now) => !now)}
        className="h-auto self-start px-0"
      >
        {open ? 'Hide filaments' : 'Choose from a filament'}
      </Button>
      {open && (
        <div id={listId}>
          <FilamentColourPicker
            label={label}
            value={value}
            onPick={(hex) => {
              onPick(hex)
              setOpen(false)
            }}
          />
        </div>
      )}
    </div>
  )
}
