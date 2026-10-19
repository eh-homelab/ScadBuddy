import { useCallback, useEffect, useRef, useState } from 'react'
import { getRealtime, readAgain, type Confirmation, type ReadRecord, type RealtimeSignal } from './realtime'
import { inReadScope, ReadScope } from './readCursor'

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
  /**
   * #1039 — for a caller that follows a topic itself: whether that subscription's
   * confirmation means the current key's first read must be made again (`readAgain`).
   */
  staleFor: (topic: string, confirmation: Confirmation) => Promise<boolean>
}

/** How long a first read left to the subscription's resync waits for it (#1039). */
export const RESYNC_WAIT_MS = 500
/** The least time between two reads a hook's topic signals start (#4149). */
export const SIGNAL_READ_MIN_MS = 250
/**
 * How long a signal's read waits on the read in flight before it starts anyway (#4165):
 * a stalled fetch (a half-open connection, a proxy holding the request) must not leave
 * the view deaf to its topics until it remounts.
 */
export const SIGNAL_READ_STALL_MS = 5_000

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
 * starts at once, and a change made between it and the server's `subscribed` would
 * otherwise go unseen. So the confirmation is checked against what that read saw
 * (`lib/readCursor.ts`): it reads again only when the read did not see the server's
 * last event and the server says something since touched the topic, or cannot say.
 */
export function useAsync<T>(
  load: () => Promise<T>,
  deps: readonly unknown[],
  topics: readonly string[] = [],
  /** Which signals on `topics` are news for this read; every one when left out. */
  news?: (signal: RealtimeSignal) => boolean,
): AsyncState<T> {
  const isNews = useRef(news)
  useEffect(() => {
    isNews.current = news
  })
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
  /** The current key's newest read that is shown: what it saw (#1039). */
  const shownRead = useRef<ReadRecord | undefined>(undefined)

  useEffect(() => {
    let cancelled = false
    shownRead.current = undefined
    const read = () => {
      const mine = ++sequence.current
      const scope = new ReadScope()
      let shown: (ok: boolean) => void = () => {}
      shownRead.current = { scope, shown: new Promise((resolve) => (shown = resolve)) }
      inReadScope(scope, load)
        .then((data) => {
          const current = !cancelled && sequence.current === mine
          if (current) setSnapshot({ key, data })
          shown(current)
        })
        .catch((cause: unknown) => {
          if (!cancelled && sequence.current === mine) {
            setSnapshot({ key, error: cause instanceof Error ? cause : new Error(String(cause)) })
          }
          shown(false)
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

  // A background read can land after unmount, when a test's environment may already be
  // torn down: it then touches nothing, neither the state nor a caller's callbacks.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const queued = useRef(false)
  const accepting = useRef<((data: T) => boolean) | undefined>(undefined)
  const failing = useRef<(() => void)[]>([])
  /** Settles when the newest background read does, answered or not (#4149). */
  const reading = useRef<Promise<void>>(Promise.resolve())
  const refresh = useCallback((accept?: (data: T) => boolean, failed?: () => void) => {
    // Signals that arrive together are read once, deciding by the latest `accept`; each
    // caller's `failed` is kept, so none misses the failure of the read it was merged into.
    accepting.current = accept
    if (failed) failing.current.push(failed)
    if (queued.current) return
    queued.current = true
    let settle: () => void = () => {}
    reading.current = new Promise((resolve) => (settle = resolve))
    queueMicrotask(() => {
      queued.current = false
      if (!mounted.current) {
        // Gone: the callers' closures are let go now, not when the hook is collected.
        accepting.current = undefined
        failing.current = []
        settle()
        return
      }
      const { load: current, key: at } = latest.current
      const decide = accepting.current
      const fail = failing.current
      failing.current = []
      const mine = ++sequence.current
      const scope = new ReadScope()
      const answered = inReadScope(scope, current)
      void answered.then(settle, settle)
      answered.then(
        (data) => {
          if (!mounted.current || sequence.current !== mine) return
          if (decide && !decide(data)) return
          setSnapshot({ key: at, data })
          // A later confirmation (a reconnect's) asks from what is on screen now.
          if (latest.current.key === at) shownRead.current = { scope, shown: Promise.resolve(true) }
        },
        (cause: unknown) => {
          if (!mounted.current || sequence.current !== mine) return
          const error = cause instanceof Error ? cause : new Error(String(cause))
          // A failed background read keeps what is on screen: the next change reads
          // again. Only a key with nothing to show yet shows the error. The very same
          // snapshot, `data` and all: LibraryUpgrade's re-list alert relies on it (#3839).
          setSnapshot((s) => (s.key === at && s.data !== undefined ? s : { key: at, error }))
          for (const f of fail) f()
        },
      )
    })
  }, [])

  /**
   * #4149 — a topic's signal reads through here, never straight through `refresh`: one
   * such read in flight and one waiting behind it, which every signal meanwhile joins,
   * and each started at least `SIGNAL_READ_MIN_MS` after the last. However often a topic
   * is signalled (or a bug signals it), this hook reads it a few times a second at most.
   * A read in flight that stalls is waited on for `SIGNAL_READ_STALL_MS` only (#4165):
   * then the next starts beside it, so a stalled fetch adds one read per that interval.
   */
  const signals = useRef<{
    waiting: boolean
    last: number
    timer?: ReturnType<typeof setTimeout>
    /** The read in flight's stall timer, and what ends its wait at once. */
    stalled?: { timer: ReturnType<typeof setTimeout>; end: () => void }
    /** Bumped when the effects are torn down: a wait from before that ends doing nothing. */
    generation: number
  }>({ waiting: false, last: -Infinity, generation: 0 })
  /** Ends the read in flight's wait now, once: its timer cleared, its promise settled. */
  const endStall = useCallback(() => {
    const state = signals.current
    clearTimeout(state.stalled?.timer)
    state.stalled?.end()
    state.stalled = undefined
  }, [])
  const signalled = useCallback(() => {
    const state = signals.current
    if (state.waiting) return
    state.waiting = true
    const mine = state.generation
    const start = () => {
      state.timer = undefined
      const stall = new Promise<void>(
        (end) => (state.stalled = { timer: setTimeout(end, SIGNAL_READ_STALL_MS), end }),
      )
      void Promise.race([reading.current, stall]).then(() => {
        // Torn down and replayed in place meanwhile: that replay's own signals read.
        if (state.generation !== mine) return
        endStall()
        if (!mounted.current) return
        state.waiting = false
        state.last = Date.now()
        refresh()
      })
    }
    const wait = state.last + SIGNAL_READ_MIN_MS - Date.now()
    if (wait > 0) state.timer = setTimeout(start, wait)
    else start()
  }, [refresh, endStall])
  useEffect(() => {
    const state = signals.current
    return () => {
      clearTimeout(state.timer)
      // A read left waiting here never runs: an effect replayed in place (StrictMode,
      // Fast Refresh, <Activity>) must hear its topics again, and a wait from before
      // must not start a read beside the replay's own.
      state.generation++
      // Ended, not only its timer cleared: a read that never settles would otherwise
      // hold the wait (#4318 review). The generation above makes it do nothing.
      endStall()
      state.timer = undefined
      state.waiting = false
      state.last = -Infinity
    }
  }, [endStall])

  const staleFor = useCallback(
    (topic: string, confirmation: Confirmation) => readAgain(shownRead.current, topic, confirmation),
    [],
  )
  /** A topic's subscription is live: read again unless the first read is known current. */
  const confirmed = useCallback(
    (topic: string, confirmation: Confirmation) => {
      void staleFor(topic, confirmation).then((stale) => {
        if (stale) signalled()
      })
    },
    [signalled, staleFor],
  )

  useEffect(() => {
    if (!topicList) return
    const realtime = getRealtime()
    const stops = topicList.split('\n').map((topic) =>
      realtime.subscribe(topic, (signal, confirmation) => {
        if (isNews.current && !isNews.current(signal)) return
        if (confirmation) confirmed(topic, confirmation)
        else signalled()
      }),
    )
    followed.current = topicList
    return () => {
      followed.current = ''
      for (const stop of stops) stop()
    }
  }, [topicList, signalled, confirmed])

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
    staleFor,
  }
}
