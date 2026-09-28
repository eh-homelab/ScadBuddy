import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useNavigationType, useSearchParams } from 'react-router'
import { AgentToolError } from '../agent/types'
import { touch } from '../agent/highlight'
import { useAgentHandlers } from '../agent/useAgentHandlers'
import { api } from '../api/client'
import type { ModelSummary } from '../api/types'
import { DuplicatedFrom, DuplicateModelButton } from '../components/DuplicateModelButton'
import { CatalogueFilters } from '../components/CatalogueFilters'
import { ImportDialog } from '../components/ImportDialog'
import { MediaCarousel } from '../components/media/MediaCarousel'
import { MediaLightbox } from '../components/media/MediaLightbox'
import { namedSlides, type Slide } from '../components/media/slides'
import { ModelRow } from '../components/ModelRow'
import { ModelThumbnail } from '../components/ModelThumbnail'
import { UploadDialog } from '../components/UploadDialog'
import { UpstreamBadge } from '../components/UpstreamUpdate'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import {
  type CatalogueQuery,
  clearFilters,
  filterModels,
  fold,
  parseQuery,
  tagCounts,
  toParams,
} from '../lib/catalogueQuery'
import { readStoredView, storeView } from '../lib/catalogueView'
import { modelPath } from '../lib/deeplink'
import { timeAgo } from '../lib/format'
import { safeHttpUrl } from '../lib/safeUrl'
import { useAsync } from '../lib/useAsync'

export function CataloguePage() {
  // #269 — live: models created, duplicated, renamed or deleted anywhere appear here.
  const { data, error, loading, setData, reload } = useAsync(() => api.listModels(), [], ['models'])
  const [uploadOpen, setUploadOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  // One lightbox for the page: whichever card's media was clicked, at that item.
  const [lightbox, setLightbox] = useState<{ slides: Slide[]; index: number } | null>(null)
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const query = useMemo(() => parseQuery(params), [params])
  // #278 — arriving with no `view` in the URL, the one this browser last chose is
  // written into it (replacing this entry), so the URL stays the one source of truth
  // and back/forward move between views like any other filter (#276). That covers any
  // in-app navigation to `/` while the page stays mounted (the Models tab, the agent's
  // `navigate`), but not Back/Forward to an entry without a view, which keeps Cards.
  const location = useLocation()
  const navigationType = useNavigationType()
  const arrived = useRef(false)
  useEffect(() => {
    const first = !arrived.current
    arrived.current = true
    // The first load is a POP too; only a later one is Back/Forward.
    if (!first && navigationType === 'POP') return
    const stored = readStoredView()
    if (!params.has('view') && stored && stored !== query.view) {
      setParams(toParams({ ...query, view: stored }), { replace: true })
    }
    // Per navigation only: between navigations the URL alone decides.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key])
  const shown = useMemo(() => (data ? filterModels(data, query) : []), [data, query])
  // Counted over what the other filters leave, so a chip's count is what clicking it shows.
  const tags = useMemo(() => tagCounts(shown, query.tags), [shown, query.tags])

  function setQuery(next: CatalogueQuery, options?: { replace?: boolean }) {
    if (next.view !== query.view) storeView(next.view)
    setParams(toParams(next), options)
  }

  function addTag(tag: string) {
    if (!query.tags.includes(tag)) setQuery({ ...query, tags: [...query.tags, tag] })
  }

  // #254 — the catalogue's browser tools. `search` filters the list the page already
  // holds rather than typing into the search box; the filters on screen (#276) are
  // URL state, which the global `navigate` tool sets (`/?q=…&tag=…`).
  useAgentHandlers(
    'catalogue',
    {
      search: ({ query }) => {
        if (!data) throw new AgentToolError('timeout', error ? `The catalogue did not load: ${error.message}` : 'The catalogue is still loading.')
        return data.filter((model) => matches(model, query)).map(summarise)
      },
      open_model: async ({ slug }) => {
        const model = data?.find((entry) => entry.slug === slug)
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
    () => ({ loading, error: error?.message, models: data?.length, shown: shown.length }),
  )

  function added(model: ModelSummary) {
    setData([model, ...(data ?? []).filter((m) => m.slug !== model.slug)])
    void navigate(modelPath(model.slug))
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5 flex items-end justify-between gap-4">
          <div>
            <h1 className="text-lg font-semibold tracking-tight">Models</h1>
            <p className="mt-0.5 text-[13px] text-muted">
              Pick a model to set its parameters and generate a multi-colour 3MF.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button onClick={() => void navigate('/new')}>Paste source</Button>
            <Button onClick={() => setImportOpen(true)}>Import from URL</Button>
            <Button variant="primary" onClick={() => setUploadOpen(true)}>
              Add model
            </Button>
          </div>
        </div>

        {loading && (
          <p className="flex items-center gap-2 py-16 text-[13px] text-muted">
            <Spinner /> Loading models
          </p>
        )}

        {error && (
          <div role="alert" className="rounded-[6px] border border-warn/40 bg-warn/8 p-4">
            <p className="text-[13px] text-warn">Could not load the catalogue: {error.message}</p>
            <Button size="sm" className="mt-3" onClick={reload}>
              Try again
            </Button>
          </div>
        )}

        {data && data.length === 0 && (
          <EmptyState onUpload={() => setUploadOpen(true)} onImport={() => setImportOpen(true)} />
        )}

        {data && data.length > 0 && (
          <CatalogueFilters
            query={query}
            onChange={setQuery}
            tags={tags}
            shown={shown.length}
            total={data.length}
          />
        )}

        {data && data.length > 0 && shown.length === 0 && (
          <NoResults onClear={() => setQuery(clearFilters(query))} />
        )}

        {shown.length > 0 && query.view === 'list' && (
          <ul aria-label="Models" className="flex flex-col gap-2">
            {shown.map((model) => (
              <ModelRow
                key={model.slug}
                model={model}
                onOpen={(slides, index) => setLightbox({ slides, index })}
                onTag={addTag}
              />
            ))}
          </ul>
        )}

        {shown.length > 0 && query.view === 'cards' && (
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {shown.map((model) => (
              <ModelCard
                key={model.slug}
                model={model}
                upstreamName={data?.find((m) => m.slug === model.upstream?.id)?.name}
                onTag={addTag}
                onOpenMedia={(slides, index) => setLightbox({ slides, index })}
              />
            ))}
          </ul>
        )}
      </div>

      <MediaLightbox
        slides={lightbox?.slides ?? []}
        index={lightbox?.index ?? null}
        onClose={() => setLightbox(null)}
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
  const origin = safeHttpUrl(model.origin_url)
  const slides = useMemo(() => namedSlides(model), [model])
  // The title is the card's one link, and its ::after stretches over the card. What
  // must not follow it (the carousel, the tag chips, the origin link, the action row)
  // sits above that on `relative z-10`: a carousel's buttons cannot nest in an anchor.
  const raised = 'relative z-10'
  return (
    <li
      data-model-card={model.slug}
      className="relative rounded-[6px] border border-line bg-surface transition-colors hover:border-line-strong">
      <div className="p-3 pb-0">
        <MediaCarousel
          slides={slides}
          onOpen={(index) => onOpenMedia(slides, index)}
          label={model.name}
          className={raised}
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
          <h2 className="min-w-0 truncate text-[14px] font-medium">
            <Link
              to={modelPath(model.slug)}
              className="outline-none after:absolute after:inset-0 after:rounded-[6px] after:content-[''] focus-visible:after:ring-2 focus-visible:after:ring-accent"
            >
              {model.name}
            </Link>
          </h2>
          <UpstreamBadge state={model.upstream_state} />
        </div>
        {model.origin === 'builtin' && (
          <p data-testid="builtin-badge" className="mt-0.5 text-[11px] text-faint">
            Built-in template — read-only
          </p>
        )}
        {model.description && (
          <p className="mt-1 line-clamp-2 text-[13px] leading-snug text-muted">
            {model.description}
          </p>
        )}
        {/* Above the stretched link: a tag is a button that adds it to the filter. */}
        {(model.tags ?? []).length > 0 && (
          <ul className="mt-2.5 flex flex-wrap gap-1">
            {(model.tags ?? []).map((tag) => (
              <li key={tag} className={raised}>
                <button
                  type="button"
                  aria-label={`Filter by ${tag}`}
                  onClick={() => onTag(tag)}
                  className="rounded-[3px] bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted transition-colors hover:bg-surface-3 hover:text-ink"
                >
                  {tag}
                </button>
              </li>
            ))}
          </ul>
        )}

        <p className="mt-3 border-t border-line pt-2 text-[12px] text-faint">
          Updated {timeAgo(model.updated_at)}
        </p>
      </div>
      {/* Only an http(s) origin is linked at all; anything else is not shown (#179). */}
      {origin && (
        <p className={`${raised} truncate px-3 pb-2.5 text-[12px] text-faint`}>
          From{' '}
          <a
            href={origin}
            target="_blank"
            rel="noreferrer"
            className="text-muted underline decoration-line-strong underline-offset-2 hover:text-ink"
          >
            {hostOf(origin)}
          </a>
        </p>
      )}
      <div className={`${raised} flex items-center justify-between gap-2 px-3 pb-2`}>
        <DuplicatedFrom upstream={model.upstream} name={upstreamName} className="min-w-0 truncate" />
        <span className="ml-auto">
          <DuplicateModelButton slug={model.slug} name={model.name} />
        </span>
      </div>
    </li>
  )
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

function hostOf(url: string): string {
  try {
    return new URL(url).hostname
  } catch {
    return url
  }
}

function NoResults({ onClear }: { onClear: () => void }) {
  return (
    <div className="rounded-[6px] border border-dashed border-line-strong bg-surface p-10 text-center">
      <h2 className="text-[15px] font-medium">No models match</h2>
      <p className="mx-auto mt-2 max-w-md text-[13px] leading-relaxed text-muted">
        Nothing in the catalogue fits this search and these filters.
      </p>
      <Button className="mt-4" onClick={onClear}>
        Clear filters
      </Button>
    </div>
  )
}

function EmptyState({ onUpload, onImport }: { onUpload: () => void; onImport: () => void }) {
  return (
    <div className="rounded-[6px] border border-dashed border-line-strong bg-surface p-10 text-center">
      <h2 className="text-[15px] font-medium">No models yet</h2>
      <p className="mx-auto mt-2 max-w-md text-[13px] leading-relaxed text-muted">
        Drop <code className="sb-num text-ink">.scad</code> files into{' '}
        <code className="sb-num text-ink">models/</code> on the ScadBuddy volume, or add one here.
        Parameters marked up for the MakerWorld customizer work unchanged.
      </p>
      <div className="mt-4 flex justify-center gap-2">
        <Button onClick={onImport}>Import from URL</Button>
        <Button variant="primary" onClick={onUpload}>
          Add model
        </Button>
      </div>
    </div>
  )
}
