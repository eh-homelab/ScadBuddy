import { useEffect, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { InventorySpool } from '../../api/types'
import { ClearSwatch, Swatch, UnknownSwatch } from '../../components/print/Swatch'
import { bambuddyBase } from '../../lib/bambuddyLinks'
import { spoolChip, spoolName } from '../../lib/filamentColour'
import { trayPlace } from '../../lib/filaments'

/** Bambuddy's not-configured refusal: a setup step still to take, not a failure. */
const NOT_CONFIGURED = 'https://scadbuddy.dev/problems/bambuddy-not-configured'

/** Loaded spools first, then by material, brand and colour. */
function order(a: InventorySpool, b: InventorySpool): number {
  if (!!a.loaded !== !!b.loaded) return a.loaded ? -1 : 1
  // Field by field: a joined key would not do, as localeCompare ignores a separator
  // like \u0000 and runs the fields' letters together.
  const key = (s: InventorySpool) => [s.material, s.subtype ?? '', s.brand ?? '', s.color_name ?? '']
  const right = key(b)
  for (const [index, field] of key(a).entries()) {
    const order = field.localeCompare(right[index] ?? '')
    if (order !== 0) return order
  }
  return 0
}

function where(spool: InventorySpool): string {
  if (!spool.loaded) return spool.storage_location ?? 'On the shelf'
  return trayPlace(spool.loaded, { printer: true })
}

/** The spool's chip: its colour; a clear one only for a spool recorded clear (alpha 00);
 * otherwise, with no colour recorded or one unreadable, an unknown one. Never black. */
function colourOf(spool: InventorySpool) {
  const chip = spoolChip(spool)
  if (chip.kind === 'colour') return <Swatch colour={chip.hex} size="sm" />
  return chip.kind === 'clear' ? <ClearSwatch size="sm" /> : <UnknownSwatch size="sm" />
}

/**
 * #2351 — Bambuddy's spool inventory, read only: what is left on each spool, where it
 * is loaded and what it cost. Spools are added, weighed and edited in Bambuddy, which
 * the link opens.
 */
export function SpoolInventoryPanel() {
  const [archived, setArchived] = useState(false)
  const [spools, setSpools] = useState<InventorySpool[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [unconfigured, setUnconfigured] = useState(false)
  // #3357: a toggle's re-read is in flight, so the table on screen is the last selection.
  const [reading, setReading] = useState(false)

  useEffect(() => {
    let live = true
    // The last table stays while the re-read is in flight, so a toggle does not flicker
    // back to "Reading…"; a failed read clears it, so it never sits under its error.
    api.getSpoolInventory(archived).then(
      (view) => {
        if (!live) return
        setSpools([...(view.spools ?? [])].sort(order))
        setError(null)
        setUnconfigured(false)
        setReading(false)
      },
      (cause: unknown) => {
        if (!live) return
        setSpools(null)
        setReading(false)
        const notConfigured = cause instanceof ApiError && cause.problem.type === NOT_CONFIGURED
        setUnconfigured(notConfigured)
        setError(
          notConfigured ? null : cause instanceof ApiError ? cause.detail : "Bambuddy's inventory could not be read.",
        )
      },
    )
    return () => {
      live = false
    }
  }, [archived])

  const base = bambuddyBase()
  const total = (spools ?? []).filter((spool) => !spool.archived).reduce((sum, spool) => sum + spool.remaining_g, 0)
  return (
    <div data-testid="spool-inventory">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-[12.5px]">
        <label className="flex items-center gap-2 text-muted">
          <input
            type="checkbox"
            checked={archived}
            onChange={(event) => {
              setArchived(event.target.checked)
              // #3357: the last answer's error or setup note is not this read's; the
              // table stays, marked as being read again, until the new one answers.
              setReading(true)
              setError(null)
              setUnconfigured(false)
            }}
          />
          Include archived spools
        </label>
        {base && (
          <a href={`${base}/inventory`} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            Manage in Bambuddy
          </a>
        )}
      </div>
      {unconfigured && (
        <p className="text-[12px] text-muted" data-testid="spool-inventory-unconfigured">
          Bambuddy isn't connected yet, so there are no spools to show.
        </p>
      )}
      {error && (
        <p className="text-[12px] text-warn" data-testid="spool-inventory-error">
          {error}
        </p>
      )}
      {spools === null && !error && !unconfigured && (
        <p className="text-[12px] text-muted">Reading Bambuddy's inventory…</p>
      )}
      {spools !== null && spools.length === 0 && <p className="text-[12px] text-muted">Bambuddy has no spools.</p>}
      {spools !== null && spools.length > 0 && (
        <>
          <p className="sb-num mb-2 text-[12px] text-muted" data-testid="spool-inventory-total">
            {spools.filter((spool) => !spool.archived).length} spools, {(total / 1000).toFixed(2)} kg left
          </p>
          <div className="overflow-x-auto">
            <table
              className={`w-full text-left text-[12.5px]${reading ? ' opacity-60' : ''}`}
              aria-busy={reading}
            >
              <thead className="text-[11.5px] text-muted">
                <tr>
                  <th scope="col" className="py-1 pr-3 font-normal">
                    Filament
                  </th>
                  <th scope="col" className="py-1 pr-3 text-right font-normal">
                    Left
                  </th>
                  <th scope="col" className="py-1 pr-3 text-right font-normal">
                    Per kg
                  </th>
                  <th scope="col" className="py-1 font-normal">
                    Where
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {spools.map((spool) => (
                  <tr key={spool.id} data-testid={`spool-${spool.id}`} className={spool.archived ? 'text-faint' : ''}>
                    <td className="py-1.5 pr-3">
                      <span className="flex min-w-0 items-center gap-2">
                        {colourOf(spool)}
                        <span className="truncate">
                          {spoolName(spool)}
                          {spool.archived ? ' (archived)' : ''}
                        </span>
                      </span>
                    </td>
                    <td className="sb-num py-1.5 pr-3 text-right whitespace-nowrap">
                      {Math.round(spool.remaining_g)} / {spool.label_weight} g
                    </td>
                    <td className="sb-num py-1.5 pr-3 text-right whitespace-nowrap">
                      {spool.cost_per_kg != null ? spool.cost_per_kg.toFixed(2) : '—'}
                    </td>
                    <td className="py-1.5 whitespace-nowrap text-muted">{where(spool)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
