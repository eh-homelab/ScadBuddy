import { useRef, useState, type DragEvent } from 'react'
import { api } from '../../api/client'
import type { MediaView, ModelSummary } from '../../api/types'
import { MEDIA_ACCEPT, failure, mediaProblem, useUploadLimit } from '../../lib/mediaFiles'
import { DuplicateModelButton } from '../DuplicateModelButton'
import { Button } from '../ui/Button'

interface Props {
  model: ModelSummary
  /** Called with the record each write answers with. */
  onChanged?: (model: ModelSummary) => void
}

interface Upload {
  key: number
  name: string
  fraction: number
}

const FIELD =
  'w-full rounded-[6px] border border-line bg-surface-2 px-2 py-1 text-[13px] text-ink outline-none focus:border-accent'

/**
 * #279 — a template's images and videos, in order (the first is the cover), with
 * everything to change them: add (picker or drop), reorder (drag, or Move up/down
 * from the keyboard), make cover, caption, delete. Each change is its own write and
 * the list shown is always the one the server answered with. A built-in's media is
 * shown as it is, with Duplicate as the way to change it.
 */
export function MediaManager({ model: initial, onChanged }: Props) {
  const [model, setModel] = useState(initial)
  // A newer record from the page (a change made elsewhere, #269) replaces this one.
  const [given, setGiven] = useState(initial)
  if (initial !== given) {
    setGiven(initial)
    setModel(initial)
  }
  const [busy, setBusy] = useState(false)
  const [errors, setErrors] = useState<string[]>([])
  const [uploads, setUploads] = useState<Upload[]>([])
  const [confirming, setConfirming] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<number | null>(null)
  const [dragging, setDragging] = useState(false)
  const dragged = useRef<number | null>(null)
  const queue = useRef<Promise<void>>(Promise.resolve())
  const uploadKey = useRef(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const uploadLimit = useUploadLimit()

  const slug = model.slug
  const media = model.media ?? []
  const readOnly = model.origin === 'builtin'
  // Reordering while an upload is in flight could land an order without the new item.
  const locked = busy || uploads.length > 0

  function landed(next: ModelSummary) {
    setModel(next)
    onChanged?.(next)
  }

  async function write(action: () => Promise<ModelSummary>) {
    setBusy(true)
    setErrors([])
    try {
      landed(await action())
    } catch (caught) {
      setErrors([failure(caught)])
    } finally {
      setBusy(false)
    }
  }

  function move(from: number, to: number) {
    if (from === to || to < 0 || to >= media.length) return
    const ids = media.map((item) => item.id)
    const [id] = ids.splice(from, 1)
    ids.splice(to, 0, id!)
    void write(() => api.reorderMedia(slug, ids))
  }

  async function add(files: File[]) {
    if (files.length === 0) return
    const limit = await uploadLimit()
    const refused: string[] = []
    const accepted: File[] = []
    for (const file of files) {
      const problem = mediaProblem(file, limit)
      if (problem) refused.push(problem)
      else accepted.push(file)
    }
    setErrors(refused)
    // One after another, in the order given: the server appends each as the last item.
    for (const file of accepted) {
      const key = ++uploadKey.current
      setUploads((all) => [...all, { key, name: file.name, fraction: 0 }])
      queue.current = queue.current.then(async () => {
        try {
          landed(
            await api.uploadMedia(slug, file, {}, (fraction) =>
              setUploads((all) =>
                all.map((entry) => (entry.key === key ? { ...entry, fraction } : entry)),
              ),
            ),
          )
        } catch (caught) {
          setErrors((all) => [...all, `${file.name} was not added: ${failure(caught)}`])
        } finally {
          setUploads((all) => all.filter((entry) => entry.key !== key))
        }
      })
    }
  }

  function onDropFiles(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    void add(Array.from(event.dataTransfer?.files ?? []))
  }

  function rowDrag(index: number) {
    if (readOnly) return {}
    return {
      draggable: !locked,
      onDragStart: (event: DragEvent<HTMLLIElement>) => {
        dragged.current = index
        event.dataTransfer?.setData('text/plain', media[index]?.id ?? '')
        if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
      },
      onDragOver: (event: DragEvent<HTMLLIElement>) => {
        if (dragged.current === null) return
        event.preventDefault()
        setDropTarget(index)
      },
      onDragLeave: () => setDropTarget((target) => (target === index ? null : target)),
      onDrop: (event: DragEvent<HTMLLIElement>) => {
        event.preventDefault()
        const from = dragged.current
        dragged.current = null
        setDropTarget(null)
        if (from !== null) move(from, index)
      },
      onDragEnd: () => {
        dragged.current = null
        setDropTarget(null)
      },
    }
  }

  return (
    <section aria-label="Media" className="flex flex-col gap-3 text-[13px]">
      {readOnly && (
        <div className="flex items-center justify-between gap-3 rounded-[6px] border border-line bg-surface-2 px-3 py-2 text-[12px] text-muted">
          <span>
            Built-in media is read-only. Duplicate the template to add, reorder or remove its
            images and videos.
          </span>
          <DuplicateModelButton slug={slug} name={model.name} primary />
        </div>
      )}

      {media.length === 0 ? (
        <p className="text-muted">No images or videos yet.</p>
      ) : (
        <ol aria-label="Media items" className="flex flex-col gap-2">
          {media.map((item, index) => (
            <li
              key={item.id}
              data-media-id={item.id}
              {...rowDrag(index)}
              className={`flex items-start gap-3 rounded-[6px] border bg-surface-2 p-2 ${
                dropTarget === index ? 'border-accent' : 'border-line'
              } ${readOnly ? '' : 'cursor-grab'}`}
            >
              <Thumb slug={slug} item={item} index={index} />
              <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                <div className="flex items-center gap-2 text-[12px] text-faint">
                  <span>{item.kind === 'video' ? 'Video' : 'Image'}</span>
                  {index === 0 && (
                    <span className="rounded-[6px] bg-accent/12 px-1.5 py-0.5 text-[11px] text-accent">
                      Cover
                    </span>
                  )}
                </div>
                {item.missing ? (
                  <div className="flex items-center gap-2 text-warn">
                    <span>File missing{readOnly ? '' : ' —'}</span>
                    {!readOnly && (
                      <Button
                        size="sm"
                        variant="danger"
                        disabled={locked}
                        onClick={() => void write(() => api.deleteMedia(slug, item.id))}
                      >
                        Remove
                      </Button>
                    )}
                  </div>
                ) : readOnly ? (
                  item.caption && <p className="text-ink">{item.caption}</p>
                ) : (
                  <input
                    // Remounted when the saved caption changes, so it shows what was saved.
                    key={item.caption}
                    aria-label="Caption"
                    placeholder="Caption"
                    className={FIELD}
                    defaultValue={item.caption}
                    onBlur={(event) => {
                      const caption = event.target.value
                      if (caption !== item.caption) {
                        void write(() => api.patchMedia(slug, item.id, caption))
                      }
                    }}
                  />
                )}
                {!readOnly && confirming === item.id && (
                  <div className="flex flex-wrap items-center gap-2 text-[12px]">
                    <span className="text-warn">
                      Delete this {item.kind}? It cannot be undone.
                    </span>
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={locked}
                      onClick={() => {
                        setConfirming(null)
                        void write(() => api.deleteMedia(slug, item.id))
                      }}
                    >
                      Yes, delete
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>
                      Cancel
                    </Button>
                  </div>
                )}
              </div>
              {!readOnly && !item.missing && (
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <div className="flex gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={locked || index === 0}
                      onClick={() => move(index, index - 1)}
                    >
                      Move up
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={locked || index === media.length - 1}
                      onClick={() => move(index, index + 1)}
                    >
                      Move down
                    </Button>
                  </div>
                  <div className="flex gap-1">
                    {index > 0 && (
                      <Button size="sm" disabled={locked} onClick={() => move(index, 0)}>
                        Make cover
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={locked}
                      onClick={() => setConfirming(item.id)}
                    >
                      Delete
                    </Button>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ol>
      )}

      {uploads.length > 0 && (
        <ul aria-label="Uploads" className="flex flex-col gap-1.5">
          {uploads.map((upload) => (
            <li key={upload.key} className="flex items-center gap-2 text-[12px]">
              <span className="sb-num min-w-0 flex-1 truncate">{upload.name}</span>
              <div
                role="progressbar"
                aria-label={`Uploading ${upload.name}`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(upload.fraction * 100)}
                className="h-1.5 w-32 overflow-hidden rounded-full bg-surface-3"
              >
                <div
                  className="h-full bg-accent"
                  style={{ width: `${Math.round(upload.fraction * 100)}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
      )}

      {!readOnly && (
        <div
          data-testid="media-dropzone"
          onDragOver={(event) => {
            // Only files: a row dragged out of the list is not an upload.
            if (!event.dataTransfer?.types.includes('Files')) return
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDropFiles}
          className={`flex flex-col items-center gap-2 rounded-[6px] border border-dashed px-4 py-5 text-center transition-colors ${
            dragging ? 'border-accent bg-accent/8' : 'border-line-strong bg-surface-2'
          }`}
        >
          <p className="text-muted">Drop images or videos here</p>
          <Button size="sm" onClick={() => inputRef.current?.click()}>
            Choose files
          </Button>
          <p className="text-[12px] text-faint">PNG, JPEG or WebP images; MP4 or WebM videos</p>
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={MEDIA_ACCEPT}
            className="sr-only"
            aria-label="Add images or videos"
            onChange={(event) => {
              void add(Array.from(event.target.files ?? []))
              event.target.value = ''
            }}
          />
        </div>
      )}

      {errors.length > 0 && (
        <div role="alert" className="flex flex-col gap-1 text-[13px] text-warn">
          {errors.map((message, index) => (
            <p key={index}>{message}</p>
          ))}
        </div>
      )}
    </section>
  )
}

/** A small preview of one item: the image, a video's poster, or a plain tile. */
function Thumb({ slug, item, index }: { slug: string; item: MediaView; index: number }) {
  const tile =
    'flex h-14 w-20 shrink-0 items-center justify-center overflow-hidden rounded-[4px] bg-surface-3 text-[11px] text-faint'
  if (item.missing) return <div className={tile}>Missing</div>
  const src = item.kind === 'image' ? api.mediaUrl(slug, item) : api.mediaPosterUrl(slug, item)
  if (!src) return <div className={tile}>Video</div>
  const kind = item.kind === 'video' ? 'Video' : 'Image'
  return (
    <div className={tile}>
      <img
        src={src}
        alt={item.caption || `${kind} ${index + 1}`}
        draggable={false}
        loading="lazy"
        className="h-full w-full object-cover"
      />
    </div>
  )
}
