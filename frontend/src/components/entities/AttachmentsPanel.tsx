import { memo, useRef, useState, type DragEvent } from 'react'
import { Link } from 'react-router'
import { DownloadIcon, ExternalLinkIcon, PaperclipIcon, PencilIcon, Trash2Icon } from 'lucide-react'
import { NEW_TAB, downloadBlob } from '../../lib/embed'
import { formatBytes } from '../../lib/format'
import { failure, formatMegabytes, useUploadLimit } from '../../lib/mediaFiles'
import { useAsync } from '../../lib/useAsync'
import { FilePick } from '../orders/OrderAttachments'
import { ENTITY_ROLES, OPENS, entityImageUrl, roleLabel } from './attachmentMedia'
import { ORDER_KINDS, PRINT_ARCHIVE, entityApi, entityParts, type EntityAttachment } from './api'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'
import { cn } from '@/lib/utils'

/** The server's caption limit (`MAX_ATTACHMENT_CAPTION`, `orders/models.py`). */
const MAX_CAPTION_CHARS = 1000

type Upload = { id: number; name: string; fraction: number }

/**
 * #6950 — the files of any entity (`entity` is `kind:ref`, such as `spool:<id>`): photos,
 * receipts, manuals, each with a role and a caption. An order's files are listed here but
 * added and changed on its order page; a print's photos are Bambuddy's, listed by name.
 */
/** Memoized: the forms it sits in re-render on every keystroke, and its props are strings. */
export const AttachmentsPanel = memo(function AttachmentsPanel({ entity, title = 'Files' }: { entity: string; title?: string }) {
  const { kind, ref } = entityParts(entity)
  const list = useAsync(() => entityApi.attachments(entity), [entity])
  const items = list.data ?? []
  const ofOrder = ORDER_KINDS.has(kind)
  const ofPrint = kind === PRINT_ARCHIVE
  const readOnly = ofOrder || ofPrint
  const limit = useUploadLimit()
  const [role, setRole] = useState<string>('photo')
  const [caption, setCaption] = useState('')
  const [uploads, setUploads] = useState<Upload[]>([])
  const nextUpload = useRef(0)
  const [errors, setErrors] = useState<string[]>([])
  const [over, setOver] = useState(false)

  async function add(files: File[]) {
    if (files.length === 0) return
    const most = await limit()
    const fine = files.filter((file) => file.size > 0 && file.size <= most)
    setErrors(
      files
        .filter((file) => !fine.includes(file))
        .map((file) => (file.size === 0 ? `${file.name} is empty.` : `${file.name} is larger than the ${formatMegabytes(most)} upload limit.`)),
    )
    const fields = { role, caption: caption.trim() || undefined }
    let added = 0
    for (const file of fine) {
      const id = (nextUpload.current += 1)
      setUploads((now) => [...now, { id, name: file.name, fraction: 0 }])
      try {
        await entityApi.addAttachment(entity, file, fields, (fraction) =>
          setUploads((now) => now.map((upload) => (upload.id === id ? { ...upload, fraction } : upload))),
        )
        added += 1
        list.refresh()
      } catch (caught) {
        setErrors((now) => [...now, `${file.name}: ${failure(caught)}`])
      } finally {
        setUploads((now) => now.filter((upload) => upload.id !== id))
      }
    }
    // Kept when nothing was added, so the person can send the files again with it.
    if (added > 0) setCaption('')
  }

  function dropped(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setOver(false)
    void add([...event.dataTransfer.files])
  }

  return (
    <section aria-label={title} className="flex flex-col gap-3">
      <div className="flex items-baseline gap-2">
        <h3 className="text-[13px] font-medium">{title}</h3>
        {items.length > 0 && <span className="text-[12px] text-muted-foreground tabular-nums">{items.length}</span>}
      </div>

      {ofOrder ? (
        <p className="text-[13px] text-muted-foreground">
          An order's files follow the order's rules.{' '}
          <Link to={kind === 'order' ? `/orders/${encodeURIComponent(ref)}` : '/orders'} className="underline underline-offset-4 hover:text-foreground">
            Add files on the order page
          </Link>
        </p>
      ) : ofPrint ? (
        <p className="text-[13px] text-muted-foreground">Bambuddy keeps this print's photos and videos.</p>
      ) : (
        !readOnly && (
          <div
            data-testid="entity-attach-drop"
            onDragOver={(event) => {
              event.preventDefault()
              setOver(true)
            }}
            onDragLeave={() => setOver(false)}
            onDrop={dropped}
            className={cn('flex flex-wrap items-center gap-2 rounded-md border border-dashed p-2.5 text-[13px] text-muted-foreground transition-colors', over && 'border-primary bg-muted')}
          >
            <span className="mr-auto">Drop a photo, receipt or manual here.</span>
            <Select value={role} onValueChange={setRole}>
              <SelectTrigger size="sm" aria-label="Add files as" className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                {ENTITY_ROLES.map((r) => (
                  <SelectItem key={r} value={r}>
                    {roleLabel(r)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input
              value={caption}
              onChange={(event) => setCaption(event.target.value)}
              aria-label="Caption for new files"
              placeholder="Caption (optional)"
              maxLength={MAX_CAPTION_CHARS}
              className="h-8 w-44 min-w-0"
            />
            <FilePick label="Attach files" multiple onPick={(files) => void add(files)} icon={<PaperclipIcon aria-hidden="true" />} />
          </div>
        )
      )}

      {uploads.length > 0 && (
        <ul aria-label="Uploading" className="space-y-1 text-xs text-muted-foreground">
          {uploads.map((upload) => (
            <li key={upload.id} className="flex items-center gap-2">
              <Spinner /> {upload.name}
              <progress max={1} value={upload.fraction} aria-label={`Uploading ${upload.name}`} className="h-1.5 flex-1 overflow-hidden rounded-full accent-primary" />
            </li>
          ))}
        </ul>
      )}
      {errors.length > 0 && (
        <ul role="alert" className="space-y-0.5 text-xs text-warn">
          {errors.map((error, at) => (
            <li key={at}>{error}</li>
          ))}
        </ul>
      )}

      {list.error ? (
        <p role="alert" className="text-[13px] text-warn">
          The files could not be loaded: {failure(list.error)}
        </p>
      ) : list.loading && !list.data ? (
        <p className="text-[13px] text-muted-foreground">Loading the files…</p>
      ) : items.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No files yet.</p>
      ) : (
        <ul aria-label={title} className="grid grid-cols-[repeat(auto-fill,minmax(9.5rem,1fr))] gap-3">
          {items.map((attachment) =>
            ofPrint ? (
              <PrintFile key={attachment.id} attachment={attachment} archiveId={ref} />
            ) : (
              <FileTile key={attachment.id} entity={entity} attachment={attachment} readOnly={readOnly} onChanged={() => list.refresh()} />
            ),
          )}
        </ul>
      )}
    </section>
  )
})

/** The file's extension, set large where a photo would be. */
function extensionOf(filename: string): string {
  const dot = filename.lastIndexOf('.')
  return dot > 0 && filename.length - dot <= 6 ? filename.slice(dot + 1).toUpperCase() : 'FILE'
}

/** A print archive's file: Bambuddy serves it on the print's page, so no preview here. */
function PrintFile({ attachment, archiveId }: { attachment: EntityAttachment; archiveId: string }) {
  const name = attachment.caption || attachment.filename
  return (
    <li aria-label={name} className="flex min-w-0 flex-col gap-1 rounded-md border p-2.5 text-[13px]">
      <span className="truncate font-medium" title={attachment.filename}>
        {attachment.filename}
      </span>
      {attachment.caption && <span className="text-muted-foreground">{attachment.caption}</span>}
      <Link to={`/prints/${encodeURIComponent(archiveId)}`} className="mt-auto inline-flex items-center gap-1 text-[12px] underline underline-offset-4">
        Open the print
      </Link>
    </li>
  )
}

type Busy = 'save' | 'delete' | 'download'

function FileTile({ entity, attachment, readOnly, onChanged }: { entity: string; attachment: EntityAttachment; readOnly: boolean; onChanged: () => void }) {
  const name = attachment.caption || attachment.filename
  const image = entityImageUrl(entity, attachment)
  const content = entityApi.attachmentUrl(entity, attachment.id)
  // The thumbnail, then the file itself (a photo the server could not shrink), then the extension.
  const [failures, setFailures] = useState(0)
  const shown = image === null ? null : failures === 0 ? image : failures === 1 && image !== content ? content : null
  const [editing, setEditing] = useState(false)
  const [role, setRole] = useState(attachment.role)
  const [caption, setCaption] = useState(attachment.caption)
  const [busy, setBusy] = useState<Busy | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const roles: string[] = ENTITY_ROLES.some((r) => r === attachment.role) ? [...ENTITY_ROLES] : [attachment.role, ...ENTITY_ROLES]

  async function act(what: Busy, call: () => Promise<unknown>) {
    setBusy(what)
    setError(null)
    try {
      await call()
      if (what === 'save') setEditing(false)
      if (what === 'delete') setConfirming(false)
      if (what !== 'download') onChanged()
    } catch (caught) {
      setError(failure(caught))
    } finally {
      setBusy(null)
    }
  }

  return (
    <li aria-label={name} className="flex min-w-0 flex-col overflow-hidden rounded-md border bg-card text-[13px]">
      <div className="relative aspect-[4/3] bg-muted">
        {attachment.kind === 'video' ? (
          <video src={content} controls playsInline preload="metadata" aria-label={name} className="size-full bg-black object-contain" />
        ) : shown ? (
          <img key={shown} src={shown} alt={name} loading="lazy" className="size-full object-cover" onError={() => setFailures((n) => n + 1)} />
        ) : (
          <span aria-hidden="true" className="flex size-full items-center justify-center text-2xl font-semibold tracking-tight text-muted-foreground/70">
            {extensionOf(attachment.filename)}
          </span>
        )}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5 p-2">
        {editing ? (
          <form
            className="flex flex-col gap-1.5"
            onSubmit={(event) => {
              event.preventDefault()
              void act('save', () => entityApi.updateAttachment(entity, attachment.id, { role, caption: caption.trim() }))
            }}
          >
            <Select value={role} onValueChange={setRole}>
              <SelectTrigger size="sm" aria-label={`Role of ${attachment.filename}`} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                {roles.map((r) => (
                  <SelectItem key={r} value={r}>
                    {roleLabel(r)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Input
              value={caption}
              onChange={(event) => setCaption(event.target.value)}
              aria-label={`Caption for ${attachment.filename}`}
              placeholder="Caption"
              maxLength={MAX_CAPTION_CHARS}
              className="h-7"
            />
            <div className="flex gap-1">
              <Button size="xs" type="submit" disabled={busy !== null}>
                {busy === 'save' && <Spinner />}
                Save
              </Button>
              <Button
                size="xs"
                type="button"
                variant="ghost"
                disabled={busy !== null}
                onClick={() => {
                  setEditing(false)
                  setRole(attachment.role)
                  setCaption(attachment.caption)
                  setError(null)
                }}
              >
                Cancel
              </Button>
            </div>
          </form>
        ) : (
          <>
            {attachment.caption && <span className="line-clamp-2 font-medium">{attachment.caption}</span>}
            <span className="truncate text-[12px] text-muted-foreground" title={attachment.filename}>
              {attachment.filename}
            </span>
            <span className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
              <span className="rounded-sm bg-muted px-1.5 py-px text-foreground">{roleLabel(attachment.role)}</span>
              <span className="tabular-nums">{formatBytes(attachment.size_bytes)}</span>
            </span>
          </>
        )}
        {!editing && (
          <div className="mt-auto flex items-center gap-0.5 pt-1">
            {OPENS.has(attachment.media_type) && (
              <Button asChild variant="ghost" size="icon-sm" aria-label={`Open ${attachment.filename}`} title="Open">
                <a href={content} {...NEW_TAB}>
                  <ExternalLinkIcon aria-hidden="true" />
                </a>
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon-sm"
              disabled={busy !== null}
              aria-label={`Download ${attachment.filename}`}
              title="Download"
              onClick={() =>
                void act('download', () =>
                  downloadBlob(async () => {
                    const response = await fetch(entityApi.attachmentUrl(entity, attachment.id, true))
                    if (!response.ok) throw new Error(`The download failed (${response.status}).`)
                    return response.blob()
                  }, attachment.filename),
                )
              }
            >
              {busy === 'download' ? <Spinner /> : <DownloadIcon aria-hidden="true" />}
            </Button>
            {!readOnly && (
              <>
                <Button variant="ghost" size="icon-sm" aria-label={`Edit ${attachment.filename}`} title="Edit" onClick={() => setEditing(true)}>
                  <PencilIcon aria-hidden="true" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Remove ${attachment.filename}`}
                  title="Remove"
                  className="ml-auto text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                  onClick={() => {
                    setError(null)
                    setConfirming(true)
                  }}
                >
                  <Trash2Icon aria-hidden="true" />
                </Button>
              </>
            )}
          </div>
        )}
        {error && !confirming && (
          <p role="alert" className="text-xs text-warn">
            {error}
          </p>
        )}
      </div>
      <AlertDialog open={confirming} onOpenChange={(next) => !next && busy !== 'delete' && setConfirming(false)}>
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>{`Remove ${name}?`}</AlertDialogTitle>
            <AlertDialogDescription>The file goes from this record. This cannot be undone.</AlertDialogDescription>
          </AlertDialogHeader>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy === 'delete'}>Cancel</AlertDialogCancel>
            <Button variant="destructive" disabled={busy !== null} onClick={() => void act('delete', () => entityApi.deleteAttachment(entity, attachment.id))}>
              {busy === 'delete' && <Spinner />}
              Remove
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </li>
  )
}
