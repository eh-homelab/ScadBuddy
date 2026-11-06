import { useEffect, useState } from 'react'
import { FileIcon, FileImageIcon, FolderOpenIcon } from 'lucide-react'
import { ApiError, api } from '../../api/client'
import type { PickerFile, PickerSession, ResourceRef } from '../../api/types'
import { statusLabel } from '../../agent/chat/labels'
import { useAssistantOpener } from '../../agent/chat/opener'
import { formatBytes } from '../../lib/format'
import { failure } from '../../lib/mediaFiles'
import { sourceLabel } from '../../lib/workspaceFiles'
import { useAsync } from '../../lib/useAsync'
import { Spinner } from '../ui/spinner'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

/** How long the search waits after the last key before it asks the agent. */
export const SEARCH_DEBOUNCE_MS = 250
/** The largest image the picker loads as a thumbnail. */
export const THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
const THUMBNAIL_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif'])

/** Which workspace file was picked: the bytes came from this path at this sha256. */
export interface WorkspacePick {
  sessionId: string
  path: string
  sha256: string
}

interface Props {
  open: boolean
  onClose: () => void
  /** What the default list is scoped to: the sessions that touched it. Null opens Advanced. */
  resource: ResourceRef | null
  /** The resource in words, for the default list: "this model". */
  resourceLabel?: string
  /** The target's file input `accept`: only the files it takes are listed. */
  accept: string
  /** What the target takes, in words: "PNG, JPEG or WebP". */
  acceptLabel?: string
  title?: string
  /**
   * Takes the picked file, as a disk upload would give it. The picker closes once it
   * resolves, and shows what it throws.
   */
  onFile: (file: File, pick: WorkspacePick) => Promise<void> | void
}

/** The last segment of a workspace path: the picked file's name. */
function baseName(path: string): string {
  return path.slice(path.lastIndexOf('/') + 1)
}

/**
 * #4703 — one way to take a file from an assistant session's workspace (#2329), wherever
 * the UI takes a file. The default list is the sessions that touched the resource on
 * screen, newest first; Advanced lists every session the user may see, archived ones
 * too, searched by title and file name. Only the files `accept` takes are listed. A
 * session whose files cannot be read now is greyed out with the reason.
 *
 * A pick reads the file at the sha256 the list showed (agent `routes/sessionFiles.ts`):
 * a file that changed since is refused, and the list reads again, so stale bytes are
 * never sent. The bytes then go to `onFile` as a `File`, the same way as a disk upload.
 */
export function WorkspaceFilePicker({
  open,
  onClose,
  resource,
  resourceLabel = 'this',
  accept,
  acceptLabel,
  title = 'Pick a file from an assistant session',
  onFile,
}: Props) {
  const [advanced, setAdvanced] = useState(resource === null)
  const [typed, setTyped] = useState('')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const timer = setTimeout(() => setQuery(typed.trim()), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [typed])

  const scoped = advanced ? null : resource
  const listed = useAsync(
    async () => (open ? (await api.listWorkspacePicks({ resource: scoped, accept, q: advanced ? query : '' })).sessions : null),
    [open, scoped?.type, scoped?.id, accept, advanced, advanced ? query : ''],
  )

  function close() {
    if (busy) return
    setError(null)
    onClose()
  }

  async function pick(session: PickerSession, file: PickerFile) {
    const key = `${session.id}/${file.path}`
    setBusy(key)
    setError(null)
    let blob: Blob
    try {
      blob = await api.pickAiSessionFile(session.id, file.path, file.sha256)
    } catch (caught) {
      setBusy(null)
      if (caught instanceof ApiError && (caught.status === 409 || caught.status === 404)) {
        setError(`${file.path} changed after the list was shown, so it was not used. The list now shows the current files.`)
        listed.reload()
      } else {
        setError(failure(caught))
      }
      return
    }
    try {
      await onFile(new File([blob], baseName(file.path), { type: file.media_type }), {
        sessionId: session.id,
        path: file.path,
        sha256: file.sha256,
      })
      setBusy(null)
      onClose()
    } catch (caught) {
      setBusy(null)
      setError(failure(caught))
    }
  }

  const sessions = listed.data ?? null

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {advanced
              ? 'Every assistant session you can see, archived ones too.'
              : `The assistant sessions that worked on ${resourceLabel}, newest first.`}
            {acceptLabel ? ` Only ${acceptLabel} files are shown.` : ''}
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3" data-testid="workspace-file-picker">
          <div className="flex items-center gap-2">
            {resource && (
              <Button
                size="sm"
                variant={advanced ? 'ghost' : 'outline'}
                aria-pressed={!advanced}
                onClick={() => setAdvanced(false)}
              >
                Worked on {resourceLabel}
              </Button>
            )}
            <Button size="sm" variant={advanced ? 'outline' : 'ghost'} aria-pressed={advanced} onClick={() => setAdvanced(true)}>
              Advanced
            </Button>
          </div>
          {advanced && (
            <Input
              type="search"
              value={typed}
              placeholder="Search session titles and file names"
              aria-label="Search sessions and files"
              maxLength={200}
              onChange={(event) => setTyped(event.target.value)}
            />
          )}

          {listed.error && (
            <p role="alert" className="text-xs text-destructive">
              {listed.error instanceof ApiError ? listed.error.detail : 'The assistant service did not answer; try again.'}
            </p>
          )}
          {!listed.error && sessions === null && (
            <div role="status" className="flex flex-col gap-2">
              <span className="sr-only">Loading…</span>
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-12 w-full" />
            </div>
          )}
          {sessions !== null && sessions.length === 0 && (
            <p className="text-xs text-muted-foreground" data-testid="workspace-picker-empty">
              {advanced ? (
                query ? 'No session or file matches.' : 'No session holds a file of this kind.'
              ) : (
                <>
                  No assistant session worked on {resourceLabel} yet.{' '}
                  <button type="button" className="text-primary underline" onClick={() => setAdvanced(true)}>
                    Look in every session (Advanced)
                  </button>
                </>
              )}
            </p>
          )}
          {sessions !== null && sessions.length > 0 && (
            <ul className="flex flex-col gap-2" aria-label="Assistant sessions">
              {sessions.map((session) => (
                <SessionEntry
                  key={session.id}
                  session={session}
                  busy={busy}
                  onPick={(file) => void pick(session, file)}
                />
              ))}
            </ul>
          )}

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

function SessionEntry({
  session,
  busy,
  onPick,
}: {
  session: PickerSession
  busy: string | null
  onPick: (file: PickerFile) => void
}) {
  const name = session.title || 'Untitled session'
  return (
    <li
      aria-label={name}
      aria-disabled={session.unavailable ? true : undefined}
      className={cn('rounded-lg border px-3 py-2 text-xs', session.unavailable && 'opacity-60')}
    >
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate font-medium text-foreground">{name}</span>
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {session.archived ? 'Archived · ' : ''}
          {statusLabel(session.status)} · {new Date(session.updated_at).toLocaleString()}
        </span>
      </div>
      {session.unavailable && <p className="mt-1 text-muted-foreground">{session.unavailable}</p>}
      {session.files.length === 0 && !session.unavailable && (
        <p className="mt-1 text-muted-foreground">
          No file of this kind{session.other_files > 0 ? ` (${session.other_files} of other kinds)` : ''}.
        </p>
      )}
      {session.files.length > 0 && (
        <ul className="mt-1.5 flex flex-col gap-1" aria-label={`Files of ${name}`}>
          {session.files.map((file) => {
            const key = `${session.id}/${file.path}`
            const origin = sourceLabel(file.source)
            return (
              <li key={file.path}>
                <button
                  type="button"
                  disabled={busy !== null || file.unavailable !== null}
                  aria-label={`Use ${file.path}`}
                  title={file.unavailable ?? undefined}
                  onClick={() => onPick(file)}
                  className="flex w-full items-center gap-2 rounded-md p-1 text-left hover:bg-muted disabled:opacity-60 disabled:hover:bg-transparent"
                >
                  <span className="grid size-10 shrink-0 place-items-center overflow-hidden rounded bg-muted">
                    {busy === key ? (
                      <Spinner />
                    ) : (
                      <Thumbnail sessionId={session.id} file={file} />
                    )}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-foreground">{file.path}</span>
                    <span className="block text-[11px] text-muted-foreground">
                      {file.media_type} · {formatBytes(file.size)}
                      {origin ? ` · ${origin}` : ''}
                      {file.unavailable ? ` · ${file.unavailable}` : ''}
                    </span>
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </li>
  )
}

/** A small image of a picture file, read from the workspace; an icon for anything else. */
function Thumbnail({ sessionId, file }: { sessionId: string; file: PickerFile }) {
  const shows = THUMBNAIL_TYPES.has(file.media_type) && file.size <= THUMBNAIL_MAX_BYTES && file.unavailable === null
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!shows) return
    let made: string | null = null
    let live = true
    api
      .getAiSessionFile(sessionId, file.path)
      .then((blob) => {
        if (!live) return
        made = URL.createObjectURL(blob)
        setUrl(made)
      })
      // No thumbnail then: the row still names the file.
      .catch(() => undefined)
    return () => {
      live = false
      if (made) URL.revokeObjectURL(made)
    }
  }, [shows, sessionId, file.path, file.sha256])
  if (url) return <img src={url} alt="" className="size-full object-cover" draggable={false} />
  return file.media_type.startsWith('image/') ? (
    <FileImageIcon aria-hidden="true" className="size-4 text-muted-foreground" />
  ) : (
    <FileIcon aria-hidden="true" className="size-4 text-muted-foreground" />
  )
}

/**
 * The button that opens the picker, beside a target's own upload. It shows nothing
 * without the assistant (AI off), as `ResourceSessions` does.
 */
export function WorkspacePickButton({
  label = 'From a session…',
  disabled = false,
  ...picker
}: Omit<Props, 'open' | 'onClose'> & { label?: string; disabled?: boolean }) {
  const opener = useAssistantOpener()
  const [open, setOpen] = useState(false)
  if (!opener) return null
  return (
    <>
      <Button size="sm" variant="outline" disabled={disabled} onClick={() => setOpen(true)}>
        <FolderOpenIcon data-icon="inline-start" />
        {label}
      </Button>
      {open && <WorkspaceFilePicker {...picker} open onClose={() => setOpen(false)} />}
    </>
  )
}
