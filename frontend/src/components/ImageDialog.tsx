import { useEffect, useState } from 'react'
import type { ModelSummary } from '../api/types'
import { copyImage } from '../lib/clipboard'
import { DownloadBlockedError, downloadBlob } from '../lib/embed'
import { addMedia, failure, makeCover, mediaProblem, useUploadLimit } from '../lib/mediaFiles'
import { snapshotSize, type SnapshotOptions } from '../lib/snapshot'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

/** Pixels per CSS pixel of the viewer, offered as the image's size. */
const IMAGE_SCALES = [2, 3, 4] as const

interface Props {
  open: boolean
  /** Used in the file name. */
  slug: string
  captureImage: (options: SnapshotOptions) => Promise<Blob | null>
  viewSize: () => { width: number; height: number }
  /** The template, for adding the image to its media; unknown until it loads. */
  model?: ModelSummary
  /** Called with the record after the image was added to the template's media. */
  onMediaChanged?: (model: ModelSummary) => void
  onClose: () => void
}

/**
 * A high-resolution image of the preview, as the camera sees it now, to share. The
 * bounding box outline is always left out; the build plate and the background are the
 * user's choice. Besides saving or copying it, it can be added to the template's
 * media, where it can be the cover and is offered by every media picker.
 */
export function ImageDialog({
  open,
  slug,
  captureImage,
  viewSize,
  model,
  onMediaChanged,
  onClose,
}: Props) {
  const [scale, setScale] = useState<number>(2)
  const [plate, setPlate] = useState(true)
  const [transparent, setTransparent] = useState(false)
  const [preview, setPreview] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [added, setAdded] = useState<string | null>(null)
  const uploadLimit = useUploadLimit()

  const size = open ? viewSize() : { width: 0, height: 0 }

  // A small image of the same view with the same choices, redrawn as they change.
  useEffect(() => {
    if (!open) return
    let url: string | null = null
    let live = true
    void captureImage({ scale: 1, plate, transparent }).then((blob) => {
      if (!live || !blob) return
      url = URL.createObjectURL(blob)
      setPreview(url)
    })
    return () => {
      live = false
      if (url) URL.revokeObjectURL(url)
    }
  }, [open, plate, transparent, captureImage])

  function close() {
    setError(null)
    setCopied(false)
    setAdded(null)
    setPreview(null)
    onClose()
  }

  async function render(): Promise<Blob> {
    const blob = await captureImage({ scale, plate, transparent })
    if (!blob) throw new Error('The viewer could not draw the image.')
    return blob
  }

  async function save() {
    setBusy(true)
    setError(null)
    try {
      // Through lib/embed, so the file survives Bambuddy's sandboxed frame.
      await downloadBlob(render, `${slug}-render.png`)
    } catch (cause) {
      setError(cause instanceof DownloadBlockedError ? cause.message : 'Could not save the image.')
    } finally {
      setBusy(false)
    }
  }

  /** Adds the image to the template's media, and with `cover` puts it first. */
  async function keep(cover: boolean) {
    if (!model) return
    setBusy(true)
    setError(null)
    setAdded(null)
    try {
      const blob = await render()
      const file = new File([blob], `render-${stamp()}.png`, { type: 'image/png' })
      const problem = mediaProblem(file, await uploadLimit())
      if (problem) throw new Error(`${problem} Choose a smaller size.`)
      const result = await addMedia(slug, file, model.media ?? [])
      let record = result.model
      if (cover && result.id) record = (await makeCover(slug, record.media ?? [], result.id)) ?? record
      onMediaChanged?.(record)
      setAdded(cover ? 'Added to the media as the cover' : 'Added to the media')
    } catch (cause) {
      setError(failure(cause))
    } finally {
      setBusy(false)
    }
  }

  async function copy() {
    setBusy(true)
    setError(null)
    setCopied(false)
    try {
      // Started inside the click, so the browser may ask for clipboard access (#722).
      const result = await copyImage(render())
      if (result.ok) setCopied(true)
      else setError(result.message)
    } finally {
      setBusy(false)
    }
  }

  const readOnly = model?.origin === 'builtin'
  const canCopy = typeof ClipboardItem !== 'undefined' && Boolean(navigator.clipboard?.write)

  return (
    <Dialog
      open={open}
      title="Rendered image"
      description="The preview as it is framed now, drawn at a higher resolution to share."
      onClose={close}
      footer={
        <>
          {error && (
            <span role="alert" className="mr-auto text-[12px] text-warn">
              {error}
            </span>
          )}
          {!error && (copied || added) && (
            <span role="status" className="mr-auto text-[12px] text-ok">
              {added ?? 'Copied'}
            </span>
          )}
          <Button onClick={close} disabled={busy}>
            {added ? 'Done' : 'Cancel'}
          </Button>
          {canCopy && (
            <Button onClick={() => void copy()} disabled={busy} data-testid="image-copy">
              Copy
            </Button>
          )}
          <Button variant="primary" onClick={() => void save()} disabled={busy} data-testid="image-save">
            {busy && <Spinner />}
            Save PNG
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div
          className="flex aspect-video items-center justify-center overflow-hidden rounded-[6px] border border-line"
          style={
            transparent
              ? {
                  backgroundImage:
                    'repeating-conic-gradient(var(--sb-surface-3, #333) 0% 25%, transparent 0% 50%)',
                  backgroundSize: '16px 16px',
                }
              : undefined
          }
        >
          {preview ? (
            <img
              src={preview}
              alt="What the image will show"
              data-testid="image-preview"
              className="max-h-full max-w-full object-contain"
            />
          ) : (
            <Spinner />
          )}
        </div>

        <fieldset>
          <legend className="text-[12px] text-muted">Size</legend>
          <div className="mt-1.5 flex flex-wrap gap-4">
            {IMAGE_SCALES.map((option) => {
              const pixels = snapshotSize(size.width, size.height, option)
              return (
                <label key={option} className="flex cursor-pointer items-center gap-2 text-[13px]">
                  <input
                    type="radio"
                    name="image-scale"
                    checked={scale === option}
                    onChange={() => setScale(option)}
                    className="accent-[var(--sb-accent)]"
                  />
                  {option}×
                  <span className="sb-num text-[12px] text-faint">
                    {pixels.width} × {pixels.height}
                  </span>
                </label>
              )
            })}
          </div>
        </fieldset>

        <div className="flex flex-wrap gap-4">
          <label className="flex cursor-pointer items-center gap-2 text-[13px]">
            <input
              type="checkbox"
              checked={plate}
              onChange={(event) => setPlate(event.target.checked)}
              className="accent-[var(--sb-accent)]"
              data-testid="image-plate"
            />
            Show the build plate
          </label>
          <label className="flex cursor-pointer items-center gap-2 text-[13px]">
            <input
              type="checkbox"
              checked={transparent}
              onChange={(event) => setTransparent(event.target.checked)}
              className="accent-[var(--sb-accent)]"
              data-testid="image-transparent"
            />
            Transparent background
          </label>
        </div>

        <section
          aria-label="Template media"
          className="flex flex-wrap items-center gap-2 border-t border-line pt-3"
        >
          <span className="mr-auto text-[12px] text-muted">
            {readOnly
              ? 'A built-in template’s media cannot change. Duplicate it to keep images with it.'
              : 'Keep it with the template, to use as its cover or in any image picker.'}
          </span>
          <Button
            size="sm"
            onClick={() => void keep(false)}
            disabled={busy || !model || readOnly}
            data-testid="image-add-media"
          >
            Add to media
          </Button>
          <Button
            size="sm"
            onClick={() => void keep(true)}
            disabled={busy || !model || readOnly}
            data-testid="image-add-cover"
          >
            Add as cover
          </Button>
        </section>
      </div>
    </Dialog>
  )
}

/** A sortable, file-name-safe time: 20260929-011530. */
function stamp(): string {
  const now = new Date()
  const two = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}${two(now.getMonth() + 1)}${two(now.getDate())}-${two(now.getHours())}${two(now.getMinutes())}${two(now.getSeconds())}`
}
