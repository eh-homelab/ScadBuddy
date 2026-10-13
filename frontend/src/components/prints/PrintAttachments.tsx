import { useState, type DragEvent } from 'react'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { PrintAttachment, PrintDetail } from '../../api/types'
import { formatBytes } from '../../lib/format'
import { MEDIA_ACCEPT, failure, mediaProblem, useUploadLimit } from '../../lib/mediaFiles'
import { modelPath } from '../../lib/deeplink'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'

type Upload = { name: string; fraction: number }

/**
 * #309, #311 — photos and videos of the real finished object. Dropped, chosen, or taken
 * with a phone's camera; each with a caption, removable, and copied into the template's
 * gallery on request. Bambuddy keeps the files (a photo on the print too), ScadBuddy
 * which they are and their captions.
 */
export function PrintAttachments({ print, onChanged }: { print: PrintDetail; onChanged: () => void }) {
  const limit = useUploadLimit()
  const [uploads, setUploads] = useState<Upload[]>([])
  const [errors, setErrors] = useState<string[]>([])
  const [over, setOver] = useState(false)

  async function add(files: File[]) {
    if (files.length === 0) return
    const most = await limit()
    const problems: string[] = []
    const fine: File[] = []
    for (const file of files) {
      const problem = mediaProblem(file, most)
      if (problem) problems.push(problem)
      else fine.push(file)
    }
    setErrors(problems)
    for (const file of fine) {
      setUploads((now) => [...now, { name: file.name, fraction: 0 }])
      try {
        await api.addPrintAttachment(print.archive_id, file, '', (fraction) =>
          setUploads((now) => now.map((upload) => (upload.name === file.name ? { ...upload, fraction } : upload))),
        )
      } catch (caught) {
        setErrors((now) => [...now, `${file.name}: ${failure(caught)}`])
      } finally {
        setUploads((now) => now.filter((upload) => upload.name !== file.name))
      }
    }
    if (fine.length > 0) onChanged()
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
        className={`flex flex-wrap items-center gap-2 rounded-[6px] border border-dashed p-3 text-[13px] text-muted ${
          over ? 'border-accent bg-accent/8' : 'border-line'
        }`}
      >
        <span className="mr-auto">Drop photos or videos of the finished print here, or</span>
        <FilePick label="Take a photo" accept="image/*" capture onPick={(files) => void add(files)} />
        <FilePick label="Choose files" accept={MEDIA_ACCEPT} multiple onPick={(files) => void add(files)} />
      </div>
      {uploads.length > 0 && (
        <ul aria-label="Uploading" className="space-y-1 text-[12px] text-muted">
          {uploads.map((upload) => (
            <li key={upload.name} className="flex items-center gap-2">
              <Spinner /> {upload.name}
              <progress max={1} value={upload.fraction} aria-label={`Uploading ${upload.name}`} className="flex-1" />
            </li>
          ))}
        </ul>
      )}
      {errors.length > 0 && (
        <ul role="alert" className="space-y-0.5 text-[12px] text-warn">
          {errors.map((error) => (
            <li key={error}>{error}</li>
          ))}
        </ul>
      )}
      {attachments.length === 0 ? (
        <p className="text-[13px] text-muted">No photos or videos added yet.</p>
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
  accept,
  capture,
  multiple,
  onPick,
}: {
  label: string
  accept: string
  capture?: boolean
  multiple?: boolean
  onPick: (files: File[]) => void
}) {
  return (
    <label className="inline-flex cursor-pointer items-center rounded-[6px] border border-line px-2.5 py-1 text-[12px] text-ink hover:bg-surface-2 focus-within:ring-2 focus-within:ring-accent">
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
  const [error, setError] = useState<string | null>(null)
  const [promoted, setPromoted] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const name = attachment.caption ?? attachment.filename

  async function act(what: 'caption' | 'delete' | 'promote', call: () => Promise<unknown>) {
    setBusy(what)
    setError(null)
    try {
      await call()
      if (what === 'promote') setPromoted(true)
      else onChanged()
    } catch (caught) {
      setError(failure(caught))
    } finally {
      setBusy(null)
    }
  }

  const changed = caption.trim() !== (attachment.caption ?? '')
  return (
    <li aria-label={name} className="flex flex-col gap-2 rounded-[6px] border border-line p-2">
      {attachment.kind === 'video' ? (
        <video
          src={attachment.url}
          controls
          playsInline
          preload="metadata"
          aria-label={name}
          className="aspect-video w-full rounded-[4px] bg-black"
        />
      ) : (
        <img src={attachment.url} alt={name} className="aspect-video w-full rounded-[4px] object-cover" />
      )}
      <span className="sb-num truncate text-[11px] text-faint" title={attachment.filename}>
        {attachment.filename} · {formatBytes(attachment.size_bytes)}
      </span>
      <form
        className="flex gap-1.5"
        onSubmit={(event) => {
          event.preventDefault()
          void act('caption', () => api.captionPrintAttachment(print.archive_id, attachment.id, caption.trim()))
        }}
      >
        <input
          value={caption}
          onChange={(event) => setCaption(event.target.value)}
          aria-label={`Caption for ${attachment.filename}`}
          placeholder="Caption"
          maxLength={500}
          className="min-w-0 flex-1 rounded-[6px] border border-line bg-surface px-2 py-1 text-[13px]"
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
          <Link to={modelPath(print.slug)} className="text-[12px] text-accent underline">
            See the gallery
          </Link>
        )}
        {confirming ? (
          <>
            <span className="ml-auto text-[12px] text-muted">Delete it from Bambuddy too?</span>
            <Button
              size="sm"
              variant="danger"
              disabled={busy !== null}
              onClick={() => void act('delete', () => api.deletePrintAttachment(print.archive_id, attachment.id))}
            >
              {busy === 'delete' && <Spinner />}
              Delete
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>
              Keep
            </Button>
          </>
        ) : (
          <Button size="sm" variant="ghost" className="ml-auto" onClick={() => setConfirming(true)}>
            Remove
          </Button>
        )}
      </div>
      {error && (
        <p role="alert" className="text-[12px] text-warn">
          {error}
        </p>
      )}
    </li>
  )
}
