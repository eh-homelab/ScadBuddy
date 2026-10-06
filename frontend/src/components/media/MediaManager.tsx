import { useEffect, useEffectEvent, useRef, useState, type DragEvent } from 'react'
import { api } from '../../api/client'
import type { MediaView, ModelSummary } from '../../api/types'
import {
  MEDIA_ACCEPT,
  failure,
  mediaProblem,
  pastedMedia,
  takesText,
  useUploadLimit,
} from '../../lib/mediaFiles'
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

/** What Move up/down and dragging reorder: everything of mine's; on a built-in what
 * was added to it, less its chosen cover, which is listed first whatever its place. */
function movableOf(model: ModelSummary) {
  const cover = model.origin === 'builtin' ? (model.media_cover ?? null) : null
  return (model.media ?? []).filter((item) => !item.readonly && item.id !== cover)
}

/** A row action that was pressed: the item's id and the button's name. */
interface Pressed {
  id: string
  action: string
}

/** The row actions focus may land on after a move, in order of preference. */
const ROW_ACTIONS = ['Move up', 'Move down', 'Make cover', 'Delete']

/** Mounted managers, oldest first: a paste aimed at none of them goes to the newest. */
const pasteTargets: object[] = []

/**
 * #279 — a template's images and videos, in order (the first is the cover), with
 * everything to change them: add (picker or drop), reorder (drag, or Move up/down
 * from the keyboard), make cover, caption, delete. Each change is its own write and
 * the list shown is always the one the server answered with.
 *
 * #722 — a built-in takes media too. What it ships comes first, marked Shipped and
 * read-only; what was added after it changes as a template of mine's does, moved
 * among itself. Any item can be made its cover, which is a choice of its own
 * (`media_cover`) rather than a position, and can be put back to the shipped one.
 *
 * A pasted image or video is added as an upload is (#722): anywhere in the section
 * while it has focus or the pointer, and from anywhere on the page while no text
 * field has focus, since a paste into a field is the field's own. With more than one
 * manager mounted, that page-wide paste goes to the newest only.
 */
export function MediaManager({ model: initial, onChanged }: Props) {
  const [model, setModel] = useState(initial)
  // A newer record from the page (a change made elsewhere, #269) replaces this one.
  const [given, setGiven] = useState(initial)
  if (initial !== given) {
    setGiven(initial)
    setModel(initial)
  }
  const [errors, setErrors] = useState<string[]>([])
  const [uploads, setUploads] = useState<Upload[]>([])
  const [confirming, setConfirming] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<number | null>(null)
  const [dragging, setDragging] = useState(false)
  const dragged = useRef<number | null>(null)
  const queue = useRef<Promise<void>>(Promise.resolve())
  const uploadKey = useRef(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const sectionRef = useRef<HTMLElement>(null)
  const hovered = useRef(false)
  // The newest record, for a queued write to act on when its turn comes.
  const latest = useRef(model)
  useEffect(() => {
    latest.current = model
  }, [model])
  // #1321 — the row and action a move that just landed came from, so focus can follow it.
  const refocus = useRef<Pressed | null>(null)
  const uploadLimit = useUploadLimit()

  const slug = model.slug
  const media = model.media ?? []
  const builtin = model.origin === 'builtin'
  const chosenCover = builtin ? (model.media_cover ?? null) : null
  const movable = movableOf(model)
  // Reordering while an upload is in flight could land an order without the new item.
  // A write in flight does not lock anything (#1321): the next one waits its turn.
  const locked = uploads.length > 0

  function landed(next: ModelSummary) {
    latest.current = next
    setModel(next)
    onChanged?.(next)
  }

  /**
   * Runs a write after the ones before it (#1321): a caption saved on blur and the
   * click that blurred it both happen, in that order, instead of the click being
   * dropped on a disabled button. `action` gets the newest record when its turn
   * comes and answers null to do nothing; `pressed` is the row action it came from.
   */
  function write(action: (current: ModelSummary) => Promise<ModelSummary> | null, pressed?: Pressed) {
    setErrors([])
    queue.current = queue.current.then(async () => {
      try {
        const pending = action(latest.current)
        if (!pending) return
        const next = await pending
        if (pressed) refocus.current = pressed
        landed(next)
      } catch (caught) {
        setErrors([failure(caught)])
      }
    })
  }

  /** Moves the item `id` to `to` among the movable items, as they are when its turn comes. */
  function move(id: string, to: number | ((from: number) => number), pressed?: Pressed) {
    write((current) => {
      const ids = movableOf(current).map((item) => item.id)
      const from = ids.indexOf(id)
      const place = typeof to === 'number' ? to : to(from)
      if (from < 0 || from === place || place < 0 || place >= ids.length) return null
      ids.splice(from, 1)
      ids.splice(place, 0, id)
      // A built-in's order names every added item; its shipped ones keep their place.
      const cover =
        current.origin === 'builtin'
          ? (current.media ?? []).find((item) => item.id === current.media_cover && !item.readonly)
          : undefined
      return api.reorderMedia(slug, cover ? [cover.id, ...ids] : ids)
    }, pressed)
  }

  function makeCover(item: MediaView) {
    const pressed = { id: item.id, action: 'Make cover' }
    if (builtin) write(() => api.setMediaCover(slug, item.id), pressed)
    else move(item.id, 0, pressed)
  }

  // #1321 — after a move the pressed button may be disabled (at an end) or gone (Make
  // cover), which drops focus to <body>; put it back on the row's nearest action.
  useEffect(() => {
    const target = refocus.current
    if (!target) return
    refocus.current = null
    const active = document.activeElement
    if (active && active !== document.body && !(active as HTMLButtonElement).disabled) return
    const row = sectionRef.current?.querySelector(`li[data-media-id="${target.id}"]`)
    if (!row) return
    const buttons = Array.from(row.querySelectorAll('button')).filter((button) => !button.disabled)
    const named = (name: string) => buttons.find((button) => button.textContent === name)
    const next =
      named(target.action) ?? ROW_ACTIONS.map(named).find(Boolean) ?? buttons[0]
    next?.focus()
  }, [model])

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

  const pasteTarget = useRef({})
  const onPaste = useEffectEvent((event: ClipboardEvent) => {
    if (event.defaultPrevented) return
    const files = pastedMedia(event.clipboardData)
    if (files.length === 0) return
    const section = sectionRef.current
    const active = document.activeElement
    const here =
      hovered.current ||
      Boolean(section?.contains(active)) ||
      (event.target instanceof Node && Boolean(section?.contains(event.target)))
    if (!here && (takesText(active) || pasteTargets.at(-1) !== pasteTarget.current)) return
    event.preventDefault()
    void add(files)
  })

  useEffect(() => {
    const me = pasteTarget.current
    pasteTargets.push(me)
    const listener = (event: ClipboardEvent) => onPaste(event)
    document.addEventListener('paste', listener)
    return () => {
      document.removeEventListener('paste', listener)
      pasteTargets.splice(pasteTargets.indexOf(me), 1)
    }
  }, [])

  function onDropFiles(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    void add(Array.from(event.dataTransfer?.files ?? []))
  }

  function rowDrag(item: MediaView) {
    const index = movable.indexOf(item)
    if (index < 0) return {}
    return {
      draggable: !locked,
      onDragStart: (event: DragEvent<HTMLLIElement>) => {
        dragged.current = index
        event.dataTransfer?.setData('text/plain', item.id)
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
        const id = from !== null ? movable[from]?.id : undefined
        if (id) move(id, index)
      },
      onDragEnd: () => {
        dragged.current = null
        setDropTarget(null)
      },
    }
  }

  return (
    <section
      ref={sectionRef}
      aria-label="Media"
      className="flex flex-col gap-3 text-[13px]"
      onPointerEnter={() => (hovered.current = true)}
      onPointerLeave={() => (hovered.current = false)}
    >
      {builtin && (
        <p className="rounded-[6px] border border-line bg-surface-2 px-3 py-2 text-[12px] text-muted">
          What this built-in template ships is read-only. Images and videos you add come after
          it, and can be captioned, reordered, removed or made the cover.
        </p>
      )}

      {media.length === 0 ? (
        <p className="text-muted">No images or videos yet.</p>
      ) : (
        <ol aria-label="Media items" className="flex flex-col gap-2">
          {media.map((item, index) => {
            const place = movable.indexOf(item)
            const editable = !item.readonly
            return (
              <li
                key={item.id}
                data-media-id={item.id}
                {...rowDrag(item)}
                className={`flex items-start gap-3 rounded-[6px] border bg-surface-2 p-2 ${
                  place >= 0 && dropTarget === place ? 'border-accent' : 'border-line'
                } ${place >= 0 ? 'cursor-grab' : ''}`}
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
                    {item.readonly && (
                      <span
                        className="rounded-[6px] bg-surface-3 px-1.5 py-0.5 text-[11px] text-muted"
                        title="Shipped with the template; it cannot be changed"
                      >
                        Shipped
                      </span>
                    )}
                  </div>
                  {item.missing ? (
                    <div className="flex items-center gap-2 text-warn">
                      <span>File missing{editable ? ' —' : ''}</span>
                      {editable && (
                        <Button
                          size="sm"
                          variant="danger"
                          disabled={locked}
                          onClick={() => write(() => api.deleteMedia(slug, item.id))}
                        >
                          Remove
                        </Button>
                      )}
                    </div>
                  ) : !editable ? (
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
                          write(() => api.patchMedia(slug, item.id, caption))
                        }
                      }}
                    />
                  )}
                  {editable && confirming === item.id && (
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
                          write(() => api.deleteMedia(slug, item.id))
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
                {!item.missing && (editable || builtin) && (
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    {place >= 0 && (
                      <div className="flex gap-1">
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={locked || place === 0}
                          onClick={() => move(item.id, (from) => from - 1, { id: item.id, action: 'Move up' })}
                        >
                          Move up
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={locked || place === movable.length - 1}
                          onClick={() => move(item.id, (from) => from + 1, { id: item.id, action: 'Move down' })}
                        >
                          Move down
                        </Button>
                      </div>
                    )}
                    <div className="flex gap-1">
                      {index > 0 && (
                        <Button size="sm" disabled={locked} onClick={() => makeCover(item)}>
                          Make cover
                        </Button>
                      )}
                      {index === 0 && chosenCover !== null && (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={locked}
                          onClick={() => write(() => api.setMediaCover(slug, null))}
                        >
                          Use the shipped cover
                        </Button>
                      )}
                      {editable && (
                        <Button
                          size="sm"
                          variant="danger"
                          disabled={locked}
                          onClick={() => setConfirming(item.id)}
                        >
                          Delete
                        </Button>
                      )}
                    </div>
                  </div>
                )}
              </li>
            )
          })}
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
        <p className="text-[12px] text-faint" data-testid="media-paste-hint">
          or paste an image or video
        </p>
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
