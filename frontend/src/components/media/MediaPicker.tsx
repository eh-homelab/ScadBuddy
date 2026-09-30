import { useRef, useState, type DragEvent } from 'react'
import { failure } from '../../lib/mediaFiles'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

export interface PickerItem {
  /** Unique across every section. */
  key: string
  /** The image shown; a plain tile without one. */
  src?: string
  label: string
  /** A short tag over the tile, such as "Cover". */
  badge?: string
}

export interface PickerSection {
  title: string
  items: PickerItem[]
  /** Said when the section has no items; the section is left out without it. */
  empty?: string
}

interface Props {
  open: boolean
  title: string
  description?: string
  onClose: () => void
  sections: PickerSection[]
  /** Still reading what the sections list. */
  loading?: boolean
  /** The key of what is chosen now, marked in the grid. */
  selected?: string
  /** Takes the picked item. The picker closes once it resolves, and shows what it throws. */
  onPick: (item: PickerItem) => Promise<void> | void
  /**
   * Upload as a way to pick: a new file is added where the caller keeps them and
   * chosen, and the picker closes once `onFile` resolves.
   */
  upload?: {
    accept: string
    /** What may be uploaded, e.g. "PNG, JPEG or WebP, up to 10 MB". */
    hint: string
    onFile: (file: File) => Promise<void>
  }
  /** Why uploading is not possible here; replaces the upload zone. */
  uploadBlocked?: string
}

/**
 * One way to choose an image everywhere one is chosen: what already exists (the
 * template's media, its samples, ...) as a grid, with uploading a new one built in.
 * The callers decide what the sections are and what picking or uploading does.
 */
export function MediaPicker({
  open,
  title,
  description,
  onClose,
  sections,
  loading = false,
  selected,
  onPick,
  upload,
  uploadBlocked,
}: Props) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  function close() {
    if (busy) return
    setError(null)
    onClose()
  }

  async function run(what: string, action: () => Promise<void> | void) {
    setBusy(what)
    setError(null)
    try {
      await action()
      setBusy(null)
      onClose()
    } catch (caught) {
      setBusy(null)
      setError(failure(caught))
    }
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    const file = event.dataTransfer.files[0]
    if (file && upload && !busy) void run('upload', () => upload.onFile(file))
  }

  const shown = sections.filter((section) => section.items.length > 0 || section.empty)

  return (
    <Dialog
      open={open}
      title={title}
      description={description}
      onClose={close}
      footer={
        <Button onClick={close} disabled={busy !== null}>
          Cancel
        </Button>
      }
    >
      <div className="flex flex-col gap-4" data-testid="media-picker">
        {upload && !uploadBlocked && (
          <div
            data-testid="media-picker-drop"
            onDragOver={(event) => {
              event.preventDefault()
              setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            className={`flex items-center gap-3 rounded-[6px] border border-dashed px-3 py-2.5 transition-colors ${
              dragging ? 'border-accent bg-accent/10' : 'border-line bg-surface-2/60'
            }`}
          >
            <div className="min-w-0 flex-1 text-[12px]">
              <span className="block text-ink">
                {busy === 'upload' ? 'Uploading…' : 'Drop a file here, or upload one'}
              </span>
              <span className="text-faint">{upload.hint}</span>
            </div>
            <input
              ref={input}
              type="file"
              className="sr-only"
              accept={upload.accept}
              aria-label="Upload a file"
              onChange={(event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (file) void run('upload', () => upload.onFile(file))
              }}
            />
            <Button
              size="sm"
              disabled={busy !== null}
              onClick={() => input.current?.click()}
            >
              {busy === 'upload' && <Spinner />}
              Upload…
            </Button>
          </div>
        )}
        {uploadBlocked && <p className="text-[12px] text-faint">{uploadBlocked}</p>}

        {loading && (
          <p className="flex items-center gap-2 text-[13px] text-muted">
            <Spinner /> Loading
          </p>
        )}

        {!loading &&
          shown.map((section) => (
            <section key={section.title} aria-label={section.title}>
              <h3 className="mb-1.5 text-[11px] tracking-wide text-faint">{section.title}</h3>
              {section.items.length === 0 ? (
                <p className="text-[12px] text-muted">{section.empty}</p>
              ) : (
                <div className="grid grid-cols-[repeat(auto-fill,minmax(96px,1fr))] gap-2">
                  {section.items.map((item) => {
                    const chosen = item.key === selected
                    return (
                      <button
                        key={item.key}
                        type="button"
                        aria-pressed={chosen}
                        aria-label={`Choose ${item.label}`}
                        title={item.label}
                        disabled={busy !== null}
                        onClick={() => void run(item.key, () => onPick(item))}
                        className={`relative flex flex-col gap-1 rounded-[6px] border p-1 text-left text-[11px] transition-colors disabled:opacity-60 ${
                          chosen
                            ? 'border-accent bg-accent/10 text-ink'
                            : 'border-line bg-surface-2/60 text-muted hover:border-accent/60'
                        }`}
                      >
                        <span className="grid aspect-square w-full place-items-center overflow-hidden rounded-[4px] bg-[repeating-conic-gradient(var(--color-surface-3)_0_25%,var(--color-surface-2)_0_50%)] bg-[length:12px_12px]">
                          {busy === item.key ? (
                            <Spinner />
                          ) : item.src ? (
                            <img
                              src={item.src}
                              alt=""
                              loading="lazy"
                              draggable={false}
                              className="size-full object-contain"
                            />
                          ) : (
                            <span className="text-faint">No preview</span>
                          )}
                        </span>
                        <span className="w-full truncate">{item.label}</span>
                        {item.badge && (
                          <span className="absolute top-1.5 left-1.5 rounded-[4px] bg-accent px-1 text-[10px] font-medium text-accent-ink">
                            {item.badge}
                          </span>
                        )}
                      </button>
                    )
                  })}
                </div>
              )}
            </section>
          ))}

        {error && (
          <p role="alert" className="text-[12px] text-warn">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  )
}
