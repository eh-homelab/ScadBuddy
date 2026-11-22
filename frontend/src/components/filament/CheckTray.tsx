import { ScanSearchIcon } from 'lucide-react'
import type { FilamentInventoryView } from '../../api/filament'
import { Badge } from '@/components/ui/badge'
import { filamentLinks } from '../../lib/filamentLinks'
import { EntityLink } from './EntityLink'
import { RgbaSwatch } from './RgbaSwatch'

/** What the printer reports in the tray a spool is placed in, where the spool does not match it (#6713). */
export interface TrayCheck {
  /** The printer and the tray: "H2C HT-A". */
  place: string
  reportedType: string
  reportedRgba: string | null
}

/** Each placed spool whose untagged tray reports another material or colour, by spool id (`mismatch`, #6713). */
export function trayChecks(view: FilamentInventoryView): Map<string, TrayCheck> {
  const found = new Map<string, TrayCheck>()
  for (const printer of view.trays) {
    for (const tray of printer.trays) {
      if (!tray.spool_id || !tray.mismatch) continue
      found.set(tray.spool_id, {
        place: `${printer.printer_name} ${tray.label}`,
        reportedType: tray.mismatch.reported_type,
        reportedRgba: tray.mismatch.reported_rgba ?? null,
      })
    }
  }
  return found
}

/**
 * "Check this tray", and what the printer reports there: the spool placed in the tray does
 * not match it, and Bambuddy names no spool that does, so nothing moved (#6713). The
 * colour is shown without its alpha, as the match ignores it.
 */
export function CheckTray({ check }: { check?: TrayCheck }) {
  if (!check) return null
  const colour = check.reportedRgba?.slice(0, 7)
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <Badge variant="outline" className="border-warn/50 font-normal text-warn">
        <ScanSearchIcon /> Check this tray
      </Badge>
      <span className="inline-flex items-center gap-1 text-[12px] text-muted-foreground">
        {check.reportedRgba && <RgbaSwatch rgba={check.reportedRgba} size="sm" />}
        <span>
          <EntityLink to={filamentLinks.printers}>{check.place}</EntityLink>
          {` reports ${check.reportedType}${colour ? ` ${colour}` : ''}`}
        </span>
      </span>
    </span>
  )
}
