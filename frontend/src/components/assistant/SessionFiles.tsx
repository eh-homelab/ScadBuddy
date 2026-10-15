import { useEffect, useRef, useState, type DragEvent } from 'react'
import { api, ApiError } from '../../api/client'
import type { WorkspaceFile } from '../../api/types'
import { DownloadBlockedError, downloadBlob } from '../../lib/embed'
import { formatBytes, timeAgo } from '../../lib/format'
import { fileReference, sourceLabel, uploadPath } from '../../lib/workspaceFiles'
import { useAsync } from '../../lib/useAsync'
import { DownloadIcon, EyeIcon, Trash2Icon, UploadIcon } from 'lucide-react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Toggle } from '@/components/ui/toggle'
import { Tip } from './Tip'

/** Types the panel previews inline: the four the Messages API reads. SVG never (it can run script). */
const PREVIEWABLE = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])

interface Props {
  sessionId: string
  /** Changes when the workspace may have changed (a tool call finished, a `workspace.file` event), so the list reads again. */
  refreshKey?: unknown
  /** The browser user owns the session and it is not archived: uploads and deletes are offered. */
  canWrite: boolean
  /** Adds text to the composer (an upload's `[workspace file <path>]`). */
  onInsert: (text: string) => void
}

/** #2329 — the session view's Files: the assistant's workspace for this session. */
export function SessionFiles({ sessionId, refreshKey, canWrite, onInsert }: Props) {
  const { data, error, refresh, setData } = useAsync(() => api.listAiSessionFiles(sessionId), [sessionId])
  const readAt = useRef(refreshKey)
  useEffect(() => {
    if (Object.is(readAt.current, refreshKey)) return
    readAt.current = refreshKey
    refresh()
  }, [refreshKey, refresh])

  const picker = useRef<HTMLInputElement>(null)
  const [dropping, setDropping] = useState(false)
  const [uploading, setUploading] = useState(0)
  const [errors, setErrors] = useState<string[]>([])
  const [confirming, setConfirming] = useState<string | null>(null)
  /** The Delete that asked for the confirm: focus goes back to it on Cancel. */
  const deleteButton = useRef<HTMLElement | null>(null)
  const [preview, setPreview] = useState<{ path: string; url: string } | null>(null)
  const [announcement, setAnnouncement] = useState('')

  // The object URL of a preview lives until the preview closes or the panel goes.
  useEffect(() => () => {
    if (preview) URL.revokeObjectURL(preview.url)
  }, [preview])

  const why = (caught: unknown) =>
    caught instanceof ApiError ? caught.detail : caught instanceof DownloadBlockedError ? caught.message : 'the assistant service did not answer'

  async function upload(files: File[]) {
    if (files.length === 0 || !canWrite) return
    const taken = new Set((data?.files ?? []).map((f) => f.path))
    const failures: string[] = []
    const added: string[] = []
    setUploading((n) => n + files.length)
    for (const file of files) {
      let path = uploadPath(file.name, taken)
      taken.add(path)
      try {
        try {
          await api.uploadAiSessionFile(sessionId, file, path)
        } catch (caught) {
          // The list was stale: something else took the path meanwhile (#2469). Read it again and take the next free one, once.
          if (!(caught instanceof ApiError && caught.status === 409 && /already exists/.test(caught.detail))) throw caught
          for (const f of (await api.listAiSessionFiles(sessionId)).files) taken.add(f.path)
          path = uploadPath(file.name, taken)
          taken.add(path)
          await api.uploadAiSessionFile(sessionId, file, path)
        }
        added.push(path)
      } catch (caught) {
        failures.push(`${file.name} was not uploaded: ${why(caught)}`)
      } finally {
        setUploading((n) => n - 1)
      }
    }
    setErrors(failures)
    if (added.length > 0) {
      onInsert(added.map(fileReference).join(' '))
      setAnnouncement(`Uploaded ${added.join(', ')}.`)
      refresh()
    }
  }

  async function download(file: WorkspaceFile) {
    setErrors([])
    try {
      await downloadBlob(() => api.getAiSessionFile(sessionId, file.path), file.path.split('/').at(-1) ?? 'file')
    } catch (caught) {
      setErrors([`${file.path} was not downloaded: ${why(caught)}`])
    }
  }

  async function togglePreview(file: WorkspaceFile) {
    if (preview?.path === file.path) {
      setPreview(null)
      return
    }
    setErrors([])
    try {
      // Fetched and shown through a blob URL: the route answers as an attachment.
      const blob = await api.getAiSessionFile(sessionId, file.path)
      setPreview({ path: file.path, url: URL.createObjectURL(blob) })
    } catch (caught) {
      setErrors([`${file.path} could not be shown: ${why(caught)}`])
    }
  }

  async function remove(file: WorkspaceFile) {
    setConfirming(null)
    setErrors([])
    try {
      await api.deleteAiSessionFile(sessionId, file.path)
      if (preview?.path === file.path) setPreview(null)
      if (data) setData({ ...data, files: data.files.filter((f) => f.path !== file.path) })
      setAnnouncement(`Deleted ${file.path}.`)
      refresh()
    } catch (caught) {
      setErrors([`${file.path} was not deleted: ${why(caught)}`])
    }
  }

  const carriesFiles = (event: DragEvent) => Array.from(event.dataTransfer.types ?? []).includes('Files')

  if (error) {
    return (
      <p role="alert" className="px-3 py-2 text-xs text-warn">
        {error instanceof ApiError ? error.detail : 'The assistant service did not answer; try again.'}
      </p>
    )
  }
  if (!data) {
    return (
      <div role="status" className="flex flex-col gap-1.5 px-3 py-2">
        <span className="sr-only">Loading…</span>
        <Skeleton className="h-4 w-1/2" />
        <Skeleton className="h-10 w-full" />
      </div>
    )
  }
  const confirmingFile = data.files.find((f) => f.path === confirming)

  return (
    <div
      className={`max-h-72 space-y-2 overflow-y-auto px-3 py-2 text-xs ${dropping ? 'bg-primary/5 outline-1 outline-primary' : ''}`}
      onDragOver={(event) => {
        if (!canWrite || !carriesFiles(event)) return
        event.preventDefault()
        setDropping(true)
      }}
      onDragLeave={() => setDropping(false)}
      onDrop={(event) => {
        setDropping(false)
        if (!canWrite || !carriesFiles(event)) return
        event.preventDefault()
        void upload(Array.from(event.dataTransfer.files))
      }}
      data-testid="session-files"
    >
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 text-muted-foreground">
          {data.usage.files} file{data.usage.files === 1 ? '' : 's'} · {formatBytes(data.usage.bytes)} of{' '}
          {formatBytes(data.limits.workspace_max_bytes)}
        </span>
        {canWrite && (
          <>
            <input
              ref={picker}
              type="file"
              multiple
              hidden
              aria-label="Upload files to the workspace"
              onChange={(event) => {
                const files = Array.from(event.target.files ?? [])
                event.target.value = ''
                void upload(files)
              }}
            />
            <Tip
              wrap={uploading > 0}
              content={`Add files for the assistant (up to ${formatBytes(data.limits.file_max_bytes)} each). You can also drop them here.`}
            >
              <Button
                size="sm"
                variant="outline"
                data-agent-user-only=""
                disabled={uploading > 0}
                onClick={() => picker.current?.click()}
              >
                <UploadIcon data-icon="inline-start" />
                {uploading > 0 ? 'Uploading…' : 'Upload'}
              </Button>
            </Tip>
          </>
        )}
      </div>
      {errors.map((message) => (
        <p key={message} role="alert" className="text-warn">
          {message}
        </p>
      ))}
      <p className="sr-only" role="status">
        {announcement}
      </p>
      {data.files.length === 0 ? (
        <p className="text-muted-foreground">
          No files yet.{canWrite ? ' Drop a file here or press Upload to give the assistant one.' : ''}
        </p>
      ) : (
        <ul aria-label="Workspace files" className="space-y-1">
          {data.files.map((file) => {
            const source = sourceLabel(file.source)
            return (
              <li key={file.path} className="rounded-lg border border-border bg-card px-2 py-1.5">
                <div className="flex items-baseline gap-1.5">
                  <Tip content={`${file.path}\nsha256 ${file.sha256}`}>
                    <span className="min-w-0 flex-1 truncate font-mono">{file.path}</span>
                  </Tip>
                  <span className="shrink-0 text-muted-foreground">{formatBytes(file.size)}</span>
                </div>
                <div className="flex flex-wrap items-center gap-x-1.5 text-[11px] text-muted-foreground">
                  <span>{file.media_type}</span>
                  {source && <span>· {source}</span>}
                  <Tip content={file.updated_at}>
                    <span>· {timeAgo(file.updated_at)}</span>
                  </Tip>
                  <span className="ml-auto flex items-center gap-0.5">
                    {PREVIEWABLE.has(file.media_type) && (
                      <Toggle
                        size="sm"
                        className="h-6 min-w-6 px-1.5 text-xs"
                        pressed={preview?.path === file.path}
                        onPressedChange={() => void togglePreview(file)}
                      >
                        <EyeIcon />
                        {preview?.path === file.path ? 'Hide' : 'Preview'}
                      </Toggle>
                    )}
                    <Tip content="Download">
                      <Button size="icon-xs" variant="ghost" aria-label={`Download ${file.path}`} onClick={() => void download(file)}>
                        <DownloadIcon />
                      </Button>
                    </Tip>
                    {canWrite && (
                      <Tip content="Delete">
                        <Button
                          size="icon-xs"
                          variant="ghost"
                          className="hover:text-destructive"
                          data-agent-user-only=""
                          aria-label={`Delete ${file.path}`}
                          onClick={(event) => {
                            deleteButton.current = event.currentTarget
                            setConfirming(file.path)
                          }}
                        >
                          <Trash2Icon />
                        </Button>
                      </Tip>
                    )}
                  </span>
                </div>
                {preview?.path === file.path && (
                  <img src={preview.url} alt={file.path} className="mt-1.5 max-h-48 max-w-full rounded-md border border-border object-contain" />
                )}
              </li>
            )
          })}
        </ul>
      )}
      <AlertDialog open={confirmingFile !== undefined} onOpenChange={(next) => !next && setConfirming(null)}>
        <AlertDialogContent
          data-modal=""
          onCloseAutoFocus={(event) => {
            // Back to its Delete, still there after a Cancel; once the file is gone, to the list.
            event.preventDefault()
            const back = deleteButton.current
            if (back?.isConnected) back.focus()
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {confirmingFile?.path}?</AlertDialogTitle>
            <AlertDialogDescription>The assistant loses it too.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              data-agent-user-only=""
              onClick={() => confirmingFile && void remove(confirmingFile)}
            >
              Delete file
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
