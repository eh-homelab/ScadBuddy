import { api, ApiError } from '../../api/client'
import type { BambuddyTargets, PrinterRackUsage, RackHotendUsage, RackSpoolUse } from '../../api/types'
import { flowLabel } from '../../components/print/rackLabels'
import { Badge } from '@/components/ui/badge'
import { LoadingLines, Tip } from '../../components/settings/form'
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
    return <span aria-hidden="true" className="size-3.5 shrink-0 rounded-[3px] border border-dashed" />
  }
  const hex = normalizeHex(colour)
  return (
    <Tip content={hex}>
      <span
        role="img"
        aria-label={label}
        tabIndex={0}
        style={{ background: hex }}
        className="size-3.5 shrink-0 rounded-[3px] ring-1 ring-black/25 outline-none ring-inset focus-visible:ring-2 focus-visible:ring-ring"
      />
    </Tip>
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
    <li aria-label={`Position ${hotend.position}`} className="space-y-1.5 border-t py-2.5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <span className="font-medium">
          Position <span className="sb-num">{hotend.position}</span> · {hotend.nozzle_diameter} mm{' '}
          {flowLabel(hotend.high_flow ? 'high_flow' : 'standard')}
        </span>
        <span className="font-mono text-[12px] break-all text-muted-foreground">
          {hotend.serial ? `Serial ${hotend.serial}` : 'No serial reported'}
        </span>
      </div>
      <dl className="flex flex-wrap gap-x-4 gap-y-1 text-[12px]">
        <div className="flex items-center gap-1.5">
          <dt className="text-muted-foreground">Loaded</dt>
          <dd className="flex items-center gap-1.5">
            <Swatch colour={hotend.filament_colour} label={`${loaded} ${hotend.filament_colour ?? ''}`.trim()} />
            {loaded}
          </dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-muted-foreground">Wear</dt>
          <dd className={wearKnown(hotend.wear) ? 'sb-num' : 'text-muted-foreground'}>{wearLabel(hotend.wear)}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-muted-foreground">Prints</dt>
          <dd className="sb-num">
            {hotend.prints}
            {hotend.pending > 0 && <span className="text-muted-foreground"> (+{hotend.pending} queued)</span>}
          </dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-muted-foreground">Print time</dt>
          <dd className="sb-num">{hotend.print_seconds > 0 ? formatDuration(hotend.print_seconds) : '—'}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-muted-foreground">Last used</dt>
          <dd>{hotend.last_used_at ? timeAgo(hotend.last_used_at) : 'Never'}</dd>
        </div>
      </dl>
      {spools.length > 0 && (
        <div className="flex items-baseline gap-1.5 text-[12px]">
          <span className="text-muted-foreground">Ran</span>
          <ul aria-label={`Spools run through position ${hotend.position}`} className="flex min-w-0 flex-wrap gap-1.5">
            {spools.map((spool, index) => (
              <li key={spool.spool_id ?? `label-${spool.label ?? index}`}>
                <Badge variant="outline" className="h-6 gap-1.5 font-normal">
                  <Swatch colour={spool.colour} label={`${spoolName(spool)} ${spool.colour ?? ''}`.trim()} />
                  <span>{spoolName(spool)}</span>
                  <span className="sb-num whitespace-nowrap text-muted-foreground">
                    ×{spool.prints}
                    {spool.grams > 0 && ` · ${Math.round(spool.grams)} g`}
                  </span>
                </Badge>
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
    return <LoadingLines label="Loading hotend usage" lines={['w-1/3', 'w-full', 'w-5/6']} />
  }
  const reads = state.data ?? []
  const racks = reads.filter((read) => (read.usage?.hotends.length ?? 0) > 0)
  const failed = reads.filter((read) => read.error !== null)
  if (racks.length === 0 && failed.length === 0) return null

  return (
    <div className="space-y-3">
      {racks.map((read) => (
        <section key={read.printerId} aria-label={`Hotend usage for ${read.name}`} className="min-w-0">
          <h4 className="text-sm font-medium">Hotend usage · {read.name}</h4>
          <ul className="text-sm">
            {read.usage?.hotends.map((hotend) => (
              <HotendRow key={hotend.serial ?? `position-${hotend.position}`} hotend={hotend} />
            ))}
          </ul>
        </section>
      ))}
      {failed.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Hotend usage could not be read for {failed.map((read) => `${read.name} (${read.error})`).join(', ')}.
        </p>
      )}
    </div>
  )
}
