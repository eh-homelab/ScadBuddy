import { useState } from 'react'
import { statusLabel } from '../../agent/chat/labels'
import { api, ApiError } from '../../api/client'
import type { AiSessionView } from '../../api/types'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { Tip } from './Tip'

// #1885 — the switcher's Archived view: the archived chats, newest first, read over
// HTTP (`GET /api/v1/ai/sessions?archived=true`), since the socket's snapshot leaves
// them out. Each opens read-only, or is unarchived back into the main list.

/** The most archived chats listed: the agent's list maximum (agent routes/sessions.ts LIST_LIMIT_MAX). */
export const ARCHIVE_LIMIT = 500

interface Props {
  onOpen: (session: AiSessionView) => void
  /** Resolve once the agent took it; reject with what to show beside the row. */
  onUnarchive: (session: AiSessionView) => Promise<void>
  /**
   * Whether the panel's state now holds this chat unarchived: one unarchived elsewhere
   * (the composer's note) leaves the list at once, not when the view re-mounts (#2118).
   */
  unarchived: (id: string) => boolean
}

export function ArchivedSessions({ onOpen, onUnarchive, unarchived }: Props) {
  const archived = useAsync(() => api.listArchivedAiSessions(ARCHIVE_LIMIT), [])
  const [gone, setGone] = useState<ReadonlySet<string>>(new Set())
  const [errors, setErrors] = useState<Record<string, string>>({})

  if (archived.loading) {
    return (
      <div role="status" className="flex flex-col gap-1.5 px-3 py-2">
        <span className="sr-only">Loading archived chats…</span>
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-9 w-full" />
      </div>
    )
  }
  if (archived.error) {
    return (
      <p role="alert" className="px-3 py-2 text-xs text-warn">
        {archived.error instanceof ApiError ? archived.error.detail : 'The assistant service did not answer.'}
      </p>
    )
  }
  const sessions = (archived.data?.sessions ?? []).filter((s) => !gone.has(s.id) && !unarchived(s.id))
  if (sessions.length === 0) return <p className="px-3 py-2 text-xs text-muted-foreground">No archived chats.</p>

  const unarchive = async (session: AiSessionView) => {
    setErrors(({ [session.id]: _, ...rest }) => rest)
    try {
      await onUnarchive(session)
      setGone((was) => new Set(was).add(session.id))
    } catch (caught) {
      setErrors((was) => ({ ...was, [session.id]: caught instanceof Error ? caught.message : String(caught) }))
    }
  }

  return (
    <ScrollArea className="[&>[data-slot=scroll-area-viewport]]:max-h-56">
    <ul aria-label="Archived chats" className="flex flex-col gap-0.5 p-1.5">
      {sessions.map((s) => (
        <li key={s.id} className="rounded-md px-2.5 py-1.5 hover:bg-ui-muted">
          <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
            <Tip content={s.title}>
              <span className="min-w-0 flex-1 basis-40 truncate text-[13px]">{s.title}</span>
            </Tip>
            <span className="flex shrink-0 items-center gap-0.5">
              <Button variant="ghost" size="sm" aria-label={`Open ${s.title}`} onClick={() => onOpen(s)}>
                Open
              </Button>
              <Button
                variant="outline"
                size="sm"
                aria-label={`Unarchive ${s.title}`}
                data-agent-user-only=""
                onClick={() => void unarchive(s)}
              >
                Unarchive
              </Button>
            </span>
          </div>
          <span className="flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] text-muted-foreground">{statusLabel(s.status)}</span>
            {s.parent_id && <span className="text-[10.5px] text-muted-foreground">fork</span>}
            {s.archived_at && (
              <time dateTime={s.archived_at} className="text-[11px] text-muted-foreground">
                archived {timeAgo(s.archived_at)}
              </time>
            )}
          </span>
          {errors[s.id] && (
            <p role="alert" className="pt-1 text-xs text-warn">
              {errors[s.id]}
            </p>
          )}
        </li>
      ))}
    </ul>
    </ScrollArea>
  )
}
