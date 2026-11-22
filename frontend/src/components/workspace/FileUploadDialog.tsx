import { useRef, useState, type DragEvent } from 'react'
import { UploadIcon } from 'lucide-react'
import type { ResourceRef } from '../../api/types'
import { failure } from '../../lib/mediaFiles'
import { Spinner } from '../ui/spinner'
import { WorkspacePickButton } from './WorkspaceFilePicker'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

interface Props {
  open: boolean
  title: string
  description: string
  onClose: () => void
  /** The file input's `accept`; also what the workspace picker lists. Empty takes any file. */
  accept: string
  /** What may be uploaded, in words: "3MF or STL". */
  hint: string
  /** Takes the file. The dialog closes once it resolves, and shows what it throws. */
  onFile: (file: File) => Promise<void>
  /** #4703 — the workspace picker's default list: the sessions that touched `resource` (every session when null). */
  workspace: { resource: ResourceRef | null; label: string }
}

/**
 * #4703 — one file into a target that has no other choices to offer: dropped, chosen from
 * disk, or picked from an assistant session's workspace (`WorkspacePickButton`, shown only
 * with the assistant). The output thumbnail and a library upload use it; a mesh import
 * uses its drop zone (`FileDropZone`) beside its other sources (#6916).
 */
export function FileUploadDialog({ open, title, description, onClose, accept, hint, onFile, workspace }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function close() {
    if (busy) return
    setError(null)
    onClose()
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <FileDropZone
          accept={accept}
          hint={hint}
          workspace={workspace}
          onBusy={setBusy}
          onError={setError}
          onFile={async (file) => {
            await onFile(file)
            onClose()
          }}
        />
        {error && (
          <p role="alert" className="text-xs text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={close} disabled={busy}>
            Cancel
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The drop zone, "Upload…" and "From a session…": one file taken by `onFile`. Its wait is
 * told to `onBusy`, and what it throws to `onError` (null when a file is taken again).
 */
export function FileDropZone({
  accept,
  hint,
  workspace,
  onFile,
  onBusy,
  onError,
  disabled = false,
}: Pick<Props, 'accept' | 'hint' | 'workspace' | 'onFile'> & {
  onBusy: (busy: boolean) => void
  onError: (error: string | null) => void
  disabled?: boolean
}) {
  const [busy, setBusy] = useState(false)
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const off = busy || disabled

  function wait(on: boolean) {
    setBusy(on)
    onBusy(on)
  }

  async function take(file: File) {
    wait(true)
    onError(null)
    try {
      await onFile(file)
      wait(false)
    } catch (caught) {
      wait(false)
      onError(failure(caught))
    }
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    const file = event.dataTransfer.files[0]
    if (file && !off) void take(file)
  }

  return (
    <div
      data-testid="file-upload-drop"
      onDragOver={(event) => {
        event.preventDefault()
        setDragging(true)
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      className={cn(
        'flex flex-wrap items-center gap-3 rounded-xl border-2 border-dashed px-3 py-2.5 transition-colors',
        dragging ? 'border-primary bg-primary/10' : 'bg-muted/40',
      )}
    >
      <div className="min-w-0 flex-1 text-xs">
        <span className="block text-foreground">{busy ? 'Uploading…' : 'Drop a file here, or choose one'}</span>
        <span className="text-muted-foreground">{hint}</span>
      </div>
      <input
        ref={input}
        type="file"
        className="sr-only"
        accept={accept}
        aria-label="Upload a file"
        onChange={(event) => {
          const file = event.target.files?.[0]
          event.target.value = ''
          if (file) void take(file)
        }}
      />
      <Button size="sm" variant="outline" disabled={off} onClick={() => input.current?.click()}>
        {busy ? <Spinner /> : <UploadIcon />}
        Upload…
      </Button>
      <WorkspacePickButton
        disabled={off}
        resource={workspace.resource}
        resourceLabel={workspace.label}
        accept={accept}
        onFile={async (file) => {
          await onFile(file)
          onError(null)
        }}
      />
    </div>
  )
}
