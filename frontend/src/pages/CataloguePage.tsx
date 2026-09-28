import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { AgentToolError } from '../agent/types'
import { touch } from '../agent/highlight'
import { useAgentHandlers } from '../agent/useAgentHandlers'
import { api } from '../api/client'
import type { ModelSummary } from '../api/types'
import { DuplicatedFrom, DuplicateModelButton } from '../components/DuplicateModelButton'
import { CatalogueFilters } from '../components/CatalogueFilters'
import { ImportDialog } from '../components/ImportDialog'
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
import { modelPath } from '../lib/deeplink'
import { timeAgo } from '../lib/format'
import { safeHttpUrl } from '../lib/safeUrl'
import { useAsync } from '../lib/useAsync'

export function CataloguePage() {
  // #269 — live: models created, duplicated, renamed or deleted anywhere appear here.
  const { data, error, loading, setData, reload } = useAsync(() => api.listModels(), [], ['models'])
  const [uploadOpen, setUploadOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const query = useMemo(() => parseQuery(params), [params])
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

        {shown.length > 0 && (
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {shown.map((model) => (
              <ModelCard
                key={model.slug}
                model={model}
                upstreamName={data?.find((m) => m.slug === model.upstream?.id)?.name}
                onTag={addTag}
              />
            ))}
          </ul>
        )}
      </div>

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
}: {
  model: ModelSummary
  upstreamName?: string
  onTag: (tag: string) => void
}) {
  const origin = safeHttpUrl(model.origin_url)
  return (
    <li
      data-model-card={model.slug}
      className="group rounded-[6px] border border-line bg-surface transition-colors hover:border-line-strong">
      <Link to={modelPath(model.slug)} className="block p-3 pb-0 focus-visible:rounded-[6px]">
        <ModelThumbnail
          src={model.has_thumbnail ? api.modelThumbnailUrl(model) : undefined}
          alt={model.name}
        />

        <div className="mt-3 flex items-center gap-2">
          <h2 className="min-w-0 truncate text-[14px] font-medium">{model.name}</h2>
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
      </Link>
      {/* Outside the card's link too: a tag is a button that adds it to the filter. */}
      <div className="px-3 pb-3">
        {(model.tags ?? []).length > 0 && (
          <ul className="mt-2.5 flex flex-wrap gap-1">
            {(model.tags ?? []).map((tag) => (
              <li key={tag}>
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
      {/* Outside the card's link: an anchor cannot nest inside another. Only an
          http(s) origin is linked at all; anything else is not shown (#179). */}
      {origin && (
        <p className="truncate px-3 pb-2.5 text-[12px] text-faint">
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
      <div className="flex items-center justify-between gap-2 px-3 pb-2">
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
