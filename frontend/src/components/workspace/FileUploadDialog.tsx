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
 * with the assistant). The output thumbnail, a library upload and a mesh import use it.
 */
export function FileUploadDialog({ open, title, description, onClose, accept, hint, onFile, workspace }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  function close() {
    if (busy) return
    setError(null)
    onClose()
  }

  async function take(file: File) {
    setBusy(true)
    setError(null)
    try {
      await onFile(file)
      setBusy(false)
      onClose()
    } catch (caught) {
      setBusy(false)
      setError(failure(caught))
    }
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    const file = event.dataTransfer.files[0]
    if (file && !busy) void take(file)
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
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
          <Button size="sm" variant="outline" disabled={busy} onClick={() => input.current?.click()}>
            {busy ? <Spinner /> : <UploadIcon />}
            Upload…
          </Button>
          <WorkspacePickButton
            disabled={busy}
            resource={workspace.resource}
            resourceLabel={workspace.label}
            accept={accept}
            onFile={async (file) => {
              await onFile(file)
              setError(null)
              onClose()
            }}
          />
        </div>
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
