import { useCallback, useEffect, useRef, useState } from 'react'
import { ATTENTION_POLL_MS } from '../agent/attention'
import { listSpools, type Spool } from '../api/filament'

/**
 * #7167 — the spools that came out of a tray and whose home dry box waits for the person to say
 * which spool goes in (`GET /filament/spools?awaiting_home=true`). The shell reads them as one
 * more "Needs you" source (`needsYou.ts`); `components/filament/HomeReturnDialog.tsx` answers one.
 * The person always names the spool: nothing is chosen for them, the unloaded spool included.
 */

export const spoolCode = (spool: Pick<Spool, 'number'>) => `S-${spool.number}`

/** What the list, the toast and the notice call one waiting spool. */
export const homeReturnTitle = (spool: Pick<Spool, 'number'>) => `${spoolCode(spool)} came out of its tray. Which spool goes into its dry box?`

/**
 * The waiting spools: read at once, every `ATTENTION_POLL_MS` (as the unknown trays are), and
 * when the tab comes back into view. A failed read keeps the last answer; null until one answered.
 */
export function useHomeReturns(): { spools: Spool[] | null; refresh: () => void } {
  const [spools, setSpools] = useState<Spool[] | null>(null)
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
    listSpools({ awaitingHome: true })
      .then(
        (next) => {
          if (live.current) setSpools(next)
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

  return { spools, refresh }
}
