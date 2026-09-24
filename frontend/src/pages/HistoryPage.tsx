import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { api } from '../api/client'
import type { CustomizerSchema, Output } from '../api/types'
import { ColorStrip } from '../components/ColorStrip'
import { SendDialog } from '../components/SendDialog'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import { editPath, editTargetFor, type EditNavigationState } from '../lib/deeplink'
import { formatBbox, formatValue, timeAgo } from '../lib/format'
import { diffFromDefaults } from '../lib/params'
import { useAsync } from '../lib/useAsync'

/** Output ids are 32 hex characters; only the head of one is worth showing. */
function shortId(id: string): string {
  return id.slice(0, 8)
}

export function HistoryPage() {
  const { slug = '' } = useParams()
  const navigate = useNavigate()
  const schemaState = useAsync(() => api.getSchema(slug), [slug])
  const outputsState = useAsync(() => api.listOutputs(slug), [slug])
  const [sendFor, setSendFor] = useState<Output | undefined>(undefined)
  const [deleting, setDeleting] = useState<string | null>(null)

  async function remove(id: string) {
    setDeleting(id)
    try {
      await api.deleteOutput(id)
      outputsState.setData((outputsState.data ?? []).filter((o) => o.id !== id))
    } finally {
      setDeleting(null)
    }
  }

  const loading = schemaState.loading || outputsState.loading
  const schema = schemaState.data

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl px-4 py-6">
        <div className="mb-5 flex items-baseline gap-2">
          <Link to="/" className="text-[12px] text-muted hover:text-ink">
            Models
          </Link>
          <span className="text-faint">/</span>
          <Link to={`/m/${slug}`} className="text-[12px] text-muted hover:text-ink">
            {schemaState.data?.title ?? slug}
          </Link>
          <span className="text-faint">/</span>
          <h1 className="text-[13px] font-medium">History</h1>
        </div>

        {loading && (
          <p className="flex items-center gap-2 py-16 text-[13px] text-muted">
            <Spinner /> Loading history
          </p>
        )}

        {!loading && outputsState.data?.length === 0 && (
          <div className="rounded-[6px] border border-dashed border-line-strong bg-surface p-10 text-center">
            <h2 className="text-[15px] font-medium">Nothing generated yet</h2>
            <p className="mt-2 text-[13px] text-muted">
              Every output you generate is kept here with the parameters that made it.
            </p>
            <Button variant="primary" className="mt-4" onClick={() => void navigate(`/m/${slug}`)}>
              Open the customizer
            </Button>
          </div>
        )}

        {!loading && schema && outputsState.data && outputsState.data.length > 0 && (
          <ul data-testid="outputs" aria-label="Generated outputs" className="space-y-2">
            {outputsState.data.map((output) => (
              <OutputRow
                key={output.id}
                output={output}
                schema={schema}
                deleting={deleting === output.id}
                onEdit={() =>
                  void navigate(editPath(output.id), {
                    state: { editTarget: editTargetFor(output) } satisfies EditNavigationState,
                  })
                }
                onSend={() => setSendFor(output)}
                onDelete={() => void remove(output.id)}
              />
            ))}
          </ul>
        )}
      </div>

      <SendDialog
        open={sendFor !== undefined}
        output={sendFor}
        onClose={() => setSendFor(undefined)}
        onSent={() => outputsState.reload()}
      />
    </div>
  )
}

function OutputRow({
  output,
  schema,
  deleting,
  onEdit,
  onSend,
  onDelete,
}: {
  output: Output
  schema: CustomizerSchema
  deleting: boolean
  onEdit: () => void
  onSend: () => void
  onDelete: () => void
}) {
  const diff = diffFromDefaults(schema, output.params ?? {})

  return (
    <li className="rounded-[6px] border border-line bg-surface p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2.5">
            <ColorStrip colors={output.colors ?? []} size="sm" />
            <span className="text-[13px] text-ink">{output.name ?? shortId(output.id)}</span>
            <span className="text-[12px] text-faint">{timeAgo(output.created_at)}</span>
          </div>
          <p className="sb-num mt-1 text-[12px] text-muted">
            {output.bbox_mm ? formatBbox(output.bbox_mm) : 'No dimensions recorded'}
            {output.queue_item_id && (
              <span className="ml-2 text-ok">queued #{output.queue_item_id}</span>
            )}
            {!output.queue_item_id && output.pipeline_run_id && (
              <span className="ml-2 text-ok">pipeline run #{output.pipeline_run_id}</span>
            )}
            {!output.queue_item_id && !output.pipeline_run_id && output.library_file_id && (
              <span className="ml-2 text-muted">in library #{output.library_file_id}</span>
            )}
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <Button size="sm" onClick={onEdit}>
            Edit
          </Button>
          <Button size="sm" onClick={onSend}>
            Send again
          </Button>
          <Button size="sm" variant="danger" onClick={onDelete} disabled={deleting}>
            {deleting ? <Spinner /> : 'Delete'}
          </Button>
        </div>
      </div>

      <div className="mt-3 border-t border-line pt-2.5">
        {diff.length === 0 ? (
          <p className="text-[12px] text-faint">Model defaults, unchanged.</p>
        ) : (
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-[12px]">
            {diff.map((entry) => (
              <div key={entry.name} className="contents">
                <dt className="text-muted">{entry.caption}</dt>
                <dd className="sb-num min-w-0">
                  <span className="text-ink">{formatValue(entry.value)}</span>
                  <span className="ml-2 text-faint line-through">
                    {formatValue(entry.initial)}
                  </span>
                </dd>
              </div>
            ))}
          </dl>
        )}
      </div>
    </li>
  )
}
