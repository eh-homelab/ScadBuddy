import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import type { SliceQuoteRequest } from '../../api/types'
import { refusalText } from './refusal'

/** A slice quote of one item, while it runs and after it fails. */
export interface SliceQuoteRun {
  /** `Date.now()` as it started. */
  startedAt: number
  /** Set once it failed; the run is over. */
  error: string | null
}

export type SliceQuoteRuns = ReadonlyMap<string, SliceQuoteRun>

/**
 * #4693 — the order page's slice quotes, one per item. A quote is the `order_item_quote`
 * operation and runs for minutes (its `QUOTE_TIMEOUT` is 30), so it is followed here,
 * on the page, rather than in the print dialog that chose it: the modal dialog waiting on
 * it held the whole page under its scroll lock until it finished. The dialog hands its
 * request to `start` and closes; the item's row and drawer show the run.
 */
export function useSliceQuotes(orderId: string, onQuoted: () => void) {
  const [runs, setRuns] = useState<Map<string, SliceQuoteRun>>(() => new Map())
  const live = useRef(true)
  const quoted = useRef(onQuoted)
  useEffect(() => {
    quoted.current = onQuoted
  })
  useEffect(() => {
    live.current = true
    return () => {
      live.current = false
    }
  }, [])

  const update = useCallback((itemId: string, run: SliceQuoteRun | null) => {
    if (!live.current) return
    setRuns((before) => {
      const next = new Map(before)
      if (run) next.set(itemId, run)
      else next.delete(itemId)
      return next
    })
  }, [])

  const start = useCallback(
    (itemId: string, body: SliceQuoteRequest) => {
      const startedAt = Date.now()
      update(itemId, { startedAt, error: null })
      api.quoteItem(orderId, itemId, body).then(
        () => {
          update(itemId, null)
          if (live.current) quoted.current()
        },
        (cause: unknown) => update(itemId, { startedAt, error: refusalText(cause) }),
      )
    },
    [orderId, update],
  )

  const dismiss = useCallback((itemId: string) => update(itemId, null), [update])

  return { runs: runs as SliceQuoteRuns, start, dismiss }
}
