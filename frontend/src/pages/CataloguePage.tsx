import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
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
  filterModels,
  fold,
  parseQuery,
  tagCounts,
  toParams,
} from '../lib/catalogueQuery'
import { modelPath } from '../lib/deeplink'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/useAsync'

export function CataloguePage() {
  // #269 — live: models created, duplicated, renamed or deleted anywhere appear here.
  const { data, error, loading, setData, reload } = useAsync(() => api.listModels(), [], ['models'])
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
  const shown = useMemo(() => (data ? filterModels(data, query) : []), [data, query])
  // Counted over what the other filters leave, so a chip's count is what clicking it shows.
  const tags = useMemo(() => tagCounts(shown, query.tags), [shown, query.tags])

  function setQuery(next: CatalogueQuery, options?: { replace?: boolean }) {
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

        {loading && (
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
                upstreamName={data?.find((m) => m.slug === model.upstream?.id)?.name}
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
                upstreamName={data?.find((m) => m.slug === model.upstream?.id)?.name}
                onTag={addTag}
                onOpenMedia={(slides, index) => setLightbox({ slug: model.slug, slides, index })}
              />
            ))}
          </ul>
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
                    className="cursor-pointer hover:bg-ui-accent hover:text-accent-foreground"
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
