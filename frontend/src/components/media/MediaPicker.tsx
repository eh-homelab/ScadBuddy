import { useRef, useState, type DragEvent } from 'react'
import { failure } from '../../lib/mediaFiles'
import { Spinner } from '../ui/Spinner'
import { UploadIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

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
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl">
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
        {description ? <DialogDescription>{description}</DialogDescription> : <DialogDescription className="sr-only">{title}</DialogDescription>}
      </DialogHeader>
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
            className={cn(
              'flex items-center gap-3 rounded-xl border-2 border-dashed px-3 py-2.5 transition-colors',
              dragging ? 'border-primary bg-primary/10' : 'bg-ui-muted/40',
            )}
          >
            <div className="min-w-0 flex-1 text-xs">
              <span className="block text-foreground">
                {busy === 'upload' ? 'Uploading…' : 'Drop a file here, or upload one'}
              </span>
              <span className="text-muted-foreground">{upload.hint}</span>
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
            <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => input.current?.click()}>
              {busy === 'upload' ? <Spinner /> : <UploadIcon />}
              Upload…
            </Button>
          </div>
        )}
        {uploadBlocked && <p className="text-xs text-muted-foreground">{uploadBlocked}</p>}

        {loading && (
          <div role="status" aria-label="Loading" className="grid grid-cols-[repeat(auto-fill,minmax(96px,1fr))] gap-2">
            {Array.from({ length: 6 }, (_, n) => (
              <Skeleton key={n} className="aspect-square w-full" />
            ))}
          </div>
        )}

        {!loading &&
          shown.map((section) => (
            <section key={section.title} aria-label={section.title}>
              <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">{section.title}</h3>
              {section.items.length === 0 ? (
                <p className="text-xs text-muted-foreground">{section.empty}</p>
              ) : (
                <div className="grid grid-cols-[repeat(auto-fill,minmax(96px,1fr))] gap-2">
                  {section.items.map((item) => {
                    const chosen = item.key === selected
                    return (
                      <Tooltip key={item.key}>
                      <TooltipTrigger asChild>
                      <button
                        type="button"
                        aria-pressed={chosen}
                        aria-label={`Choose ${item.label}`}
                        disabled={busy !== null}
                        onClick={() => void run(item.key, () => onPick(item))}
                        className={cn(
                          'relative flex flex-col gap-1 rounded-lg border p-1 text-left text-xs transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-60',
                          chosen
                            ? 'border-primary bg-primary/10 text-foreground ring-2 ring-primary/40'
                            : 'bg-ui-muted/40 text-muted-foreground hover:border-primary/60',
                        )}
                      >
                        <span className="grid aspect-square w-full place-items-center overflow-hidden rounded-md bg-[repeating-conic-gradient(var(--color-surface-3)_0_25%,var(--color-surface-2)_0_50%)] bg-[length:12px_12px]">
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
                            <span className="text-muted-foreground">No preview</span>
                          )}
                        </span>
                        <span className="w-full truncate">{item.label}</span>
                        {item.badge && (
                          <Badge className="absolute top-1.5 left-1.5 h-4 rounded-sm px-1 text-[10px]">{item.badge}</Badge>
                        )}
                      </button>
                      </TooltipTrigger>
                      <TooltipContent>{item.label}</TooltipContent>
                      </Tooltip>
                    )
                  })}
                </div>
              )}
            </section>
          ))}

        {error && (
          <p role="alert" className="text-xs text-destructive">
            {error}
          </p>
        )}
      </div>
      <DialogFooter>
        <Button variant="outline" onClick={close} disabled={busy !== null}>
          Cancel
        </Button>
      </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
