import type { Resolved } from '../api/filament'
import { filamentLinks } from './filamentLinks'
import { inventoryPaths } from './inventoryLinks'

const NOUN: Record<Resolved['kind'], string> = { location: 'Place', spool: 'Spool', item_kind: 'Kind' }

/** "Place L-4", "Spool S-142" or "Kind K-3": how the scan page names a thing a code resolved to (#7273, #6955). */
export const codeLabel = (resolved: Resolved) => `${NOUN[resolved.kind]} ${resolved.code}`

/** The thing's own page. An item kind opens the inventory list of its items (#6949). */
export function codeTarget(resolved: Resolved): string {
  if (resolved.kind === 'location') return filamentLinks.location(resolved.id)
  if (resolved.kind === 'item_kind') return inventoryPaths.kind(resolved.id)
  return filamentLinks.spool(resolved.id)
}
