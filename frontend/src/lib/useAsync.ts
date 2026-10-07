import { useCallback, useEffect, useRef, useState } from 'react'
import { getRealtime } from './realtime'

export interface AsyncState<T> {
  data: T | undefined
  error: Error | undefined
  loading: boolean
  reload: () => void
  /**
   * Fetch again in the background: the current data stays until the answer lands.
   * With `accept`, the answer (if still the newest) is applied only when `accept`
   * returns true as it lands: a page holding unsaved edits decides there, and does
   * whatever else it must (a "changed elsewhere" banner) instead. `failed` is called
   * when the read (if still the newest) fails; every caller merged into that read is.
   */
  refresh: (accept?: (data: T) => boolean, failed?: () => void) => void
  /**
   * Shows `next` in place of the data. With `supersede`, a background read already in flight
   * for the same deps no longer replaces it (an action's own answer is newer than that read).
   */
  setData: (next: T, options?: { supersede?: boolean }) => void
}

/** How long a first read left to the subscription's resync waits for it (#1039). */
export const RESYNC_WAIT_MS = 500

interface Snapshot<T> {
  key: string
  data?: T
  error?: Error
}

/**
 * Runs `load` on mount and whenever `deps` change. No cache — the app is small
 * enough that a fetch per page beats carrying a query library.
 *
 * `loading` is derived from whether the settled snapshot belongs to the current
 * key, so nothing is set synchronously inside the effect.
 *
 * #269 — with `topics`, the data is live: whenever something under one of them
 * changes (`lib/realtime.ts`), `load` runs again in the background and replaces the
 * data when it answers. `loading` stays false and the old data stays on screen
 * meanwhile. A page whose form may hold unsaved edits passes no topics for that
 * data, and follows the topic itself to offer a reload rather than overwrite.
 *
 * #1039 — subscribing makes the realtime client signal `'resync'` once the topic is
 * live, and that signal reads. A read made just before it is the same read twice, so
 * when the socket is already open (`willResync`: in-app navigation) the first read is
 * left to that signal. On a cold page load the socket is not open yet; the read
 * starts at once, and the signal's read is still needed, since a change made between
 * the first read and the server's `subscribed` would otherwise go unseen.
 */
export function useAsync<T>(
  load: () => Promise<T>,
  deps: readonly unknown[],
  topics: readonly string[] = [],
): AsyncState<T> {
  const [nonce, setNonce] = useState(0)
  const key = `${JSON.stringify(deps)}#${nonce}`
  const [snapshot, setSnapshot] = useState<Snapshot<T>>({ key: '' })
  const latest = useRef({ load, key })
  useEffect(() => {
    latest.current = { load, key }
  })
  /** Bumped by every fetch, so an older answer never replaces a newer one. */
  const sequence = useRef(0)
  const topicList = topics.join('\n')
  /** The topics the subscription effect below follows now, or '' when none. */
  const followed = useRef('')

  useEffect(() => {
    let cancelled = false
    const read = () => {
      const mine = ++sequence.current
      load()
        .then((data) => {
          if (!cancelled && sequence.current === mine) setSnapshot({ key, data })
        })
        .catch((cause: unknown) => {
          if (!cancelled && sequence.current === mine) {
            setSnapshot({ key, error: cause instanceof Error ? cause : new Error(String(cause)) })
          }
        })
    }
    // This effect runs before the subscription's, so a list it has not followed yet is
    // about to be subscribed, and with the socket open its resync reads (#1039).
    const resyncComing = topicList !== '' && followed.current !== topicList && getRealtime().willResync
    if (!resyncComing) {
      read()
      return () => {
        cancelled = true
      }
    }
    // Should the signal not come (a topic the server refused, the socket lost before it
    // answered), read anyway. Any read started meanwhile is the signal's.
    const before = sequence.current
    const fallback = setTimeout(() => {
      if (sequence.current === before) read()
    }, RESYNC_WAIT_MS)
    return () => {
      cancelled = true
      clearTimeout(fallback)
    }
    // `load` is a fresh closure every render; `key` encodes its real inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const queued = useRef(false)
  const accepting = useRef<((data: T) => boolean) | undefined>(undefined)
  const failing = useRef<(() => void)[]>([])
  const refresh = useCallback((accept?: (data: T) => boolean, failed?: () => void) => {
    // Signals that arrive together are read once, deciding by the latest `accept`; each
    // caller's `failed` is kept, so none misses the failure of the read it was merged into.
    accepting.current = accept
    if (failed) failing.current.push(failed)
    if (queued.current) return
    queued.current = true
    queueMicrotask(() => {
      queued.current = false
      const { load: current, key: at } = latest.current
      const decide = accepting.current
      const fail = failing.current
      failing.current = []
      const mine = ++sequence.current
      current().then(
        (data) => {
          if (sequence.current !== mine) return
          if (decide && !decide(data)) return
          setSnapshot({ key: at, data })
        },
        (cause: unknown) => {
          if (sequence.current !== mine) return
          const error = cause instanceof Error ? cause : new Error(String(cause))
          // A failed background read keeps what is on screen: the next change reads
          // again. Only a key with nothing to show yet shows the error.
          setSnapshot((s) => (s.key === at && s.data !== undefined ? s : { key: at, error }))
          for (const f of fail) f()
        },
      )
    })
  }, [])

  useEffect(() => {
    if (!topicList) return
    const realtime = getRealtime()
    const stops = topicList.split('\n').map((topic) => realtime.subscribe(topic, () => refresh()))
    followed.current = topicList
    return () => {
      followed.current = ''
      for (const stop of stops) stop()
    }
  }, [topicList, refresh])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  // The key the snapshot holds, so `supersede` never cancels another key's first load.
  const held = useRef(snapshot.key)
  useEffect(() => {
    held.current = snapshot.key
  })
  const setData = useCallback((data: T, options?: { supersede?: boolean }) => {
    if (options?.supersede && held.current === latest.current.key) ++sequence.current
    setSnapshot((s) => ({ ...s, data, error: undefined }))
  }, [])

  const settled = snapshot.key === key
  return {
    data: settled ? snapshot.data : undefined,
    error: settled ? snapshot.error : undefined,
    loading: !settled,
    reload,
    refresh,
    setData,
  }
}
