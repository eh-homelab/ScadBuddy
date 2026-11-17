import { Badge } from '@/components/ui/badge'
import type { SpoolOption } from '../../api/types'
import { NEW_TAB } from '../../lib/embed'
import { AppLink } from '../AppLink'

/**
 * #6262 — the print dialog's mark for an SB spool with no type yet: a badge that opens
 * the spool's page in the Filament section, where its type is set. That page is
 * ScadBuddy's own, so it is an `AppLink` (new tab, so the dialog and its choices stay open), not a Bambuddy deep link.
 * Nothing for a spool that has a type or no SB record.
 */
export function NeedsTypeBadge({ spool }: { spool: Pick<SpoolOption, 'needs_type' | 'sb_spool_id'> }) {
  if (!spool.needs_type || !spool.sb_spool_id) return null
  return (
    <Badge variant="outline" className="text-warn" asChild>
      <AppLink
        href={`/filament/spools/${spool.sb_spool_id}`}
        {...NEW_TAB}
        onClick={(event) => event.stopPropagation()}
      >Needs type</AppLink>
    </Badge>
  )
}
