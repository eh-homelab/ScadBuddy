import { useCallback, useEffect, useRef, useState } from 'react'
import { api, mayHaveRun } from '../../api/client'
import type { OrderItemDetail, SliceQuoteRequest } from '../../api/types'
import { refusalText } from './refusal'

/** A slice quote of one item, while it runs and after it fails. */
export interface SliceQuoteRun {
  /** `Date.now()` as it started. */
  startedAt: number
  /** Set once it failed; the run is over. */
  error: string | null
  /**
   * Set when the page stopped following it but it may still be running (the client's
   * follow ended first, or its answer never arrived): it ends when the item shows a quote
   * from a slice that `known` does not name, which the order's own refresh brings.
   */
  detached?: boolean
  /** The item's quote ids as it started. */
  known: readonly string[]
}

export type SliceQuoteRuns = ReadonlyMap<string, SliceQuoteRun>

/** An operation's activity row id is `operation:<id>`; a quote's subject `order_item:<id>`. */
const OPERATION = 'operation:'
const SUBJECT = 'order_item:'

/**
 * #4693 — the order page's slice quotes, one per item. A quote is the `order_item_quote`
 * operation and runs for minutes (its `QUOTE_TIMEOUT` is 30), so it is followed here,
 * on the page, rather than in the print dialog that chose it: the modal dialog waiting on
 * it held the whole page under its scroll lock until it finished. The dialog hands its
 * request to `start`, waits only for the server to accept it (a refusal from its check
 * rejects, so the dialog keeps its choices), and closes; the item's row and drawer show
 * the run. `items` are the order's as the page last read them, which end a detached run.
 * A quote still slicing when the page loads (after a reload, or on coming back to the
 * order) is found through the activity index and followed by its operation id, so it shows
 * as running and a second press cannot start a second one (#4736).
 */
export function useSliceQuotes(orderId: string, items: readonly OrderItemDetail[] | undefined, onQuoted: () => void) {
  const [runs, setRuns] = useState<Map<string, SliceQuoteRun>>(() => new Map())
  const current = useRef(orderId)
  // The follows of this order's quotes, ended with the page: their polls of
  // `GET /operations/{id}` would otherwise run on for minutes after it is gone.
  const follows = useRef(new AbortController())
  const quoted = useRef(onQuoted)
  // What the recovery below reads without running again on each change.
  const seen = useRef({ items, runs })
  // The operations a recovery followed on this order, running or ended: an activity row can
  // stay `running` after its operation ended (its write was swallowed until the repair),
  // and following it again on each reload would reload the order without end.
  const followed = useRef(new Set<string>())
  useEffect(() => {
    quoted.current = onQuoted
    seen.current = { items, runs }
  })
  useEffect(() => {
    current.current = orderId
    const controller = new AbortController()
    follows.current = controller
    // Another order's runs are not this one's.
    setRuns(new Map())
    followed.current = new Set()
    return () => {
      current.current = ''
      controller.abort()
    }
  }, [orderId])

  const update = useCallback(
    (forOrder: string, itemId: string, run: SliceQuoteRun | null) => {
      if (current.current !== forOrder) return
      setRuns((before) => {
        const next = new Map(before)
        if (run) next.set(itemId, run)
        else next.delete(itemId)
        return next
      })
    },
    [],
  )

  const start = useCallback(
    (itemId: string, body: SliceQuoteRequest, known: readonly string[]) =>
      new Promise<void>((accepted, refused) => {
        const run = { startedAt: Date.now(), error: null, known }
        let running = false
        update(orderId, itemId, run)
        api
          .quoteItem(
            orderId,
            itemId,
            body,
            (operation) => {
              running = true
              // Its own follow ends it; a stale activity row of it is not followed again.
              followed.current.add(operation.id)
              accepted()
            },
            follows.current.signal,
          )
          .then(
            () => {
              update(orderId, itemId, null)
              if (current.current === orderId) quoted.current()
              accepted()
            },
            (cause: unknown) => {
              if (mayHaveRun(cause)) {
                // Not a failure while it may still be running: a second press would start a second.
                update(orderId, itemId, { ...run, detached: true })
                accepted()
              } else if (running) {
                update(orderId, itemId, { ...run, error: refusalText(cause) })
              } else {
                // Refused before it ran: the dialog that asked says why.
                update(orderId, itemId, null)
                refused(cause)
              }
            },
          )
      }),
    [orderId, update],
  )

  const dismiss = useCallback((itemId: string) => update(orderId, itemId, null), [orderId, update])

  // Each time this order's items are read (the page's first read, and every reload, so a
  // quote started in another tab shows too): the quotes of them still running, which this
  // page does not follow yet, followed as `start` follows its own. Best effort: without the
  // activity index the item shows idle until its quote lands, as before.
  const itemsOf = items !== undefined && items.length > 0 && items.every((i) => i.order_id === orderId) ? orderId : null
  useEffect(() => {
    if (itemsOf === null) return
    const forOrder = itemsOf
    const signal = follows.current.signal
    api.runningQuotes().then(
      (page) => {
        for (const row of page.items) {
          if (current.current !== forOrder) return
          const operationId = row.id.startsWith(OPERATION) ? row.id.slice(OPERATION.length) : ''
          const itemId = row.subject.startsWith(SUBJECT) ? row.subject.slice(SUBJECT.length) : ''
          const item = seen.current.items?.find((i) => i.id === itemId)
          if (!operationId || !item || seen.current.runs.has(itemId) || followed.current.has(operationId)) continue
          followed.current.add(operationId)
          const started = row.created_at ? Date.parse(row.created_at) : NaN
          const run: SliceQuoteRun = { startedAt: Number.isNaN(started) ? Date.now() : started, error: null, known: item.quotes.map((q) => q.id) }
          // Shown only once its operation reads as running: one that already ended behind a
          // stale row is no run of this page's, and its old failure no error to show.
          let shown = false
          api
            .followQuote(operationId, signal, () => {
              shown = true
              update(forOrder, itemId, run)
            })
            .then(
              () => {
                if (!shown) return
                update(forOrder, itemId, null)
                if (current.current === forOrder) quoted.current()
              },
              (cause: unknown) => {
                if (shown) update(forOrder, itemId, mayHaveRun(cause) ? { ...run, detached: true } : { ...run, error: refusalText(cause) })
              },
            )
        }
      },
      (cause: unknown) => console.debug('could not read the running slice quotes; their items show idle', cause),
    )
  }, [itemsOf, items, update])

  // A detached run ends when its quote shows on the item: a slice quote it did not have
  // (one typed by hand meanwhile is not it).
  useEffect(() => {
    if (!items) return
    for (const [itemId, run] of runs) {
      if (!run.detached) continue
      const item = items.find((i) => i.id === itemId)
      if (!item || item.quotes.some((q) => q.source === 'slice' && !run.known.includes(q.id))) dismiss(itemId)
    }
  }, [items, runs, dismiss])

  return { runs: runs as SliceQuoteRuns, start, dismiss }
}
