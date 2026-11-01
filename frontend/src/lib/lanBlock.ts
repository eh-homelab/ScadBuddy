import type { LanBlock } from '../api/types'
import { timeAgo } from './format'

export type LanBlockReason = LanBlock['reasons'][number]

/** The Settings sections that fix each reason: the backends, or the printer's own row. */
const BACKENDS = '/settings/library#projects'
const PRINTERS = '/settings/printing#printers'

/**
 * What one of the server's reasons (`print_run.lan_block`) says, and where it is fixed.
 * Only the server's reasons are said: the dialog never works one out itself.
 */
export function lanBlockReason(reason: LanBlockReason, seenAt?: string | null): { text: string; href: string } {
  switch (reason) {
    case 'slicer_backend':
      return { text: 'the slicer backend is not native', href: BACKENDS }
    case 'library_backend':
      return { text: 'the print library is not native', href: BACKENDS }
    case 'store_backend':
      return { text: 'the blob store is neither S3 nor Bambuddy', href: BACKENDS }
    case 'preferred_path':
      return { text: 'its path is set to Bambuddy', href: PRINTERS }
    case 'no_lan_link':
      return { text: 'it has no LAN link', href: PRINTERS }
    case 'no_access_code':
      return { text: 'it has no access code', href: PRINTERS }
    case 'no_report':
      return { text: 'it has sent no LAN report yet', href: PRINTERS }
    case 'stale_report':
      return {
        text: seenAt ? `its last LAN report was ${timeAgo(seenAt)}` : 'its LAN report is out of date',
        href: PRINTERS,
      }
  }
}
