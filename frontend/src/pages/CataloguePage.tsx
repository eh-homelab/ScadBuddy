import { useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { api } from '../api/client'
import type { ModelSummary } from '../api/types'
import { ModelThumbnail } from '../components/ModelThumbnail'
import { UploadDialog } from '../components/UploadDialog'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/useAsync'

export function CataloguePage() {
  const { data, error, loading, setData, reload } = useAsync(() => api.listModels(), [])
  const [uploadOpen, setUploadOpen] = useState(false)
  const navigate = useNavigate()

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
          <Button variant="primary" onClick={() => setUploadOpen(true)}>
            Add model
          </Button>
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

        {data && data.length === 0 && <EmptyState onUpload={() => setUploadOpen(true)} />}

        {data && data.length > 0 && (
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {data.map((model) => (
              <ModelCard key={model.slug} model={model} />
            ))}
          </ul>
        )}
      </div>

      <UploadDialog
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onUploaded={(model) => {
          setData([model, ...(data ?? []).filter((m) => m.slug !== model.slug)])
          setUploadOpen(false)
          void navigate(`/m/${model.slug}`)
        }}
      />
    </div>
  )
}

function ModelCard({ model }: { model: ModelSummary }) {
  return (
    <li className="group rounded-[6px] border border-line bg-surface transition-colors hover:border-line-strong">
      <Link to={`/m/${model.slug}`} className="block p-3 focus-visible:rounded-[6px]">
        <ModelThumbnail src={model.thumbnail_url} alt={model.name} />

        <h2 className="mt-3 text-[14px] font-medium">{model.name}</h2>
        {model.description && (
          <p className="mt-1 line-clamp-2 text-[13px] leading-snug text-muted">
            {model.description}
          </p>
        )}

        {model.tags.length > 0 && (
          <ul className="mt-2.5 flex flex-wrap gap-1">
            {model.tags.map((tag) => (
              <li
                key={tag}
                className="rounded-[3px] bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted"
              >
                {tag}
              </li>
            ))}
          </ul>
        )}

        <p className="mt-3 border-t border-line pt-2 text-[12px] text-faint">
          {model.last_generated_at ? (
            <>
              <span className="sb-num">{model.output_count}</span>{' '}
              {model.output_count === 1 ? 'output' : 'outputs'} · last generated{' '}
              {timeAgo(model.last_generated_at)}
            </>
          ) : (
            'Never generated'
          )}
        </p>
      </Link>
    </li>
  )
}

function EmptyState({ onUpload }: { onUpload: () => void }) {
  return (
    <div className="rounded-[6px] border border-dashed border-line-strong bg-surface p-10 text-center">
      <h2 className="text-[15px] font-medium">No models yet</h2>
      <p className="mx-auto mt-2 max-w-md text-[13px] leading-relaxed text-muted">
        Drop <code className="sb-num text-ink">.scad</code> files into{' '}
        <code className="sb-num text-ink">models/</code> on the ScadBuddy volume, or add one here.
        Parameters marked up for the MakerWorld customizer work unchanged.
      </p>
      <Button variant="primary" className="mt-4" onClick={onUpload}>
        Add model
      </Button>
    </div>
  )
}
