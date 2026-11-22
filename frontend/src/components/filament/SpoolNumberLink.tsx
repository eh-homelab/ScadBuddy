import type { ReactNode } from 'react'
import type { SpoolOption } from '../../api/types'
import { NEW_TAB } from '../../lib/embed'
import { filamentLinks } from '../../lib/filamentLinks'
import { AppLink } from '../AppLink'

/**
 * #6869 — an SB spool's number in the print dialog, as a link to its page. A new tab, as
 * `NeedsTypeBadge`'s, so the dialog and its choices stay open; a click does not pick the row.
 * Text when the option has no SB record.
 */
export function SpoolNumberLink({ spool, children, className }: { spool: Pick<SpoolOption, 'sb_spool_id'>; children: ReactNode; className?: string }) {
  if (!spool.sb_spool_id) return <span className={className}>{children}</span>
  return (
    <AppLink href={filamentLinks.spool(spool.sb_spool_id)} {...NEW_TAB} onClick={(event) => event.stopPropagation()} className={`${className ?? ''} underline-offset-2 hover:underline`}>
      {children}
    </AppLink>
  )
}
