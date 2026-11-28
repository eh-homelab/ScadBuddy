import { Link } from 'react-router'
import { filamentLinks } from '../../lib/filamentLinks'

/**
 * #7143 — what to do while the server has no unknown-spool area (core's `unknown-trays` is not
 * switched on): the list and the resolve route answer 404, but the print block already holds. It
 * also shows when core has no Temporal for the resolve (503 `writes_not_configured`). The
 * paths that work then are on the slot's page, which `placeId` names when the tray's place is known.
 */
export function UnknownSpoolAreaOff({ placeId, role }: { placeId?: string | null; role: 'status' | 'alert' }) {
  return (
    <span role={role} className="text-warn">
      This server cannot link unknown spools here yet. Link the spool from the slot&apos;s page: Filament, then Locations, then the
      place. There, &quot;New spool from this tray&quot; and the tray question work.{' '}
      <Link to={placeId ? filamentLinks.location(placeId) : filamentLinks.locations} className="underline underline-offset-2">
        {placeId ? "Open the slot's page" : 'Open Locations'}
      </Link>
    </span>
  )
}
