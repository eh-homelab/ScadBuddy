import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { AgentToolError } from '../agent/types'
import { touch } from '../agent/highlight'
import { useAgentHandlers } from '../agent/useAgentHandlers'
import { api, ApiError, type ModelFilters } from '../api/client'
import type { ModelSummary } from '../api/types'
import { DuplicatedFrom, DuplicateModelButton } from '../components/DuplicateModelButton'
import { CatalogueFilters } from '../components/CatalogueFilters'
import { ImportDialog } from '../components/ImportDialog'
import { MediaCarousel } from '../components/media/MediaCarousel'
import { MediaLightbox } from '../components/media/MediaLightbox'
import { namedSlides, type Slide } from '../components/media/slides'
import { ModelOrigin } from '../components/ModelOrigin'
import { ModelRow } from '../components/ModelRow'
import { ModelThumbnail } from '../components/ModelThumbnail'
import { UploadDialog } from '../components/UploadDialog'
import { UpstreamBadge } from '../components/UpstreamUpdate'
import { ClipboardPasteIcon, LinkIcon, PlusIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import {
  type CatalogueQuery,
  clearFilters,
  fold,
  parseQuery,
  toParams,
} from '../lib/catalogueQuery'
import { modelPath } from '../lib/deeplink'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/useAsync'

/** #2290 — how many models a page of the catalogue reads. */
export const MODELS_PAGE_SIZE = 24

export function CataloguePage() {
  const [uploadOpen, setUploadOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  // One lightbox for the page: whichever card's media was clicked, at that item.
  const [lightbox, setLightbox] = useState<{
    slug: string
    slides: Slide[]
    index: number
  } | null>(null)
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const query = useMemo(() => parseQuery(params), [params])
  // #278 — the view is always in the URL: one with no (or an unknown) `view` shows
  // Cards and is rewritten to say so, replacing the entry.
  useEffect(() => {
    if (params.get('view') !== query.view) setParams(toParams(query), { replace: true })
  }, [params, query, setParams])

  // #2290 — the server searches, filters, sorts and pages; the page holds only the
  // models it has read so far. A read again (#269: models created, duplicated,
  // renamed or deleted anywhere) reads as many as are shown, so Load more's pages stay.
  const filters: ModelFilters = { q: query.q, tags: query.tags, origin: query.origin, sort: query.sort }
  const filterKey = JSON.stringify(filters)
  const { sort: _sort, ...facetFilters } = filters
  const facetKey = JSON.stringify(facetFilters)
  const loaded = useRef({ key: filterKey, size: MODELS_PAGE_SIZE })
  // The window read in flight: a newer one stops it between chunks (#5292 review).
  const windowRead = useRef<AbortController | undefined>(undefined)
  const models = useAsync(
    () => {
      if (loaded.current.key !== filterKey) loaded.current = { key: filterKey, size: MODELS_PAGE_SIZE }
      windowRead.current?.abort()
      const controller = new AbortController()
      windowRead.current = controller
      return api.listModelsWindow(filters, loaded.current.size, controller.signal)
    },
    [filterKey],
    ['models'],
  )
  const facets = useAsync(() => api.modelFacets(facetFilters), [facetKey], ['models'])
  // What the last answers showed stays on screen while the next read runs, so typing
  // a search neither blanks the list nor unmounts the box being typed in.
  const page = useKept(models.data)
  const counts = useKept(facets.data)
  const shown = page?.items ?? []
  const error = models.error ?? facets.error
  // `key` is the filters the error was for: another search's list does not show it.
  const [more, setMore] = useState<{ loading: boolean; error?: string; key?: string }>({ loading: false })
  const upstreamNames = useUpstreamNames(shown)

  async function loadMore() {
    const last = shown.at(-1)
    if (!page || !last) return
    const key = filterKey
    setMore({ loading: true })
    try {
      const next = await api.listModelsPage(filters, { limit: MODELS_PAGE_SIZE, after: last.slug })
      if (loaded.current.key !== key) return
      const items = [...page.items, ...next.items.filter((m) => !page.items.some((o) => o.slug === m.slug))]
      loaded.current = { key, size: items.length }
      const dropped = models.setData({ items, total: next.total }, { supersede: true })
      setMore({ loading: false })
      // The append dropped a read in flight (a change's), so read the window again.
      // A read that fails leaves the list as it is, and says so.
      if (dropped) models.refresh(undefined, () =>
        setMore({ loading: false, key, error: 'Could not read the models again; the list shown may be out of date.' }),
      )
    } catch (cause) {
      // The last model shown went away since: start again from the first page.
      if (cause instanceof ApiError && cause.status === 409) {
        loaded.current = { key, size: MODELS_PAGE_SIZE }
        setMore({ loading: false })
        models.reload()
        return
      }
      setMore({
        loading: false,
        key,
        error: `Could not load more models: ${cause instanceof Error ? cause.message : String(cause)}`,
      })
    }
  }

  function reload() {
    models.reload()
    facets.reload()
  }

  function setQuery(next: CatalogueQuery, options?: { replace?: boolean }) {
    setParams(toParams(next), options)
  }

  function addTag(tag: string) {
    if (!query.tags.includes(tag)) setQuery({ ...query, tags: [...query.tags, tag] })
  }

  // #254 — the catalogue's browser tools. `search` reads the whole catalogue (the page
  // holds only the pages it has shown, #2290) rather than typing into the search box;
  // the filters on screen (#276) are URL state, which the global `navigate` tool sets
  // (`/?q=…&tag=…`).
  useAgentHandlers(
    'catalogue',
    {
      search: async ({ query }) => {
        const every = await api.listModels().catch((cause: unknown) => {
          throw new AgentToolError('timeout', `The catalogue did not load: ${cause instanceof Error ? cause.message : String(cause)}`)
        })
        return every.filter((model) => matches(model, query)).map(summarise)
      },
      open_model: async ({ slug }) => {
        const model = shown.find((entry) => entry.slug === slug) ?? (await api.getModel(slug).catch(() => undefined))
        if (!model) {
          throw new AgentToolError('invalid_args', `No model "${slug}" in the catalogue; search lists the slugs.`)
        }
        touch(
          [...document.querySelectorAll('[data-model-card]')].find(
            (card) => card.getAttribute('data-model-card') === slug,
          ),
        )
        await navigate(modelPath(slug))
        return { route: modelPath(slug) }
      },
    },
    () => ({ loading: !page, error: error?.message, models: counts?.total, shown: shown.length, matched: page?.total }),
  )

  function added(model: ModelSummary) {
    void navigate(modelPath(model.slug))
  }

  const empty = counts?.total === 0 && page?.total === 0

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        {/* Wraps, so on a phone the actions drop under the intro rather than pushing
            Add model past the screen's edge (#932). */}
        <div className="mb-5 flex flex-wrap items-end justify-between gap-x-4 gap-y-3">
          <div className="min-w-0 flex-[1_1_16rem]">
            <h1 className="text-lg font-semibold tracking-tight">Models</h1>
            <p className="mt-0.5 text-sm text-muted-foreground">
              Pick a model to set its parameters and generate a multi-colour 3MF.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="lg" onClick={() => void navigate('/new')}>
              <ClipboardPasteIcon /> Paste source
            </Button>
            <Button variant="outline" size="lg" onClick={() => setImportOpen(true)}>
              <LinkIcon /> Import from URL
            </Button>
            <Button size="lg" onClick={() => setUploadOpen(true)}>
              <PlusIcon /> Add model
            </Button>
          </div>
        </div>

        {!page && !error && (
          <div role="status" aria-label="Loading models">
            <span className="sr-only">Loading models</span>
            <ul aria-hidden className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {Array.from({ length: 6 }, (_, n) => (
                <li key={n}>
                  <Card className="gap-3 px-3 pt-3">
                    <Skeleton className="aspect-[4/3] w-full" />
                    <Skeleton className="h-4 w-2/3" />
                    <Skeleton className="h-3 w-full" />
                    <Skeleton className="h-3 w-1/2" />
                  </Card>
                </li>
              ))}
            </ul>
          </div>
        )}

        {error && (
          <Card role="alert" className="border-destructive/40 bg-destructive/5 px-4 ring-destructive/30">
            <p className="text-sm text-destructive">Could not load the catalogue: {error.message}</p>
            <Button size="sm" variant="outline" className="w-fit" onClick={reload}>
              Try again
            </Button>
          </Card>
        )}

        {empty && (
          <EmptyState onUpload={() => setUploadOpen(true)} onImport={() => setImportOpen(true)} />
        )}

        {page && counts && !empty && (
          <CatalogueFilters
            query={query}
            onChange={setQuery}
            tags={counts.tags}
            shown={page.total}
            total={counts.total}
          />
        )}

        {page && counts && !empty && page.total === 0 && (
          <NoResults onClear={() => setQuery(clearFilters(query))} />
        )}

        {shown.length > 0 && query.view === 'list' && (
          <ul aria-label="Models" className="flex flex-col gap-2">
            {shown.map((model) => (
              <ModelRow
                key={model.slug}
                model={model}
                upstreamName={model.upstream ? upstreamNames.get(model.upstream.id) : undefined}
                onOpen={(slides, index) => setLightbox({ slug: model.slug, slides, index })}
                onTag={addTag}
              />
            ))}
          </ul>
        )}

        {shown.length > 0 && query.view === 'cards' && (
          <ul aria-label="Models" className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {shown.map((model) => (
              <ModelCard
                key={model.slug}
                model={model}
                upstreamName={model.upstream ? upstreamNames.get(model.upstream.id) : undefined}
                onTag={addTag}
                onOpenMedia={(slides, index) => setLightbox({ slug: model.slug, slides, index })}
              />
            ))}
          </ul>
        )}

        {page && shown.length < page.total && (
          <div className="mt-4 flex flex-col items-center gap-2">
            <p className="text-xs text-muted-foreground">
              Showing {shown.length} of {page.total}
            </p>
            <Button variant="outline" onClick={() => void loadMore()} disabled={more.loading}>
              {more.loading ? 'Loading…' : 'Load more'}
            </Button>
          </div>
        )}

        {more.error && more.key === filterKey && (
          <p role="alert" className="mt-2 text-center text-sm text-destructive">
            {more.error}
          </p>
        )}
      </div>

      <MediaLightbox
        slides={lightbox?.slides ?? []}
        index={lightbox?.index ?? null}
        onClose={() => setLightbox(null)}
        link={lightbox ? { to: modelPath(lightbox.slug), label: 'Open template' } : undefined}
      />
      <UploadDialog
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onUploaded={(model) => {
          setUploadOpen(false)
          added(model)
        }}
      />
      <ImportDialog
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImported={(model) => {
          setImportOpen(false)
          added(model)
        }}
      />
    </div>
  )
}

function ModelCard({
  model,
  upstreamName,
  onTag,
  onOpenMedia,
}: {
  model: ModelSummary
  upstreamName?: string
  onTag: (tag: string) => void
  onOpenMedia: (slides: Slide[], index: number) => void
}) {
  const slides = useMemo(() => namedSlides(model), [model])
  // The title is the card's one link, and its ::after stretches over the card. What
  // must not follow it (the carousel, the tag chips, the origin link, the action row)
  // sits above that on `relative z-10`: a carousel's buttons cannot nest in an anchor.
  // The media links to the template itself; its expand button opens the lightbox.
  const raised = 'relative z-10'
  return (
    <li data-model-card={model.slug} className="relative">
      <Card className="h-full gap-0 py-0 transition-shadow hover:ring-foreground/25">
      <div className="p-3 pb-0">
        <MediaCarousel
          slides={slides}
          onOpen={(index) => onOpenMedia(slides, index)}
          to={modelPath(model.slug)}
          label={model.name}
          className={raised}
          lazy
          fallback={
            <ModelThumbnail
              src={model.has_thumbnail ? api.modelThumbnailUrl(model) : undefined}
              alt={model.name}
            />
          }
        />
      </div>

      <div className="px-3 pb-3">
        <div className="mt-3 flex items-center gap-2">
          <h2 className="min-w-0 truncate text-base font-medium">
            <Link
              to={modelPath(model.slug)}
              className="outline-none after:absolute after:inset-0 after:rounded-xl after:content-[''] focus-visible:after:ring-3 focus-visible:after:ring-ring/50"
            >
              {model.name}
            </Link>
          </h2>
          <UpstreamBadge state={model.upstream_state} />
        </div>
        {model.origin === 'builtin' && (
          <Badge data-testid="builtin-badge" variant="secondary" className="mt-1">
            Built-in template — read-only
          </Badge>
        )}
        {model.description && (
          <p className="mt-1 line-clamp-2 text-sm leading-snug text-muted-foreground">
            {model.description}
          </p>
        )}
        {/* Above the stretched link: a tag is a button that adds it to the filter. */}
        {(model.tags ?? []).length > 0 && (
          <ul className="mt-2.5 flex flex-wrap gap-1">
            {(model.tags ?? []).map((tag) => (
              <li key={tag} className={raised}>
                <Badge variant="secondary" asChild>
                  <button
                    type="button"
                    aria-label={`Filter by ${tag}`}
                    onClick={() => onTag(tag)}
                    className="cursor-pointer hover:bg-accent hover:text-accent-foreground"
                  >
                    {tag}
                  </button>
                </Badge>
              </li>
            ))}
          </ul>
        )}

        <p className="mt-3 border-t pt-2 text-xs text-muted-foreground">
          Updated {timeAgo(model.updated_at)}
        </p>
      </div>
      <ModelOrigin url={model.origin_url} className={cn(raised, 'px-3 pb-2.5')} />
      <div className={cn(raised, 'mt-auto flex items-center justify-between gap-2 px-3 pb-2')}>
        <DuplicatedFrom upstream={model.upstream} name={upstreamName} className="min-w-0 truncate" />
        <span className="ml-auto">
          <DuplicateModelButton slug={model.slug} name={model.name} />
        </span>
      </div>
      </Card>
    </li>
  )
}

/** `value`, or the last one that was not undefined. */
function useKept<T>(value: T | undefined): T | undefined {
  const [kept, setKept] = useState(value)
  if (value !== undefined && value !== kept) setKept(value)
  return value ?? kept
}

/**
 * The display name of each shown duplicate's upstream: from the models shown, or read
 * once when the upstream is on a page not shown (#2290). An upstream that cannot be
 * read is named by its id, as `DuplicatedFrom` does without a name.
 */
function useUpstreamNames(shown: ModelSummary[]): Map<string, string> {
  const [read, setRead] = useState<ReadonlyMap<string, string>>(new Map())
  const asked = useRef(new Set<string>())
  const names = useMemo(() => {
    const known = new Map(read)
    for (const model of shown) known.set(model.slug, model.name)
    return known
  }, [shown, read])
  useEffect(() => {
    for (const model of shown) {
      const id = model.upstream?.id
      if (!id || names.has(id) || asked.current.has(id)) continue
      asked.current.add(id)
      api.getModel(id).then(
        (upstream) => setRead((before) => new Map(before).set(id, upstream.name)),
        () => {},
      )
    }
  }, [shown, names])
  return names
}

function matches(model: ModelSummary, query: string): boolean {
  const wanted = fold(query.trim())
  if (!wanted) return true
  return [model.slug, model.name, model.description ?? '', ...(model.tags ?? [])].some((text) =>
    fold(text).includes(wanted),
  )
}

function summarise(model: ModelSummary) {
  return {
    slug: model.slug,
    name: model.name,
    description: model.description ?? null,
    tags: model.tags ?? [],
    origin: model.origin,
  }
}

function NoResults({ onClear }: { onClear: () => void }) {
  return (
    <Card className="items-center border border-dashed bg-transparent px-6 py-10 text-center ring-0">
      <h2 className="text-base font-medium">No models match</h2>
      <p className="max-w-md text-sm leading-relaxed text-muted-foreground">
        Nothing in the catalogue fits this search and these filters.
      </p>
      <Button variant="outline" onClick={onClear}>
        Clear filters
      </Button>
    </Card>
  )
}

function EmptyState({ onUpload, onImport }: { onUpload: () => void; onImport: () => void }) {
  return (
    <Card className="items-center border border-dashed bg-transparent px-6 py-10 text-center ring-0">
      <h2 className="text-base font-medium">No models yet</h2>
      <p className="max-w-md text-sm leading-relaxed text-muted-foreground">
        Drop <code className="sb-num text-foreground">.scad</code> files into{' '}
        <code className="sb-num text-foreground">models/</code> on the ScadBuddy volume, or add one here.
        Parameters marked up for the MakerWorld customizer work unchanged.
      </p>
      <div className="flex justify-center gap-2">
        <Button variant="outline" onClick={onImport}>
          <LinkIcon /> Import from URL
        </Button>
        <Button onClick={onUpload}>
          <PlusIcon /> Add model
        </Button>
      </div>
    </Card>
  )
}
