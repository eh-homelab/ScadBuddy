import type { Resolved } from '../api/filament'
import { filamentLinks } from './filamentLinks'

/** "Place L-4" or "Spool S-142": how the scan page names a thing a code resolved to (#7273, #6955). */
export const codeLabel = (resolved: Resolved) => `${resolved.kind === 'location' ? 'Place' : 'Spool'} ${resolved.code}`

/** The thing's own page. */
export const codeTarget = (resolved: Resolved) => (resolved.kind === 'location' ? filamentLinks.location(resolved.id) : filamentLinks.spool(resolved.id))
