import { useEffect, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { InventorySpool } from '../../api/types'
import { ClearSwatch, Swatch, UnknownSwatch } from '../../components/print/Swatch'
import { bambuddyBase } from '../../lib/bambuddyLinks'
import { spoolChip, spoolName } from '../../lib/filamentColour'
import { trayPlace } from '../../lib/filaments'
import { Card } from '@/components/ui/card'
import { LoadingLines, SwitchField } from '../../components/settings/form'

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

  // #3357: every re-read goes through here, so none skips the reset. The last answer's
  // error or setup note is not this read's; the result stays, marked as being read
  // again, until the new one answers.
  const showArchived = (next: boolean) => {
    setArchived(next)
    setReading(true)
    setError(null)
    setUnconfigured(false)
  }

  const base = bambuddyBase()
  const total = (spools ?? []).filter((spool) => !spool.archived).reduce((sum, spool) => sum + spool.remaining_g, 0)
  return (
    <div data-testid="spool-inventory">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-sm">
        <SwitchField
          id="spool-inventory-archived"
          label="Include archived spools"
          checked={archived}
          onCheckedChange={showArchived}
          userOnly={false}
          className="items-center"
        />
        {base && (
          <a href={`${base}/inventory`} target="_blank" rel="noreferrer" className="text-primary hover:underline">
            Manage in Bambuddy
          </a>
        )}
      </div>
      {unconfigured && (
        <p className="text-xs text-muted-foreground" data-testid="spool-inventory-unconfigured">
          Bambuddy isn't connected yet, so there are no spools to show.
        </p>
      )}
      {error && (
        <p className="text-xs text-warn" data-testid="spool-inventory-error">
          {error}
        </p>
      )}
      {spools === null && !error && !unconfigured && (
        <LoadingLines label="Reading Bambuddy's inventory…" lines={['w-1/3', 'w-full', 'w-full', 'w-full']} />
      )}
      {spools !== null && (
        // #3486 review: the total and the empty note are the last selection's too, so the
        // whole result is marked as being read again, not the table alone.
        <div data-testid="spool-inventory-result" aria-busy={reading} className={reading ? 'opacity-60' : undefined}>
          {spools.length === 0 && <p className="text-xs text-muted-foreground">Bambuddy has no spools.</p>}
          {spools.length > 0 && (
            <>
          <p className="sb-num mb-2 text-xs text-muted-foreground" data-testid="spool-inventory-total">
            {spools.filter((spool) => !spool.archived).length} spools, {(total / 1000).toFixed(2)} kg left
          </p>
          <Card size="sm" className="overflow-x-auto py-0">
            <table className="w-full text-left text-[13px]">
              <thead className="border-b text-xs text-muted-foreground">
                <tr>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Filament
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Left
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Per kg
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Where
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {spools.map((spool) => (
                  <tr
                    key={spool.id}
                    data-testid={`spool-${spool.id}`}
                    className={`transition-colors hover:bg-muted/50 ${spool.archived ? 'text-faint' : ''}`}
                  >
                    <td className="px-3 py-2">
                      <span className="flex min-w-0 items-center gap-2">
                        {colourOf(spool)}
                        <span className="truncate">
                          {spoolName(spool)}
                          {spool.archived ? ' (archived)' : ''}
                        </span>
                      </span>
                    </td>
                    <td className="sb-num px-3 py-2 text-right whitespace-nowrap">
                      {Math.round(spool.remaining_g)} / {spool.label_weight} g
                    </td>
                    <td className="sb-num px-3 py-2 text-right whitespace-nowrap">
                      {spool.cost_per_kg != null ? spool.cost_per_kg.toFixed(2) : '—'}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap text-muted-foreground">{where(spool)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
            </>
          )}
        </div>
      )}
    </div>
  )
}
