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
   * whatever else it must (a "changed elsewhere" banner) instead.
   */
  refresh: (accept?: (data: T) => boolean) => void
  setData: (next: T) => void
}

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

  useEffect(() => {
    let cancelled = false
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
    return () => {
      cancelled = true
    }
    // `load` is a fresh closure every render; `key` encodes its real inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const queued = useRef(false)
  const accepting = useRef<((data: T) => boolean) | undefined>(undefined)
  const refresh = useCallback((accept?: (data: T) => boolean) => {
    // Signals that arrive together are read once, deciding by the latest `accept`.
    accepting.current = accept
    if (queued.current) return
    queued.current = true
    queueMicrotask(() => {
      queued.current = false
      const { load: current, key: at } = latest.current
      const decide = accepting.current
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
        },
      )
    })
  }, [])

  const topicList = topics.join('\n')
  useEffect(() => {
    if (!topicList) return
    const realtime = getRealtime()
    const stops = topicList.split('\n').map((topic) => realtime.subscribe(topic, () => refresh()))
    return () => {
      for (const stop of stops) stop()
    }
  }, [topicList, refresh])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  const setData = useCallback((data: T) => setSnapshot((s) => ({ ...s, data, error: undefined })), [])

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
