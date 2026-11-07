import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, mayHaveRun } from '../../api/client'
import type { ActivityPage, OrderItemDetail, SliceQuoteRequest } from '../../api/types'
import { refusalText } from './refusal'

/** A slice quote of one item, while it runs and after it fails. */
export interface SliceQuoteRun {
  /** `Date.now()` as it started. */
  startedAt: number
  /** Set once it failed; the run is over. */
  error: string | null
  /** #5554 — `Date.now()` as it failed, or its operation's `finished_at`. */
  finishedAt?: number
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

/** #5585 — a slice quote's failure as a follow read it: its operation, times and error. */
interface Failure {
  operationId: string
  startedAt: number
  finishedAt: number
  error: string
}

/**
 * #5585 — what the follow of a `running` activity row found: its operation still running,
 * or ended (the row was stale), with its failure when it failed for a reason it names.
 */
type Probe = { running: true } | { running: false; failure?: Failure }

/** An operation's activity row id is `operation:<id>`; a quote's subject `order_item:<id>`. */
const OPERATION = 'operation:'
const SUBJECT = 'order_item:'

/** An activity row's operation id and item id, empty when it names none. */
function idsOf(row: ActivityPage['items'][number]): { operationId: string; itemId: string } {
  return {
    operationId: row.id.startsWith(OPERATION) ? row.id.slice(OPERATION.length) : '',
    itemId: row.subject.startsWith(SUBJECT) ? row.subject.slice(SUBJECT.length) : '',
  }
}

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
  // #5585 — by operation id, what the follow of a `running` row found. A reload while the
  // first read is still out waits on the same answer.
  const probes = useRef(new Map<string, Promise<Probe>>())
  // #5585 — the operations whose failure, carried by a probe, was shown: a dismissed one
  // does not come back on the next reload, as `followed` keeps a failed row's from coming back.
  const shownFailures = useRef(new Set<string>())
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
    probes.current = new Map()
    shownFailures.current = new Set()
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
                update(orderId, itemId, { ...run, error: refusalText(cause), finishedAt: Date.now() })
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
  // activity index the item shows idle until its quote lands, as before. #5554 — then each
  // item's newest failed quote that no quote made since answers, with its start, end and
  // error: one made over MCP or in another tab, which the page did not start. #5585 — the
  // newest is the later of the item's newest failed row and the failure a stale `running`
  // row's follow found.
  const itemsOf = items !== undefined && items.length > 0 && items.every((i) => i.order_id === orderId) ? orderId : null
  useEffect(() => {
    if (itemsOf === null || !items) return
    const forOrder = itemsOf
    const signal = follows.current.signal
    // #5585 — per item, the probes of its `running` rows: this run's follows and, through
    // `probes`, those an earlier run started whose answer may still be out. A row can stay
    // `running` after its operation ended; only a real run hides a failure.
    const itemProbes = new Map<string, Promise<Probe>[]>()
    const probeOf = (itemId: string, probe: Promise<Probe>) => itemProbes.set(itemId, [...(itemProbes.get(itemId) ?? []), probe])
    const followRunning = (page: ActivityPage) => {
      for (const row of page.items) {
        if (current.current !== forOrder) return
        const { operationId, itemId } = idsOf(row)
        const item = seen.current.items?.find((i) => i.id === itemId)
        if (!operationId || !item) continue
        const earlier = probes.current.get(operationId)
        if (earlier) {
          probeOf(itemId, earlier)
          continue
        }
        if (seen.current.runs.has(itemId) || followed.current.has(operationId)) continue
        followed.current.add(operationId)
        const started = row.created_at ? Date.parse(row.created_at) : NaN
        const run: SliceQuoteRun = { startedAt: Number.isNaN(started) ? Date.now() : started, error: null, known: item.quotes.map((q) => q.id) }
        // Shown only once its operation reads as running: one that already ended behind a
        // stale row is no run of this page's. Its failure, if it failed, goes to the probe,
        // and shows when it is the item's newest.
        let shown = false
        let told!: (found: Probe) => void
        const probe = new Promise<Probe>((resolve) => (told = resolve))
        probes.current.set(operationId, probe)
        probeOf(itemId, probe)
        const settle = (found: Probe) => {
          told(found)
          probes.current.set(operationId, Promise.resolve(found))
        }
        api
          .followQuote(operationId, signal, () => {
            shown = true
            told({ running: true })
            update(forOrder, itemId, run)
          })
          .then(
            () => {
              settle({ running: false })
              if (!shown) return
              update(forOrder, itemId, null)
              if (current.current === forOrder) quoted.current()
            },
            (cause: unknown) => {
              // A failure it names, with a start to order it by; not a lost answer, nor a
              // pruned operation's 404.
              const failed = !mayHaveRun(cause) && !(cause instanceof ApiError && cause.status === 404) && !Number.isNaN(started)
              const ended = row.finished_at ?? row.updated_at
              const finishedAt = shown ? Date.now() : ended && !Number.isNaN(Date.parse(ended)) ? Date.parse(ended) : started
              if (shown && failed) shownFailures.current.add(operationId)
              settle({ running: false, failure: failed ? { operationId, startedAt: started, finishedAt, error: refusalText(cause) } : undefined })
              if (shown) update(forOrder, itemId, mayHaveRun(cause) ? { ...run, detached: true } : { ...run, error: refusalText(cause), finishedAt: Date.now() })
            },
          )
      }
    }
    const showFailure = (itemId: string, row: ActivityPage['items'][number] | undefined, stale: Failure | undefined) => {
      if (current.current !== forOrder) return
      const item = seen.current.items?.find((i) => i.id === itemId)
      if (!item || seen.current.runs.has(itemId)) return
      // The item's newest failed row (the rows come newest first, one to a page).
      const operationId = row ? idsOf(row).operationId : ''
      const started = row?.created_at ? Date.parse(row.created_at) : NaN
      const answered = (at: number) => item.quotes.some((q) => Date.parse(q.created_at) >= at)
      // #5585 — a stale `running` row's failure that started later is the item's newest.
      if (stale && (!operationId || Number.isNaN(started) || stale.startedAt >= started)) {
        if (shownFailures.current.has(stale.operationId) || answered(stale.startedAt)) return
        shownFailures.current.add(stale.operationId)
        update(forOrder, itemId, { startedAt: stale.startedAt, finishedAt: stale.finishedAt, error: stale.error, known: item.quotes.map((q) => q.id) })
        return
      }
      if (!row || !operationId || Number.isNaN(started)) return
      const ended = row.finished_at ? Date.parse(row.finished_at) : NaN
      const finished = Number.isNaN(ended) ? started : ended
      if (followed.current.has(operationId)) return
      // A quote made since it started answers it: the failure is no longer the latest.
      if (answered(started)) return
      followed.current.add(operationId)
      // The row carries no error: its operation's read does (a failed one throws it).
      api.followQuote(operationId, signal).then(
        () => undefined,
        (cause: unknown) => {
          if (mayHaveRun(cause) || seen.current.runs.has(itemId)) return
          // #5585 — its operation was pruned (`operation_retention_seconds` is shorter than
          // the activity's TTL): no error is left to show, and "not found" is not one.
          if (cause instanceof ApiError && cause.status === 404) return
          update(forOrder, itemId, { startedAt: started, finishedAt: finished, error: refusalText(cause), known: item.quotes.map((q) => q.id) })
        },
      )
    }
    const running = api.runningQuotes().then(followRunning, (cause: unknown) => {
      console.debug('could not read the running slice quotes; their items show idle', cause)
    })
    // #5585 — one read per item, filtered by its subject: no other order's failures push
    // its newest off the page. An item whose quote runs, or that shows a run already, is
    // not read: its failure would not show.
    for (const item of items) {
      void (async () => {
        await running
        const found = await Promise.all(itemProbes.get(item.id) ?? [])
        if (found.some((p) => p.running) || seen.current.runs.has(item.id) || current.current !== forOrder) return
        const stale = found
          .map((p) => (p.running ? undefined : p.failure))
          .reduce<Failure | undefined>((a, b) => (!a || (b && b.startedAt > a.startedAt) ? b : a), undefined)
        let row: ActivityPage['items'][number] | undefined
        try {
          row = (await api.failedQuotes(item.id, item.created_at)).items[0]
        } catch (cause) {
          console.debug('could not read the failed slice quotes; the item shows none', cause)
        }
        showFailure(item.id, row, stale)
      })()
    }
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
