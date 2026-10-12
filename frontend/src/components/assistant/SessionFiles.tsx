import { useEffect, useRef, useState, type DragEvent } from 'react'
import { api, ApiError } from '../../api/client'
import type { WorkspaceFile } from '../../api/types'
import { DownloadBlockedError, downloadBlob } from '../../lib/embed'
import { formatBytes, timeAgo } from '../../lib/format'
import { fileReference, sourceLabel, uploadPath } from '../../lib/workspaceFiles'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'

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
          if (!(caught instanceof ApiError && caught.status === 409)) throw caught
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
      <p role="alert" className="px-3 py-2 text-[12px] text-warn">
        {error instanceof ApiError ? error.detail : 'The assistant service did not answer; try again.'}
      </p>
    )
  }
  if (!data) return <p className="px-3 py-2 text-[12px] text-faint">Loading…</p>

  return (
    <div
      className={`max-h-72 space-y-2 overflow-y-auto px-3 py-2 text-[12px] ${dropping ? 'bg-accent/5 outline outline-1 outline-accent' : ''}`}
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
        <span className="min-w-0 flex-1 text-faint">
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
            <Button
              size="sm"
              data-agent-user-only=""
              disabled={uploading > 0}
              title={`Add files for the assistant (up to ${formatBytes(data.limits.file_max_bytes)} each). You can also drop them here.`}
              onClick={() => picker.current?.click()}
            >
              {uploading > 0 ? 'Uploading…' : 'Upload'}
            </Button>
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
        <p className="text-muted">
          No files yet.{canWrite ? ' Drop a file here or press Upload to give the assistant one.' : ''}
        </p>
      ) : (
        <ul aria-label="Workspace files" className="space-y-1">
          {data.files.map((file) => {
            const source = sourceLabel(file.source)
            return (
              <li key={file.path} className="rounded-[4px] border border-line bg-bg px-2 py-1">
                <div className="flex items-baseline gap-1.5">
                  <span className="min-w-0 flex-1 truncate font-mono" title={`${file.path}\nsha256 ${file.sha256}`}>
                    {file.path}
                  </span>
                  <span className="shrink-0 text-faint">{formatBytes(file.size)}</span>
                </div>
                <div className="flex flex-wrap items-center gap-x-1.5 text-[11px] text-faint">
                  <span>{file.media_type}</span>
                  {source && <span>· {source}</span>}
                  <span title={file.updated_at}>· {timeAgo(file.updated_at)}</span>
                  <span className="ml-auto flex items-center gap-1">
                    {PREVIEWABLE.has(file.media_type) && (
                      <Button size="sm" variant="ghost" aria-pressed={preview?.path === file.path} onClick={() => void togglePreview(file)}>
                        {preview?.path === file.path ? 'Hide' : 'Preview'}
                      </Button>
                    )}
                    <Button size="sm" variant="ghost" aria-label={`Download ${file.path}`} onClick={() => void download(file)}>
                      Download
                    </Button>
                    {canWrite && confirming !== file.path && (
                      <Button
                        size="sm"
                        variant="ghost"
                        data-agent-user-only=""
                        aria-label={`Delete ${file.path}`}
                        onClick={() => setConfirming(file.path)}
                      >
                        Delete
                      </Button>
                    )}
                  </span>
                </div>
                {confirming === file.path && (
                  <div className="mt-1 flex flex-wrap items-center gap-1.5" role="group" aria-label={`Delete ${file.path}?`}>
                    <span className="min-w-0 flex-1 text-muted">Delete {file.path}? The assistant loses it too.</span>
                    <Button size="sm" variant="danger" data-agent-user-only="" onClick={() => void remove(file)}>
                      Delete file
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>
                      Cancel
                    </Button>
                  </div>
                )}
                {preview?.path === file.path && (
                  <img src={preview.url} alt={file.path} className="mt-1 max-h-48 max-w-full rounded-[4px] border border-line object-contain" />
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
