import { api, ApiError } from '../../api/client'
import type { BambuddyTargets, PrinterRackUsage, RackHotendUsage, RackSpoolUse } from '../../api/types'
import { flowLabel } from '../../components/print/rackLabels'
import { Spinner } from '../../components/ui/Spinner'
import { formatDuration, normalizeHex, timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'

type Read = { printerId: number; name: string; usage: PrinterRackUsage | null; error: string | null }

async function readAll(targets: BambuddyTargets): Promise<Read[]> {
  return Promise.all(
    (targets.printers ?? []).map(async (printer) => {
      try {
        return { printerId: printer.id, name: printer.name, usage: await api.getPrinterRackUsage(printer.id), error: null }
      } catch (cause) {
        const error = cause instanceof ApiError ? cause.detail : 'Could not read its hotend usage.'
        return { printerId: printer.id, name: printer.name, usage: null, error }
      }
    }),
  )
}

function Swatch({ colour, label }: { colour: string | null | undefined; label: string }) {
  if (!colour) {
    return <span aria-hidden="true" className="size-3.5 shrink-0 rounded-[3px] border border-dashed border-line" />
  }
  const hex = normalizeHex(colour)
  return (
    <span
      role="img"
      aria-label={label}
      title={hex}
      style={{ background: hex }}
      className="size-3.5 shrink-0 rounded-[3px] ring-1 ring-black/25 ring-inset"
    />
  )
}

/** The firmware's wear figure is a percentage; the H2C reports 128 for most hotends, which is not one. */
function wearKnown(wear: number | null | undefined): wear is number {
  return wear !== null && wear !== undefined && wear >= 0 && wear <= 100
}

function wearLabel(wear: number | null | undefined): string {
  return wearKnown(wear) ? `${wear}%` : 'not reported'
}

/** What the hotend last ran, as the printer reports it (#2170). */
function loadedLabel(hotend: RackHotendUsage): string {
  if (hotend.filament_name) return hotend.filament_name
  if (hotend.filament_material) return hotend.filament_material
  if (hotend.filament_id) return hotend.filament_id
  return hotend.filament_colour ? 'Unknown filament' : 'Nothing loaded'
}

function spoolName(spool: RackSpoolUse): string {
  return spool.label || spool.material || (spool.spool_id !== null && spool.spool_id !== undefined ? `Spool #${spool.spool_id}` : 'Unknown spool')
}

function HotendRow({ hotend }: { hotend: RackHotendUsage }) {
  const loaded = loadedLabel(hotend)
  const spools = hotend.spools ?? []
  return (
    <li aria-label={`Position ${hotend.position}`} className="space-y-1.5 border-t border-line py-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <span className="font-medium">
          Position <span className="sb-num">{hotend.position}</span> · {hotend.nozzle_diameter} mm{' '}
          {flowLabel(hotend.high_flow ? 'high_flow' : 'standard')}
        </span>
        <span className="font-mono text-[12px] break-all text-muted">
          {hotend.serial ? `Serial ${hotend.serial}` : 'No serial reported'}
        </span>
      </div>
      <dl className="flex flex-wrap gap-x-4 gap-y-1 text-[12px]">
        <div className="flex items-center gap-1.5">
          <dt className="text-muted">Loaded</dt>
          <dd className="flex items-center gap-1.5">
            <Swatch colour={hotend.filament_colour} label={`${loaded} ${hotend.filament_colour ?? ''}`.trim()} />
            {loaded}
          </dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-muted">Wear</dt>
          <dd className={wearKnown(hotend.wear) ? 'sb-num' : 'text-muted'}>{wearLabel(hotend.wear)}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-muted">Prints</dt>
          <dd className="sb-num">
            {hotend.prints}
            {hotend.pending > 0 && <span className="text-muted"> (+{hotend.pending} queued)</span>}
          </dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-muted">Print time</dt>
          <dd className="sb-num">{hotend.print_seconds > 0 ? formatDuration(hotend.print_seconds) : '—'}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-muted">Last used</dt>
          <dd>{hotend.last_used_at ? timeAgo(hotend.last_used_at) : 'Never'}</dd>
        </div>
      </dl>
      {spools.length > 0 && (
        <div className="flex items-baseline gap-1.5 text-[12px]">
          <span className="text-muted">Ran</span>
          <ul aria-label={`Spools run through position ${hotend.position}`} className="flex min-w-0 flex-wrap gap-1.5">
            {spools.map((spool, index) => (
              <li
                key={spool.spool_id ?? `label-${spool.label ?? index}`}
                className="flex items-center gap-1.5 rounded border border-line px-1.5 py-0.5"
              >
                <Swatch colour={spool.colour} label={spool.colour ?? ''} />
                <span>{spoolName(spool)}</span>
                <span className="sb-num whitespace-nowrap text-muted">
                  ×{spool.prints}
                  {spool.grams > 0 && ` · ${Math.round(spool.grams)} g`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </li>
  )
}

/**
 * #1298, #2170 — each hotend on a printer's rack: its serial, wear and what it has loaded,
 * the counts behind the Least used rack algorithm, and every spool that ran through it.
 * The usage follows the serial, so a hotend moved to another position keeps it. Printers
 * with no rack are left out, and one whose read failed is named in one quiet note (#2102):
 * an offline printer with no rack is no rack problem.
 */
export function HotendUsagePanel({ targets }: { targets: BambuddyTargets | null | undefined }) {
  const state = useAsync(() => (targets ? readAll(targets) : Promise.resolve([])), [targets])

  if (!targets) return null
  if (state.loading) {
    return (
      <p className="flex items-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading hotend usage
      </p>
    )
  }
  const reads = state.data ?? []
  const racks = reads.filter((read) => (read.usage?.hotends.length ?? 0) > 0)
  const failed = reads.filter((read) => read.error !== null)
  if (racks.length === 0 && failed.length === 0) return null

  return (
    <div className="space-y-3">
      {racks.map((read) => (
        <section key={read.printerId} aria-label={`Hotend usage for ${read.name}`} className="min-w-0">
          <h3 className="text-[13px] font-medium">Hotend usage · {read.name}</h3>
          <ul className="text-[13px]">
            {read.usage?.hotends.map((hotend) => (
              <HotendRow key={hotend.serial ?? `position-${hotend.position}`} hotend={hotend} />
            ))}
          </ul>
        </section>
      ))}
      {failed.length > 0 && (
        <p className="text-[12px] text-muted">
          Hotend usage could not be read for {failed.map((read) => `${read.name} (${read.error})`).join(', ')}.
        </p>
      )}
    </div>
  )
}
