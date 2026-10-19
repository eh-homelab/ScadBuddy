import { useRef, useState, type DragEvent, type ReactNode } from 'react'
import { Link } from 'react-router'
import { api, ApiError } from '../../api/client'
import type { PrintAttachment, PrintDetail } from '../../api/types'
import { formatBytes } from '../../lib/format'
import { MEDIA_ACCEPT, failure, mediaProblem, useUploadLimit } from '../../lib/mediaFiles'
import { modelPath } from '../../lib/deeplink'
import { CameraIcon, ImagePlusIcon, Trash2Icon } from 'lucide-react'
import { Hint } from '../Hint'
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
import { cn } from '@/lib/utils'
import { Spinner } from '../ui/spinner'

type Upload = { id: number; name: string; fraction: number }
/** Its own id, as an upload's: two files can fail with the same words (#3114). */
type Refusal = { id: number; text: string }

/** The server's caption limit (`MAX_CAPTION_CHARS`, `library/media.py`). */
const MAX_CAPTION_CHARS = 1000

/**
 * #309, #311 — photos and videos of the real finished object. Dropped, chosen, or taken
 * with a phone's camera; each with a caption, removable, and copied into the template's
 * gallery on request. Bambuddy keeps the files (a photo on the print too), ScadBuddy
 * which they are and their captions.
 */
export function PrintAttachments({ print, onChanged }: { print: PrintDetail; onChanged: () => void }) {
  const limit = useUploadLimit()
  const [uploads, setUploads] = useState<Upload[]>([])
  const nextUpload = useRef(0)
  /** Uploads not yet finished, from every drop and pick. */
  const running = useRef(0)
  const [errors, setErrors] = useState<Refusal[]>([])
  const nextError = useRef(0)
  const [over, setOver] = useState(false)

  async function add(files: File[]) {
    if (files.length === 0) return
    const most = await limit()
    const refusal = (text: string): Refusal => ({ id: (nextError.current += 1), text })
    const problems: Refusal[] = []
    const fine: File[] = []
    for (const file of files) {
      const problem = mediaProblem(file, most)
      if (problem) problems.push(refusal(problem))
      else fine.push(file)
    }
    // A drop while an earlier one still uploads keeps that one's refusals (#3114).
    const earlier = running.current > 0
    setErrors((now) => (earlier ? [...now, ...problems] : problems))
    running.current += fine.length
    for (const file of fine) {
      // Its own id: a phone's camera names every capture alike (image.jpg).
      const id = (nextUpload.current += 1)
      setUploads((now) => [...now, { id, name: file.name, fraction: 0 }])
      try {
        await api.addPrintAttachment(print.archive_id, file, '', (fraction) =>
          setUploads((now) => now.map((upload) => (upload.id === id ? { ...upload, fraction } : upload))),
        )
        // Shown as each lands, not once the last of a batch of large videos has (#3114).
        onChanged()
      } catch (caught) {
        setErrors((now) => [...now, refusal(`${file.name}: ${failure(caught)}`)])
      } finally {
        running.current -= 1
        setUploads((now) => now.filter((upload) => upload.id !== id))
      }
    }
  }

  function dropped(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setOver(false)
    void add([...event.dataTransfer.files])
  }

  const attachments = print.media.attachments
  return (
    <div className="flex flex-col gap-3">
      <div
        data-testid="attach-drop"
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
        <span className="mr-auto">Drop photos or videos of the finished print here, or</span>
        <FilePick
          label="Take a photo"
          icon={<CameraIcon aria-hidden="true" />}
          accept="image/*"
          capture
          onPick={(files) => void add(files)}
        />
        <FilePick
          label="Choose files"
          icon={<ImagePlusIcon aria-hidden="true" />}
          accept={MEDIA_ACCEPT}
          multiple
          onPick={(files) => void add(files)}
        />
      </div>
      {uploads.length > 0 && (
        <ul aria-label="Uploading" className="space-y-1 text-xs text-muted-foreground">
          {uploads.map((upload) => (
            <li key={upload.id} className="flex items-center gap-2">
              <Spinner /> {upload.name}
              <progress
                max={1}
                value={upload.fraction}
                aria-label={`Uploading ${upload.name}`}
                className="h-1.5 flex-1 overflow-hidden rounded-full accent-primary"
              />
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
        <p className="text-[13px] text-muted-foreground">No photos or videos added yet.</p>
      ) : (
        <ul aria-label="Added photos and videos" className="grid gap-3 sm:grid-cols-2">
          {attachments.map((attachment) => (
            <AttachmentItem key={attachment.id} print={print} attachment={attachment} onChanged={onChanged} />
          ))}
        </ul>
      )}
    </div>
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
  accept: string
  capture?: boolean
  multiple?: boolean
  onPick: (files: File[]) => void
}) {
  return (
    // The file input inside it is what takes focus and opens the picker, so the ring
    // follows its focus.
    <Button
      asChild
      variant="outline"
      size="sm"
      className="cursor-pointer has-focus-visible:border-ring has-focus-visible:ring-3 has-focus-visible:ring-ring/50"
    >
      <label>
        {icon}
        {label}
        <input
          type="file"
          aria-label={label}
          accept={accept}
          multiple={multiple}
          // A phone opens its camera rather than the photo library.
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

function AttachmentItem({
  print,
  attachment,
  onChanged,
}: {
  print: PrintDetail
  attachment: PrintAttachment
  onChanged: () => void
}) {
  const [caption, setCaption] = useState(attachment.caption ?? '')
  const [busy, setBusy] = useState<'caption' | 'delete' | 'promote' | null>(null)
  const [error, setError] = useState<{ what: 'caption' | 'delete' | 'promote'; text: string } | null>(null)
  // The print's own record says, once read again; `copied` until then (#3114).
  const [copied, setCopied] = useState(false)
  const promoted = copied || (print.slug !== null && attachment.promoted_to.includes(print.slug))
  const [confirming, setConfirming] = useState(false)
  const name = attachment.caption ?? attachment.filename

  async function act(what: 'caption' | 'delete' | 'promote', call: () => Promise<unknown>) {
    setBusy(what)
    setError(null)
    try {
      await call()
      if (what === 'promote') setCopied(true)
      onChanged()
      if (what === 'delete') setConfirming(false)
    } catch (caught) {
      setError({ what, text: failure(caught) })
      // Copied from another tab, say (a 409): the print read again says so, and the
      // refusal is not shown once it does (#3114).
      if (what === 'promote' && caught instanceof ApiError && caught.status === 409) onChanged()
    } finally {
      setBusy(null)
    }
  }

  const changed = caption.trim() !== (attachment.caption ?? '')
  return (
    <li aria-label={name} className="min-w-0">
      <Card size="sm" className="h-full gap-2 pt-0">
        {attachment.kind === 'video' ? (
          <video
            src={attachment.url}
            controls
            playsInline
            preload="metadata"
            aria-label={name}
            className="aspect-video w-full rounded-t-xl bg-black"
          />
        ) : (
          <img src={attachment.url} alt={name} className="aspect-video w-full object-cover" />
        )}
        <div className="flex min-w-0 flex-col gap-2 px-3">
          <Hint content={attachment.filename}>
            <span className="sb-num truncate text-[11px] text-muted-foreground">
              {attachment.filename} · {formatBytes(attachment.size_bytes)}
            </span>
          </Hint>
          <form
            className="flex gap-1.5"
            onSubmit={(event) => {
              event.preventDefault()
              void act('caption', () => api.captionPrintAttachment(print.archive_id, attachment.id, caption.trim()))
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
            {changed && (
              <Button size="sm" type="submit" disabled={busy !== null}>
                {busy === 'caption' && <Spinner />}
                Save
              </Button>
            )}
          </form>
          <div className="flex flex-wrap items-center gap-1.5">
            {print.slug && (
              <Button
                variant="outline"
                size="sm"
                disabled={busy !== null || promoted}
                onClick={() =>
                  void act('promote', () => api.promotePrintAttachment(print.archive_id, attachment.id, print.slug!))
                }
              >
                {busy === 'promote' && <Spinner />}
                {promoted ? 'Added to the gallery' : 'Add to the template’s gallery'}
              </Button>
            )}
            {promoted && print.slug && (
              <Button asChild variant="link" size="sm" className="px-1">
                <Link to={modelPath(print.slug)}>See the gallery</Link>
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
          {error && !confirming && !(error.what === 'promote' && promoted) && (
            <p role="alert" className="text-xs text-warn">
              {error.text}
            </p>
          )}
        </div>
      </Card>
      <AlertDialog open={confirming} onOpenChange={(next) => !next && busy !== 'delete' && setConfirming(false)}>
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>{`Remove ${name}?`}</AlertDialogTitle>
            <AlertDialogDescription>Delete it from ScadBuddy and Bambuddy?</AlertDialogDescription>
          </AlertDialogHeader>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error.text}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy === 'delete'}>Cancel</AlertDialogCancel>
            <Button
              variant="destructive"
              disabled={busy !== null}
              onClick={() => void act('delete', () => api.deletePrintAttachment(print.archive_id, attachment.id))}
            >
              {busy === 'delete' && <Spinner />}
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </li>
  )
}
