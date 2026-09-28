import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { useLatest } from '../../lib/useLatest'
import { api } from '../../api/client'
import type { Output, PrintSummary } from '../../api/types'
import {
  apiFilters,
  clearPrintFilters,
  isFiltered,
  parsePrintsQuery,
  readStoredPrintsView,
  storePrintsView,
  toPrintsParams,
  type PrintFilters,
  type PrintsQuery,
} from '../../lib/printsQuery'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { MediaLightbox } from '../media/MediaLightbox'
import type { Slide } from '../media/slides'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'
import { PrintFilterBar } from './PrintFilterBar'
import { PrintItem } from './PrintItem'
import { coverSlides, printSlides } from './prints'

const PAGE_SIZE = 24

interface Pages {
  /** The filters (and reload) these pages answer; a different one is still loading. */
  key: string
  items: PrintSummary[]
  next: string | null
  error?: Error
  /** The next page's fetch: in flight, or how it failed. */
  more?: 'loading' | Error
}

function asError(cause: unknown): Error {
  return cause instanceof Error ? cause : new Error(String(cause))
}

/**
 * The print list for `filters`, a page at a time (cursor paging, plan §2.4). The first
 * page loads whenever the filters change; `loadMore` appends the next.
 */
function usePrintPages(filters: PrintFilters) {
  const [nonce, setNonce] = useState(0)
  const key = `${JSON.stringify(filters)}#${nonce}`
  const [pages, setPages] = useState<Pages>({ key: '', items: [], next: null })

  useEffect(() => {
    let cancelled = false
    api.listPrints(filters, { limit: PAGE_SIZE }).then(
      (page) => {
        if (!cancelled) setPages({ key, items: page.items, next: page.next_cursor })
      },
      (cause: unknown) => {
        if (!cancelled) setPages({ key, items: [], next: null, error: asError(cause) })
      },
    )
    return () => {
      cancelled = true
    }
    // `key` covers the filters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const current = pages.key === key
  const latest = useLatest({ pages, key, filters, current })
  const loadMore = useCallback(() => {
    const { pages, key, filters, current } = latest.current
    if (!current || pages.next === null || pages.more === 'loading') return
    const cursor = pages.next
    setPages({ ...pages, more: 'loading' })
    const settle = (next: (was: Pages) => Pages) =>
      setPages((was) => (was.key === key && was.next === cursor ? next(was) : was))
    api.listPrints(filters, { cursor, limit: PAGE_SIZE }).then(
      (page) =>
        settle((was) => {
          const seen = new Set(was.items.map((item) => item.archive_id))
          return {
            key,
            items: [...was.items, ...page.items.filter((item) => !seen.has(item.archive_id))],
            next: page.next_cursor,
          }
        }),
      (cause: unknown) => settle((was) => ({ ...was, more: asError(cause) })),
    )
  }, [latest])

  return {
    loading: !current,
    items: current ? pages.items : [],
    next: current ? pages.next : null,
    error: current ? pages.error : undefined,
    more: current ? pages.more : undefined,
    reload: () => setNonce((n) => n + 1),
    loadMore,
  }
}

/** An output that went to Bambuddy's queue, by either route (#89). */
function wasSent(output: Output): boolean {
  return Boolean(output.queue_item_id || output.pipeline_run_id || (output.plates ?? []).length > 0)
}

/**
 * #310 — the print history: the global page, or with `fixedSlug` a template's Prints
 * tab. Filters and the view live in the URL (plan §2.7).
 */
export function PrintHistory({ fixedSlug }: { fixedSlug?: string }) {
  const [params, setParams] = useSearchParams()
  const parsed = useMemo(() => parsePrintsQuery(params), [params])
  // The URL's view wins; without one, the one this browser last chose.
  const view = params.has('view') ? parsed.view : (readStoredPrintsView() ?? parsed.view)
  const query: PrintsQuery = { ...parsed, slug: fixedSlug ? '' : parsed.slug, view }
  const filters = useMemo(
    () => apiFilters(query, fixedSlug),
    // Each field on its own: `query` is rebuilt every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [fixedSlug, query.slug, query.status, query.printer, query.from, query.to, query.q],
  )
  const pages = usePrintPages(filters)
  const templates = useAsync(() => (fixedSlug ? Promise.resolve([]) : api.listModels()), [fixedSlug], ['models'])
  const [lightbox, setLightbox] = useState<{ archiveId: number; slides: Slide[] } | null>(null)

  function setQuery(next: PrintsQuery, options?: { replace?: boolean }) {
    if (next.view !== query.view) storePrintsView(next.view)
    const out = toPrintsParams(fixedSlug ? { ...next, slug: '' } : next)
    // Cards is the default and left out, unless a stored List would then win over it.
    if (next.view === 'cards' && readStoredPrintsView() === 'list') out.set('view', 'cards')
    setParams(out, options)
  }

  function openMedia(print: PrintSummary) {
    const archiveId = print.archive_id
    setLightbox({ archiveId, slides: coverSlides(print) })
    api.getPrint(archiveId).then(
      (detail) =>
        setLightbox((open) => (open?.archiveId === archiveId ? { archiveId, slides: printSlides(detail) } : open)),
      // The cover stays: it is still this print's image.
      () => undefined,
    )
  }

  // Each printer seen, by id, named by the first print that has its name.
  const printers = useMemo(() => {
    const seen = new Map<number, string | null>()
    for (const { printer_id: id, printer_name: name } of pages.items) {
      if (id !== null && !seen.get(id)) seen.set(id, name)
    }
    return seen
  }, [pages.items])
  const templateNames = useMemo(
    () => new Map((templates.data ?? []).map((model) => [model.slug, model.name])),
    [templates.data],
  )
  const filtered = isFiltered(query)

  return (
    <>
      <PrintFilterBar
        query={query}
        onChange={setQuery}
        printers={printers}
        templates={
          fixedSlug ? undefined : (templates.data ?? []).map((model) => ({ slug: model.slug, name: model.name }))
        }
      />

      {fixedSlug && !filtered && !pages.loading && !pages.error && (
        <Waiting slug={fixedSlug} prints={pages.items} complete={pages.next === null} />
      )}

      {pages.loading && (
        <p className="flex items-center gap-2 py-16 text-[13px] text-muted">
          <Spinner /> Loading prints
        </p>
      )}

      {pages.error && (
        <div role="alert" className="rounded-[6px] border border-warn/40 bg-warn/8 p-4">
          <p className="text-[13px] text-warn">Could not load the prints: {pages.error.message}</p>
          <Button size="sm" className="mt-3" onClick={pages.reload}>
            Try again
          </Button>
        </div>
      )}

      {!pages.loading && !pages.error && pages.items.length === 0 && (
        <div className="rounded-[6px] border border-dashed border-line-strong bg-surface p-10 text-center">
          {filtered ? (
            <>
              <h2 className="text-[15px] font-medium">No prints match</h2>
              <p className="mx-auto mt-2 max-w-md text-[13px] text-muted">
                No print fits these filters.
              </p>
              <Button className="mt-4" onClick={() => setQuery(clearPrintFilters(query))}>
                Clear filters
              </Button>
            </>
          ) : (
            <>
              <h2 className="text-[15px] font-medium">No prints yet</h2>
              <p className="mx-auto mt-2 max-w-md text-[13px] text-muted">
                Prints of ScadBuddy outputs show up here once Bambuddy has archived them.
              </p>
            </>
          )}
        </div>
      )}

      {pages.items.length > 0 && (
        <ul
          aria-label="Prints"
          data-view={query.view}
          className={
            query.view === 'cards'
              ? 'grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3'
              : 'flex flex-col gap-2'
          }
        >
          {pages.items.map((print) => (
            <PrintItem
              key={print.archive_id}
              print={print}
              view={query.view}
              templateName={fixedSlug ? undefined : (templateNames.get(print.slug) ?? print.slug)}
              onOpenMedia={openMedia}
            />
          ))}
        </ul>
      )}

      {pages.next !== null && <MorePrints more={pages.more} onLoadMore={pages.loadMore} />}

      <MediaLightbox
        slides={lightbox?.slides ?? []}
        index={lightbox ? 0 : null}
        onClose={() => setLightbox(null)}
      />
    </>
  )
}

/**
 * The next page: loaded on its own as the end of the list scrolls into view, with the
 * button for when it does not (no IntersectionObserver, or a failed load).
 */
function MorePrints({ more, onLoadMore }: { more: Pages['more']; onLoadMore: () => void }) {
  const sentinel = useRef<HTMLDivElement>(null)
  const latest = useLatest(onLoadMore)
  const failed = more instanceof Error

  useEffect(() => {
    const element = sentinel.current
    if (!element || failed || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) latest.current()
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [failed, latest])

  return (
    <div ref={sentinel} className="mt-4 flex flex-col items-center gap-2">
      {failed && (
        <p role="alert" className="text-[12px] text-warn">
          Could not load more prints: {more.message}
        </p>
      )}
      <Button size="sm" onClick={onLoadMore} disabled={more === 'loading'}>
        {more === 'loading' ? <Spinner /> : 'Load more'}
      </Button>
    </div>
  )
}

/**
 * #310 — an output sent to Bambuddy that no print is linked to yet (plan §2.4: it is not
 * a print, so the list does not carry it). While older pages are still unloaded, only
 * outputs newer than the oldest print shown are judged: an older one's print may be on
 * a page not loaded yet.
 */
function Waiting({ slug, prints, complete }: { slug: string; prints: PrintSummary[]; complete: boolean }) {
  const outputs = useAsync(() => api.listOutputs(slug), [slug], [`model:${slug}`])
  const linked = new Set(prints.map((print) => print.output_id))
  const oldest = prints.at(-1)
  const since = complete ? null : Date.parse(oldest?.started_at ?? oldest?.completed_at ?? '')
  const waiting = (outputs.data ?? []).filter(
    (output) =>
      wasSent(output) &&
      !linked.has(output.id) &&
      (since === null || (!Number.isNaN(since) && Date.parse(output.created_at) >= since)),
  )
  if (waiting.length === 0) return null
  return (
    <ul aria-label="Waiting for Bambuddy" className="mb-3 flex flex-col gap-2">
      {waiting.map((output) => (
        <li
          key={output.id}
          className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-[6px] border border-dashed border-line-strong bg-surface px-3 py-2 text-[13px]"
        >
          <span className="text-ink">{output.name ?? output.id.slice(0, 8)}</span>
          <span className="inline-flex items-center gap-1.5 text-[12px] text-muted">
            <Spinner /> Waiting for Bambuddy
          </span>
          <span className="ml-auto text-[12px] text-faint">Generated {timeAgo(output.created_at)}</span>
        </li>
      ))}
    </ul>
  )
}
