import { useId, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { InventorySpool } from '../../api/types'
import { amsUnitLabel } from '../../lib/filaments'
import { spoolColour, spoolName } from '../../lib/filamentColour'
import { Swatch } from '../print/Swatch'

type State =
  | { kind: 'closed' }
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
 * preview shows what will actually print. The inventory is read when the list is
 * opened, never on page load. A clear spool with no recorded tint has no colour to
 * take and is listed disabled; a translucent one gives its tint.
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
  const [state, setState] = useState<State>({ kind: 'closed' })
  const listId = useId()
  const open = state.kind !== 'closed'

  const toggle = () => {
    if (open) {
      setState({ kind: 'closed' })
      return
    }
    setState({ kind: 'loading' })
    api.getSpoolInventory().then(
      (view) => setState({ kind: 'open', spools: [...(view.spools ?? [])].filter((s) => !s.archived).sort(order) }),
      (cause: unknown) =>
        setState({
          kind: 'failed',
          message: cause instanceof ApiError ? cause.detail : "Bambuddy's filaments could not be read.",
        }),
    )
  }

  return (
    <div className="mt-1.5 text-[12px]">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={listId}
        onClick={toggle}
        className="text-accent hover:underline"
      >
        {open ? 'Hide filaments' : 'Choose from a filament'}
      </button>
      {open && (
        <div id={listId} className="mt-1.5 rounded-[6px] border border-line bg-surface p-1.5">
          {state.kind === 'loading' && <p className="px-1 text-muted">Reading Bambuddy's filaments…</p>}
          {state.kind === 'failed' && (
            <p className="px-1 text-warn" role="alert">
              {state.message}
            </p>
          )}
          {state.kind === 'open' && state.spools.length === 0 && (
            <p className="px-1 text-muted">Bambuddy has no spools.</p>
          )}
          {state.kind === 'open' && state.spools.length > 0 && (
            <ul aria-label={`Filaments for ${label}`} className="max-h-64 space-y-0.5 overflow-y-auto">
              {state.spools.map((spool) => {
                const colour = spoolColour(spool)
                const name = spoolName(spool)
                const chosen = colour !== null && colour.hex === value
                const where = spool.loaded
                  ? `${amsUnitLabel(spool.loaded.ams_id)} · slot ${spool.loaded.tray_id + 1}`
                  : null
                return (
                  <li key={spool.id}>
                    <button
                      type="button"
                      disabled={colour === null}
                      aria-pressed={chosen}
                      onClick={() => {
                        if (!colour) return
                        onPick(colour.hex)
                        setState({ kind: 'closed' })
                      }}
                      className="flex w-full items-center gap-2 rounded-[4px] px-1.5 py-1 text-left hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50 aria-pressed:bg-accent/10"
                    >
                      <Swatch colour={colour?.hex ?? null} size="sm" />
                      <span className="min-w-0 flex-1 truncate text-ink">{name}</span>
                      <span className="sb-num shrink-0 text-muted">
                        {colour === null
                          ? 'clear, no tint recorded'
                          : [colour.translucent ? 'translucent' : null, where, `${Math.round(spool.remaining_g)} g`]
                              .filter(Boolean)
                              .join(' · ')}
                      </span>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
