import { api, ApiError } from '../../api/client'
import type { BambuddyTargets, PrinterRackUsage } from '../../api/types'
import { flowLabel } from '../../components/print/rackLabels'
import { Spinner } from '../../components/ui/Spinner'
import { formatDuration, timeAgo } from '../../lib/format'
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

/**
 * #1298 — the counts behind the Least used rack algorithm: what each hotend on a printer's
 * rack has printed since ScadBuddy first saw it. Printers with no rack are left out.
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
  const reads = (state.data ?? []).filter((read) => read.error !== null || (read.usage?.hotends.length ?? 0) > 0)
  if (reads.length === 0) return null

  return (
    <div className="space-y-3">
      {reads.map((read) => (
        <div key={read.printerId}>
          <h3 className="text-[13px] font-medium">Hotend usage · {read.name}</h3>
          {read.error !== null ? (
            <p role="alert" className="text-[12px] text-warn">
              {read.error}
            </p>
          ) : (
            <table className="w-full text-left text-[13px]" aria-label={`Hotend usage for ${read.name}`}>
              <thead className="text-[12px] text-muted">
                <tr>
                  <th className="py-1 pr-3 font-normal">Position</th>
                  <th className="py-1 pr-3 font-normal">Hotend</th>
                  <th className="py-1 pr-3 font-normal">Prints</th>
                  <th className="py-1 pr-3 font-normal">Print time</th>
                  <th className="py-1 font-normal">Last used</th>
                </tr>
              </thead>
              <tbody>
                {read.usage?.hotends.map((hotend) => (
                  <tr key={hotend.position} className="border-t border-line align-top">
                    <td className="sb-num py-1.5 pr-3">{hotend.position}</td>
                    <td className="py-1.5 pr-3">
                      {hotend.nozzle_diameter} mm {flowLabel(hotend.high_flow ? 'high_flow' : 'standard')}
                    </td>
                    <td className="sb-num py-1.5 pr-3">
                      {hotend.prints}
                      {hotend.pending > 0 && <span className="text-muted"> (+{hotend.pending} queued)</span>}
                    </td>
                    <td className="sb-num py-1.5 pr-3">{hotend.print_seconds > 0 ? formatDuration(hotend.print_seconds) : '—'}</td>
                    <td className="py-1.5">{hotend.last_used_at ? timeAgo(hotend.last_used_at) : 'Never'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ))}
    </div>
  )
}
