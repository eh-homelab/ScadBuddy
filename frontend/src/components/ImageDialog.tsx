import { useEffect, useState } from 'react'
import type { ModelSummary } from '../api/types'
import { copyImage } from '../lib/clipboard'
import { DownloadBlockedError, downloadBlob } from '../lib/embed'
import { addMedia, failure, fileStamp, makeCover, mediaProblem, useUploadLimit } from '../lib/mediaFiles'
import { framedSize, type CameraView } from '../lib/framing'
import { snapshotSize, type SnapshotOptions } from '../lib/snapshot'
import { FramingSurface } from './FramingSurface'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

/** Pixels per CSS pixel of the viewer, offered as the image's size. */
const IMAGE_SCALES = [2, 3, 4] as const

/** #722 — the image's shape: the viewer's own, or a fixed one. */
const ASPECTS = [
  { key: 'view', label: 'As the viewer', ratio: null },
  { key: '1:1', label: 'Square', ratio: 1 },
  { key: '4:3', label: '4:3', ratio: 4 / 3 },
  { key: '16:9', label: '16:9', ratio: 16 / 9 },
] as const
type AspectKey = (typeof ASPECTS)[number]['key']

interface Props {
  open: boolean
  /** Used in the file name. */
  slug: string
  captureImage: (options: SnapshotOptions) => Promise<Blob | null>
  viewSize: () => { width: number; height: number }
  /**
   * #722 — the viewer's camera now. The dialog frames a copy of it, so the image can
   * be turned, slid and zoomed without moving the viewer. Without it the image is the
   * viewer's own framing.
   */
  cameraView?: () => CameraView | null
  /** The template, for adding the image to its media; unknown until it loads. */
  model?: ModelSummary
  /** Called with the record after the image was added to the template's media. */
  onMediaChanged?: (model: ModelSummary) => void
  onClose: () => void
}

/**
 * A high-resolution image of the preview to share, framed from the viewer's camera and
 * then by hand in the dialog's own preview (#722). The
 * bounding box outline is always left out; the build plate and the background are the
 * user's choice. Besides saving or copying it, it can be added to the template's
 * media, where it can be the cover and is offered by every media picker.
 */
export function ImageDialog({
  open,
  slug,
  captureImage,
  viewSize,
  cameraView,
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
  // The dialog's own copy of the camera, taken as it opens.
  const [pose, setPose] = useState<CameraView | null>(null)
  const [aspectKey, setAspectKey] = useState<AspectKey>('view')
  const [wasOpen, setWasOpen] = useState(false)
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) setPose(cameraView?.() ?? null)
  }

  const size = open ? viewSize() : { width: 0, height: 0 }
  // Null: the viewer's own shape, drawn at the viewer's own size.
  const aspect = ASPECTS.find((option) => option.key === aspectKey)?.ratio ?? null
  const view = pose ? { ...pose, ...(aspect ? { aspect } : {}) } : null
  const frame = view && aspect ? framedSize(size.width, size.height, aspect) : size

  // A small image of the same framing with the same choices, redrawn as they change;
  // once a frame at most, so a drag draws only the latest.
  useEffect(() => {
    if (!open) return
    let url: string | null = null
    let live = true
    const drawn = requestAnimationFrame(() => {
      void captureImage({ scale: 1, plate, transparent, ...(view ? { view } : {}) }).then((blob) => {
        if (!live || !blob) return
        url = URL.createObjectURL(blob)
        setPreview((previous) => {
          if (previous) URL.revokeObjectURL(previous)
          return url
        })
      })
    })
    return () => {
      live = false
      cancelAnimationFrame(drawn)
    }
    // `view` is rebuilt each render; its parts are the dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, plate, transparent, captureImage, pose, aspect])

  function reset() {
    setPose(cameraView?.() ?? null)
  }

  function close() {
    setError(null)
    setCopied(false)
    setAdded(null)
    setPreview((previous) => {
      if (previous) URL.revokeObjectURL(previous)
      return null
    })
    onClose()
  }

  async function render(): Promise<Blob> {
    const blob = await captureImage({ scale, plate, transparent, ...(view ? { view } : {}) })
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
      const file = new File([blob], `render-${fileStamp()}.png`, { type: 'image/png' })
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
      description={
        view
          ? 'Frame it here without moving the viewer, then draw it at a higher resolution to share.'
          : 'The preview as it is framed now, drawn at a higher resolution to share.'
      }
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
        <FramingSurface
          view={view}
          // The shape is the dialog's own choice, not part of the pose.
          onChange={(next) => setPose({ position: next.position, target: next.target, fov: next.fov })}
          className="mx-auto flex items-center justify-center overflow-hidden rounded-[6px] border border-line outline-none focus-visible:border-accent"
          style={{
            // The image's own shape, no taller than half the window.
            aspectRatio: String(frame.width > 0 && frame.height > 0 ? frame.width / frame.height : 16 / 9),
            width: `min(100%, calc(50vh * ${frame.width > 0 && frame.height > 0 ? frame.width / frame.height : 16 / 9}))`,
            ...(transparent
              ? {
                  backgroundImage:
                    'repeating-conic-gradient(var(--sb-surface-3, #333) 0% 25%, transparent 0% 50%)',
                  backgroundSize: '16px 16px',
                }
              : {}),
          }}
        >
          {preview ? (
            <img
              src={preview}
              alt="What the image will show"
              data-testid="image-preview"
              draggable={false}
              className="pointer-events-none h-full w-full object-contain select-none"
            />
          ) : (
            <Spinner />
          )}
        </FramingSurface>

        {view && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <span className="mr-auto text-[12px] text-faint">
              Drag to orbit · Shift- or right-drag to pan · Scroll or pinch to zoom
            </span>
            <Button size="sm" onClick={reset} disabled={busy} data-testid="image-reset-view">
              Reset to view
            </Button>
          </div>
        )}

        {view && (
          <fieldset>
            <legend className="text-[12px] text-muted">Shape</legend>
            <div className="mt-1.5 flex flex-wrap gap-4">
              {ASPECTS.map((option) => (
                <label key={option.key} className="flex cursor-pointer items-center gap-2 text-[13px]">
                  <input
                    type="radio"
                    name="image-aspect"
                    checked={aspectKey === option.key}
                    onChange={() => setAspectKey(option.key)}
                    className="accent-[var(--sb-accent)]"
                  />
                  {option.label}
                </label>
              ))}
            </div>
          </fieldset>
        )}

        <fieldset>
          <legend className="text-[12px] text-muted">Size</legend>
          <div className="mt-1.5 flex flex-wrap gap-4">
            {IMAGE_SCALES.map((option) => {
              const pixels = snapshotSize(frame.width, frame.height, option)
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
