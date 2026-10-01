import { useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { USER_ONLY } from '../agent/dom'
import { ApiError, api } from '../api/client'
import type { CustomizerSchema, LibraryCopy, Output } from '../api/types'
import { BomTable } from '../components/BomTable'
import { ColorStrip } from '../components/ColorStrip'
import { SendDialog } from '../components/SendDialog'
import { Button } from '../components/ui/Button'
import { Dialog } from '../components/ui/Dialog'
import { Spinner } from '../components/ui/Spinner'
import { editPath, editTargetFor, modelPath, type EditNavigationState } from '../lib/deeplink'
import { formatBbox, formatValue, timeAgo } from '../lib/format'
import { useDisplayUnit } from '../lib/units'
import { diffFromDefaults } from '../lib/params'
import { useAsync } from '../lib/useAsync'
import { isEmbedded } from '../lib/embed'

/** Output ids are 32 hex characters; only the head of one is worth showing. */
function shortId(id: string): string {
  return id.slice(0, 8)
}

export function HistoryPage() {
  const { slug = '' } = useParams()
  const navigate = useNavigate()
  const schemaState = useAsync(() => api.getSchema(slug), [slug])
  // #269 — live: outputs saved or deleted elsewhere show up here. Print progress is
  // on `print:<output id>`, which this list does not follow.
  const outputsState = useAsync(() => api.listOutputs(slug), [slug], [`model:${slug}`])
  // #89 — an output records Bambuddy's ids, never a URL, so the base to deep-link them
  // against comes from Settings. Until it answers, the ids still read as plain text.
  const settings = useAsync(() => api.getSettings(), []).data
  const bambuddyUrl = settings?.bambuddy_url ?? undefined
  const [sendFor, setSendFor] = useState<Output | undefined>(undefined)
  const [deleting, setDeleting] = useState<string | null>(null)
  // #316 — an output with copies in Bambuddy asks first, and offers the inbox ones.
  const [confirmFor, setConfirmFor] = useState<Output | undefined>(undefined)

  async function remove(id: string, deleteInboxCopies = false) {
    setDeleting(id)
    try {
      await api.deleteOutput(id, deleteInboxCopies)
      outputsState.setData((outputsState.data ?? []).filter((o) => o.id !== id))
      setConfirmFor(undefined)
    } finally {
      setDeleting(null)
    }
  }

  function requestDelete(output: Output) {
    if ((output.library_files ?? []).length === 0) void remove(output.id)
    else setConfirmFor(output)
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
          <Link to={modelPath(slug)} className="text-[12px] text-muted hover:text-ink">
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
            <Button variant="primary" className="mt-4" onClick={() => void navigate(modelPath(slug))}>
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
                onDelete={() => requestDelete(output)}
                bambuddyUrl={bambuddyUrl}
              />
            ))}
          </ul>
        )}
      </div>

      <DeleteOutputDialog
        output={confirmFor}
        // Undefined until the settings load: every copy then reads as not yet placed,
        // rather than being labelled against a guessed inbox.
        inboxFolderId={settings === undefined ? undefined : (settings.library_folder_id ?? null)}
        deleting={confirmFor !== undefined && deleting === confirmFor.id}
        onClose={() => setConfirmFor(undefined)}
        onConfirm={(output, deleteInboxCopies) => remove(output.id, deleteInboxCopies)}
      />

      <SendDialog
        open={sendFor !== undefined}
        output={sendFor}
        onClose={() => setSendFor(undefined)}
        onSent={() => outputsState.reload()}
      />
    </div>
  )
}

/** The copy the output was last uploaded as; copies are recorded in upload order. */
function lastCopy(output: Output): LibraryCopy | undefined {
  const copies = output.library_files ?? []
  return copies[copies.length - 1]
}

type CopyPlace = 'inbox' | 'project' | 'unknown'

function placeOf(copy: LibraryCopy, inboxFolderId: number | null | undefined): CopyPlace {
  if (inboxFolderId === undefined) return 'unknown'
  return (copy.folder_id ?? null) === inboxFolderId ? 'inbox' : 'project'
}

/**
 * #316 — deleting an output that has copies in Bambuddy's library. The copies in the
 * inbox folder can go with it; a copy in a project's folder is that project's record
 * of what it printed and always stays. While the inbox folder is not known yet, the
 * server decides, and deletes a copy only if it is in the inbox.
 */
function DeleteOutputDialog({
  output,
  inboxFolderId,
  deleting,
  onClose,
  onConfirm,
}: {
  output: Output | undefined
  inboxFolderId: number | null | undefined
  deleting: boolean
  onClose: () => void
  onConfirm: (output: Output, deleteInboxCopies: boolean) => Promise<void>
}) {
  const [alsoInbox, setAlsoInbox] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const copies = output?.library_files ?? []
  const count = (place: CopyPlace) =>
    copies.filter((copy) => placeOf(copy, inboxFolderId) === place).length
  const removable = count('inbox') + count('unknown')

  function close() {
    if (deleting) return
    setAlsoInbox(false)
    setError(null)
    onClose()
  }

  async function confirm() {
    if (!output) return
    setError(null)
    try {
      await onConfirm(output, alsoInbox && removable > 0)
      setAlsoInbox(false)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
    }
  }

  return (
    <Dialog
      open={output !== undefined}
      title={`Delete ${output?.name ?? (output ? shortId(output.id) : '')}?`}
      onClose={close}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={deleting}>
            Cancel
          </Button>
          <Button variant="danger" onClick={() => void confirm()} disabled={deleting} {...USER_ONLY}>
            {deleting ? <Spinner /> : 'Delete output'}
          </Button>
        </>
      }
    >
      <p className="text-[13px] text-muted">This output has copies in Bambuddy&apos;s library:</p>
      <ul className="mt-1 list-disc pl-5 text-[13px] text-muted" aria-label="Library copies">
        {copies.map((copy) => {
          const place = placeOf(copy, inboxFolderId)
          return (
            <li key={copy.id}>
              <span className="sb-num">#{copy.id}</span>{' '}
              {place === 'inbox'
                ? 'in the inbox folder'
                : place === 'project'
                  ? 'in a project folder, kept'
                  : 'folder not recorded; checked before deleting'}
            </li>
          )
        })}
      </ul>
      {removable > 0 ? (
        <label className="mt-3 flex items-center gap-2 text-[13px] text-ink">
          <input
            type="checkbox"
            checked={alsoInbox}
            onChange={(event) => setAlsoInbox(event.target.checked)}
          />
          Also delete the inbox copies in Bambuddy
        </label>
      ) : (
        <p className="mt-3 text-[13px] text-muted">Copies in project folders stay in Bambuddy.</p>
      )}
      <p className="mt-2 text-[12px] text-faint">
        Sliced files and project copies are never deleted from here.
      </p>
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-warn">
          {error}
        </p>
      )}
    </Dialog>
  )
}

/**
 * A recorded Bambuddy id, linked to the page it means something on (#89). Bambuddy has
 * no page per pipeline run — its copies land in the queue — so a run links to the queue
 * itself rather than to an invented path. `target=_blank` because ScadBuddy renders
 * inside Bambuddy's sandboxed iframe (spec §1).
 */
function BambuddyId({
  href,
  className,
  children,
}: {
  href: string | undefined
  className: string
  children: ReactNode
}) {
  if (!href) return <span className={className}>{children}</span>
  return (
    <a
      className={`${className} underline decoration-dotted underline-offset-2`}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
    >
      {children}
    </a>
  )
}

function OutputRow({
  output,
  schema,
  deleting,
  onEdit,
  onSend,
  onDelete,
  bambuddyUrl,
}: {
  output: Output
  schema: CustomizerSchema
  deleting: boolean
  onEdit: () => void
  onSend: () => void
  onDelete: () => void
  bambuddyUrl: string | undefined
}) {
  const diff = diffFromDefaults(schema, output.params ?? {})
  const unit = useDisplayUnit()

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
            {output.bbox_mm ? formatBbox(output.bbox_mm, unit) : 'No dimensions recorded'}
            {(output.plates ?? []).length > 1 &&
              (output.plates ?? []).map((plate) => (
                <BambuddyId
                  key={plate.queue_item_id}
                  className="ml-2 text-ok"
                  href={bambuddyUrl && `${bambuddyUrl}/queue/${plate.queue_item_id}`}
                >
                  plate {plate.plate_id} queued #{plate.queue_item_id}
                </BambuddyId>
              ))}
            {output.queue_item_id && (output.plates ?? []).length <= 1 && (
              <BambuddyId
                className="ml-2 text-ok"
                href={bambuddyUrl && `${bambuddyUrl}/queue/${output.queue_item_id}`}
              >
                queued #{output.queue_item_id}
              </BambuddyId>
            )}
            {!output.queue_item_id && output.pipeline_run_id && (
              <BambuddyId className="ml-2 text-ok" href={bambuddyUrl && `${bambuddyUrl}/queue`}>
                pipeline run #{output.pipeline_run_id}
              </BambuddyId>
            )}
            {!output.queue_item_id && !output.pipeline_run_id && lastCopy(output) && (
              <BambuddyId className="ml-2 text-muted" href={bambuddyUrl && `${bambuddyUrl}/library`}>
                in library #{lastCopy(output)?.id}
              </BambuddyId>
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
          <Button size="sm" variant="danger" onClick={onDelete} disabled={deleting} {...USER_ONLY}>
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

      {/* A pipeline output's bill of materials and extra files (spec 2026-09-27 §5.2). */}
      <BomTable bom={output.bom ?? []} />
      {(output.files ?? []).length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-3 text-[12px]">
          {(output.files ?? []).map((name) => (
            <li key={name}>
              {/* Bambuddy's iframe sandbox has no allow-downloads: there the file opens in
                  a tab that escapes it (allow-popups-to-escape-sandbox), as lib/embed.ts does. */}
              <a
                className="text-accent underline"
                href={api.outputFileUrl(output.id, name)}
                download
                rel="noopener"
                target={isEmbedded() ? '_blank' : undefined}
              >
                {name}
              </a>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}
