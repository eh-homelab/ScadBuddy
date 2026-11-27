import { useCallback, useEffect, useRef, useState } from 'react'
import { ATTENTION_POLL_MS } from '../agent/attention'
import { listUnknownTrays, type TagMadeSpool, type UnknownSpoolTray, type UnknownSpoolTrays } from '../api/unknownTrays'
import { filamentLinks } from './filamentLinks'
import { toast } from './toast'

/**
 * #7143 — loaded trays that no spool is linked to. The shell reads them as one more "Needs you"
 * source (`needsYou.ts`), and the resolve dialog (`components/filament/UnknownSpoolDialog.tsx`)
 * asks which spool is in one.
 */

/**
 * The spool code a scan or a typed text names: `S-142`, `s142`, a label's URL ending `/x/S-142`.
 * A place code (`L-3`) or any other text gives null.
 */
export function codeFromScan(text: string): string | null {
  const last = text.trim().split(/[?#]/)[0]!.split('/').filter(Boolean).pop() ?? ''
  const hit = /^S-?(\d+)$/i.exec(last.trim())
  return hit ? `S-${hit[1]}` : null
}

/** The net grams that the AMS's remain percent means for a label weight; null when either is not known. */
export function prefillNetWeight(remainPct: number | null | undefined, labelWeightG: number | null | undefined): number | null {
  return remainPct != null && labelWeightG != null ? Math.round((remainPct * labelWeightG) / 100) : null
}

export const unknownSpoolTitle = (tray: UnknownSpoolTray): string => `Unknown spool in ${tray.printer_name} ${tray.slot}`

/**
 * The unknown trays: read at once, every `ATTENTION_POLL_MS`, and when the tab comes back into
 * view. A failed read keeps the last answer. A 404 (the area is not switched on) is an empty
 * answer, which `listUnknownTrays` gives.
 */
export function useUnknownSpools(): { data: UnknownSpoolTrays | null; refresh: () => void } {
  const [data, setData] = useState<UnknownSpoolTrays | null>(null)
  const inflight = useRef(false)
  const again = useRef(false)
  const live = useRef(true)

  const refresh = useCallback(function read() {
    if (inflight.current) {
      again.current = true
      return
    }
    inflight.current = true
    again.current = false
    listUnknownTrays()
      .then(
        (next) => {
          if (live.current) setData(next)
        },
        () => {},
      )
      .finally(() => {
        inflight.current = false
        if (again.current && live.current) read()
      })
  }, [])

  useEffect(() => {
    live.current = true
    refresh()
    const timer = setInterval(refresh, ATTENTION_POLL_MS)
    const onVisible = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', refresh)
    return () => {
      live.current = false
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', refresh)
    }
  }, [refresh])

  return { data, refresh }
}

/** How long a "made from its tag" toast stays. */
export const TAG_MADE_TOAST_MS = 15_000

/**
 * One toast for each spool made from an RFID tag that first appears after the first answer. What
 * the first answer holds is not announced again after a reload.
 */
export function useTagMadeToasts(made: readonly TagMadeSpool[] | null): void {
  const seen = useRef<Set<string> | null>(null)
  useEffect(() => {
    if (made === null) return
    const before = seen.current
    seen.current = new Set(made.map((one) => one.spool_id))
    if (before === null) return
    for (const one of made) {
      if (before.has(one.spool_id)) continue
      toast({
        title: `Spool ${one.code} made from its RFID tag`,
        detail: `${one.printer_name} ${one.slot}`,
        actions: [{ label: 'Open', href: filamentLinks.spool(one.spool_id) }],
        ms: TAG_MADE_TOAST_MS,
      })
    }
  }, [made])
}
