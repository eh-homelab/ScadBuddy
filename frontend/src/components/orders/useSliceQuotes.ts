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
   * that `known` does not name, which the order's own refresh brings.
   */
  detached?: boolean
  /** The item's quote ids as it started. */
  known: readonly string[]
}

export type SliceQuoteRuns = ReadonlyMap<string, SliceQuoteRun>

/**
 * #4693 — the order page's slice quotes, one per item. A quote is the `order_item_quote`
 * operation and runs for minutes (its `QUOTE_TIMEOUT` is 30), so it is followed here,
 * on the page, rather than in the print dialog that chose it: the modal dialog waiting on
 * it held the whole page under its scroll lock until it finished. The dialog hands its
 * request to `start` and closes; the item's row and drawer show the run. `items` are the
 * order's as the page last read them, which end a detached run.
 */
export function useSliceQuotes(orderId: string, items: readonly OrderItemDetail[] | undefined, onQuoted: () => void) {
  const [runs, setRuns] = useState<Map<string, SliceQuoteRun>>(() => new Map())
  const current = useRef(orderId)
  const quoted = useRef(onQuoted)
  useEffect(() => {
    quoted.current = onQuoted
  })
  useEffect(() => {
    current.current = orderId
    // Another order's runs are not this one's.
    setRuns(new Map())
    return () => {
      current.current = ''
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
    (itemId: string, body: SliceQuoteRequest, known: readonly string[]) => {
      const run = { startedAt: Date.now(), error: null, known }
      update(orderId, itemId, run)
      api.quoteItem(orderId, itemId, body).then(
        () => {
          update(orderId, itemId, null)
          if (current.current === orderId) quoted.current()
        },
        (cause: unknown) =>
          // Not a failure while it may still be running: a second press would start a second.
          update(orderId, itemId, mayHaveRun(cause) ? { ...run, detached: true } : { ...run, error: refusalText(cause) }),
      )
    },
    [orderId, update],
  )

  const dismiss = useCallback((itemId: string) => update(orderId, itemId, null), [orderId, update])

  // A detached run ends when its quote shows on the item.
  useEffect(() => {
    if (!items) return
    for (const [itemId, run] of runs) {
      if (!run.detached) continue
      const item = items.find((i) => i.id === itemId)
      if (!item || item.quotes.some((q) => !run.known.includes(q.id))) dismiss(itemId)
    }
  }, [items, runs, dismiss])

  return { runs: runs as SliceQuoteRuns, start, dismiss }
}
