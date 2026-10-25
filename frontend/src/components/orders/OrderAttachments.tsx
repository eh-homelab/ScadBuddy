import { useRef, useState, type DragEvent, type ReactNode } from 'react'
import { CameraIcon, DownloadIcon, ExternalLinkIcon, FileIcon, FileTextIcon, PaperclipIcon, PackageCheckIcon, Trash2Icon } from 'lucide-react'
import { api } from '../../api/client'
import type { OrderAttachment, OrderAttachmentRole } from '../../api/types'
import { NEW_TAB, downloadBlob } from '../../lib/embed'
import { formatBytes } from '../../lib/format'
import { failure, formatMegabytes, useUploadLimit } from '../../lib/mediaFiles'
import { OPENS, ROLE_LABELS, ROLES, attachmentImageUrl } from './attachmentMedia'
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
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'
import { cn } from '@/lib/utils'

/** The server's caption limit (`MAX_ATTACHMENT_CAPTION`, `orders/models.py`). */
const MAX_CAPTION_CHARS = 1000

type Upload = { id: number; name: string; fraction: number }
type Refusal = { id: number; text: string }

/**
 * #4251 — the files of an order, or of one line item (`itemId`): any file at all,
 * dropped, chosen or taken with a phone's camera, each with a role and a caption. On an
 * item, a photo can be marked as what was actually delivered (`deliveredId`), which then
 * stands as the item's picture.
 */
export function OrderAttachments({
  orderId,
  itemId = null,
  attachments,
  deliveredId = null,
  onChanged,
}: {
  orderId: string
  itemId?: string | null
  /** This subject's own files: the order's (`item_id` null) or the item's. */
  attachments: OrderAttachment[]
  deliveredId?: string | null
  onChanged: () => void
}) {
  const limit = useUploadLimit()
  const [role, setRole] = useState<OrderAttachmentRole>(itemId ? 'finished' : 'reference')
  const [uploads, setUploads] = useState<Upload[]>([])
  const nextUpload = useRef(0)
  const [errors, setErrors] = useState<Refusal[]>([])
  const nextError = useRef(0)
  const [over, setOver] = useState(false)
  const what = itemId ? 'item' : 'order'

  async function add(files: File[]) {
    if (files.length === 0) return
    const most = await limit()
    const refusal = (text: string): Refusal => ({ id: (nextError.current += 1), text })
    const fine = files.filter((file) => file.size > 0 && file.size <= most)
    setErrors(
      files
        .filter((file) => !fine.includes(file))
        .map((file) => refusal(file.size === 0 ? `${file.name} is empty.` : `${file.name} is larger than the ${formatMegabytes(most)} upload limit.`)),
    )
    const chosen = role
    for (const file of fine) {
      const id = (nextUpload.current += 1)
      setUploads((now) => [...now, { id, name: file.name, fraction: 0 }])
      try {
        await api.addOrderAttachment(orderId, itemId, file, { role: chosen }, (fraction) =>
          setUploads((now) => now.map((upload) => (upload.id === id ? { ...upload, fraction } : upload))),
        )
        onChanged()
      } catch (caught) {
        setErrors((now) => [...now, refusal(`${file.name}: ${failure(caught)}`)])
      } finally {
        setUploads((now) => now.filter((upload) => upload.id !== id))
      }
    }
  }

  function dropped(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setOver(false)
    void add([...event.dataTransfer.files])
  }

  return (
    <section className="flex flex-col gap-3" aria-label={itemId ? 'Item files' : 'Order files'}>
      <h3 className="text-sm font-medium">Files</h3>
      <div
        data-testid={`${what}-attach-drop`}
        onDragOver={(event) => {
          event.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => setOver(false)}
        onDrop={dropped}
        className={cn(
          'flex flex-wrap items-center gap-2 rounded-xl border border-dashed p-3 text-[13px] text-muted-foreground transition-colors',
          over && 'border-primary bg-muted',
        )}
      >
        <span className="mr-auto">Drop any file here: photos, videos, PDFs, drawings.</span>
        <Select value={role} onValueChange={(value) => setRole(value as OrderAttachmentRole)}>
          <SelectTrigger size="sm" aria-label={`Add ${what} files as`} className="w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            {ROLES.map((r) => (
              <SelectItem key={r} value={r}>
                {ROLE_LABELS[r]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <FilePick label="Take a photo" capture onPick={(files) => void add(files)} icon={<CameraIcon aria-hidden="true" />} accept="image/*" />
        <FilePick label={`Attach ${what} files`} multiple onPick={(files) => void add(files)} icon={<PaperclipIcon aria-hidden="true" />} />
      </div>
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
          {errors.map((error) => (
            <li key={error.id}>{error.text}</li>
          ))}
        </ul>
      )}
      {attachments.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No files yet.</p>
      ) : (
        <ul aria-label={itemId ? 'Item files' : 'Order files'} className={cn('grid gap-3 sm:grid-cols-2', !itemId && 'lg:grid-cols-3')}>
          {attachments.map((attachment) => (
            <AttachmentCard
              key={attachment.id}
              orderId={orderId}
              itemId={itemId}
              attachment={attachment}
              delivered={attachment.id === deliveredId}
              onChanged={onChanged}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

function FilePick({
  label,
  icon,
  accept,
  capture,
  multiple,
  onPick,
}: {
  label: string
  icon: ReactNode
  accept?: string
  capture?: boolean
  multiple?: boolean
  onPick: (files: File[]) => void
}) {
  return (
    <Button asChild variant="outline" size="sm" className="cursor-pointer has-focus-visible:border-ring has-focus-visible:ring-3 has-focus-visible:ring-ring/50">
      <label>
        {icon}
        {label}
        <input
          type="file"
          aria-label={label}
          accept={accept}
          multiple={multiple}
          capture={capture ? 'environment' : undefined}
          className="sr-only"
          onChange={(event) => {
            const files = [...(event.target.files ?? [])]
            event.target.value = ''
            onPick(files)
          }}
        />
      </label>
    </Button>
  )
}

type Busy = 'caption' | 'role' | 'delete' | 'delivered' | 'download'

function AttachmentCard({
  orderId,
  itemId,
  attachment,
  delivered,
  onChanged,
}: {
  orderId: string
  itemId: string | null
  attachment: OrderAttachment
  delivered: boolean
  onChanged: () => void
}) {
  const [caption, setCaption] = useState(attachment.caption)
  const [busy, setBusy] = useState<Busy | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const name = attachment.caption || attachment.filename
  const image = attachmentImageUrl(orderId, attachment)
  const content = api.orderAttachmentUrl(orderId, attachment.id)
  // The thumbnail, then the file itself (a photo the server could not shrink), then an icon.
  const [failures, setFailures] = useState(0)
  const shown = image === null ? null : failures === 0 ? image : failures === 1 && image !== content ? content : null

  async function act(what: Busy, call: () => Promise<unknown>) {
    setBusy(what)
    setError(null)
    try {
      await call()
      if (what !== 'download') onChanged()
      if (what === 'delete') setConfirming(false)
    } catch (caught) {
      setError(failure(caught))
    } finally {
      setBusy(null)
    }
  }

  const preview =
    attachment.kind === 'video' ? (
      <video src={content} controls playsInline preload="metadata" aria-label={name} className="aspect-video w-full rounded-t-xl bg-black" />
    ) : shown ? (
      <img
        key={shown}
        src={shown}
        alt={name}
        loading="lazy"
        className="aspect-video w-full rounded-t-xl bg-muted object-contain"
        onError={() => setFailures((n) => n + 1)}
      />
    ) : (
      <span role="img" aria-label={attachment.filename} className="flex h-20 w-full items-center justify-center rounded-t-xl bg-muted text-muted-foreground">
        {attachment.kind === 'document' ? <FileTextIcon aria-hidden="true" className="size-8" /> : <FileIcon aria-hidden="true" className="size-8" />}
      </span>
    )

  return (
    <li aria-label={name} className="min-w-0" data-testid={`attachment-${attachment.id}`}>
      <Card size="sm" className={cn('h-full gap-2 pt-0', delivered && 'ring-2 ring-primary')}>
        {preview}
        <div className="flex min-w-0 flex-col gap-2 px-3">
          <span className="sb-num truncate text-[11px] text-muted-foreground" title={attachment.filename}>
            {attachment.filename} · {formatBytes(attachment.size_bytes)}
            {delivered && <span className="ml-1 font-medium text-primary">· Delivered</span>}
          </span>
          <div className="flex gap-1.5">
            <Select value={attachment.role} disabled={busy !== null} onValueChange={(value) => void act('role', () => api.updateOrderAttachment(orderId, attachment.id, { role: value as OrderAttachmentRole }))}>
              <SelectTrigger size="sm" aria-label={`Role of ${attachment.filename}`} className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                {ROLES.map((r) => (
                  <SelectItem key={r} value={r}>
                    {ROLE_LABELS[r]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <form
            className="flex gap-1.5"
            onSubmit={(event) => {
              event.preventDefault()
              void act('caption', () => api.updateOrderAttachment(orderId, attachment.id, { caption: caption.trim() }))
            }}
          >
            <Input
              value={caption}
              onChange={(event) => setCaption(event.target.value)}
              aria-label={`Caption for ${attachment.filename}`}
              placeholder="Caption"
              maxLength={MAX_CAPTION_CHARS}
              className="h-7 min-w-0 flex-1"
            />
            {caption.trim() !== attachment.caption && (
              <Button size="sm" type="submit" disabled={busy !== null}>
                {busy === 'caption' && <Spinner />}
                Save
              </Button>
            )}
          </form>
          <div className="flex flex-wrap items-center gap-1.5">
            {OPENS.has(attachment.media_type) && (
              <Button asChild variant="outline" size="sm">
                <a href={content} {...NEW_TAB}>
                  <ExternalLinkIcon aria-hidden="true" />
                  Open
                </a>
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              disabled={busy !== null}
              aria-label={`Download ${attachment.filename}`}
              onClick={() =>
                void act('download', () =>
                  downloadBlob(async () => {
                    const response = await fetch(api.orderAttachmentUrl(orderId, attachment.id, true))
                    if (!response.ok) throw new Error(`The download failed (${response.status}).`)
                    return response.blob()
                  }, attachment.filename),
                )
              }
            >
              {busy === 'download' ? <Spinner /> : <DownloadIcon aria-hidden="true" />}
              Download
            </Button>
            {itemId && image && (
              <Button
                variant={delivered ? 'secondary' : 'outline'}
                size="sm"
                disabled={busy !== null}
                aria-pressed={delivered}
                onClick={() => void act('delivered', () => api.setItemDelivered(orderId, itemId, delivered ? null : attachment.id))}
              >
                {busy === 'delivered' ? <Spinner /> : <PackageCheckIcon aria-hidden="true" />}
                {delivered ? 'Delivered (unmark)' : 'Mark as delivered'}
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
              onClick={() => {
                setError(null)
                setConfirming(true)
              }}
            >
              <Trash2Icon aria-hidden="true" />
              Remove
            </Button>
          </div>
          {error && !confirming && (
            <p role="alert" className="text-xs text-warn">
              {error}
            </p>
          )}
        </div>
      </Card>
      <AlertDialog open={confirming} onOpenChange={(next) => !next && busy !== 'delete' && setConfirming(false)}>
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>{`Remove ${name}?`}</AlertDialogTitle>
            <AlertDialogDescription>
              For a file added by mistake. The order's history records the removal{delivered ? ', and the item loses its delivered photo' : ''}.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy === 'delete'}>Cancel</AlertDialogCancel>
            <Button variant="destructive" disabled={busy !== null} onClick={() => void act('delete', () => api.deleteOrderAttachment(orderId, attachment.id))}>
              {busy === 'delete' && <Spinner />}
              Remove
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </li>
  )
}
