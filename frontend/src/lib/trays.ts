import { useState } from 'react'
import type { SpoolOption, UnknownTray } from '../api/types'

const DECLINED_KEY = 'scadbuddy.tray-declined'

function trayKey(printerId: number, tray: UnknownTray): string {
  return `${printerId}:${tray.ams_id}:${tray.tray_id}`
}

function readDeclined(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(DECLINED_KEY) ?? '{}') as Record<string, string>
  } catch {
    return {}
  }
}

/**
 * #2164 — the trays a person answered "no" about, remembered in this browser until the
 * tray changes: the fingerprint (material, colour, state) is kept beside the "no", so a
 * tray reloaded with something else is asked about again.
 */
export function useDeclinedTrays(printerId: number | null | undefined, trays: readonly UnknownTray[]) {
  const [declined, setDeclined] = useState(readDeclined)
  const isDeclined = (tray: UnknownTray) =>
    printerId != null && declined[trayKey(printerId, tray)] === tray.fingerprint
  function decline(tray: UnknownTray) {
    if (printerId == null) return
    const next = { ...readDeclined(), [trayKey(printerId, tray)]: tray.fingerprint }
    try {
      localStorage.setItem(DECLINED_KEY, JSON.stringify(next))
    } catch {
      // Not remembered past this dialog; it is still answered for now.
    }
    setDeclined(next)
  }
  return {
    /** The trays still to ask about. */
    open: trays.filter((tray) => !isDeclined(tray)),
    /** The spool ids of the trays offered as themselves, after a "no". */
    offered: new Set(trays.filter(isDeclined).map((tray) => tray.spool_id)),
    decline,
  }
}

/** "about half full, 515 g", from what is left and the label weight. */
export function howFull(spool: SpoolOption): string | null {
  const left = spool.remaining_g
  if (left === null || left === undefined) return null
  const grams = `${Math.round(left)} g`
  if (left <= 0) return 'empty, 0 g'
  const label = spool.label_weight_g ?? 0
  if (label <= 0) return `${grams} left`
  const share = left / label
  const word =
    share > 0.85 ? 'about full' : share > 0.6 ? 'about three-quarters full' : share > 0.35 ? 'about half full' : share > 0.15 ? 'about a quarter full' : 'nearly empty'
  return `${word}, ${grams}`
}
