import { useState } from 'react'
import { statusLabel } from '../../agent/chat/labels'
import { api, ApiError } from '../../api/client'
import type { AiSessionView } from '../../api/types'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'

// #1885 — the switcher's Archived view: the archived chats, newest first, read over
// HTTP (`GET /api/v1/ai/sessions?archived=true`), since the socket's snapshot leaves
// them out. Each opens read-only, or is unarchived back into the main list.

/** The most archived chats listed: the agent's list maximum (agent routes/sessions.ts LIST_LIMIT_MAX). */
export const ARCHIVE_LIMIT = 500

interface Props {
  onOpen: (session: AiSessionView) => void
  /** Resolve once the agent took it; reject with what to show beside the row. */
  onUnarchive: (session: AiSessionView) => Promise<void>
}

export function ArchivedSessions({ onOpen, onUnarchive }: Props) {
  const archived = useAsync(() => api.listArchivedAiSessions(ARCHIVE_LIMIT), [])
  const [gone, setGone] = useState<ReadonlySet<string>>(new Set())
  const [errors, setErrors] = useState<Record<string, string>>({})

  if (archived.loading) {
    return (
      <p role="status" className="px-3 py-2 text-[12.5px] text-muted">
        Loading archived chats…
      </p>
    )
  }
  if (archived.error) {
    return (
      <p role="alert" className="px-3 py-2 text-[12.5px] text-warn">
        {archived.error instanceof ApiError ? archived.error.detail : 'The assistant service did not answer.'}
      </p>
    )
  }
  const sessions = (archived.data?.sessions ?? []).filter((s) => !gone.has(s.id))
  if (sessions.length === 0) return <p className="px-3 py-2 text-[12.5px] text-muted">No archived chats.</p>

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
    <ul aria-label="Archived chats" className="max-h-56 overflow-y-auto py-1">
      {sessions.map((s) => (
        <li key={s.id} className="px-3 py-1.5">
          <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
            <span className="min-w-0 flex-1 basis-40 truncate text-[12.5px]" title={s.title}>
              {s.title}
            </span>
            <span className="flex shrink-0 items-center gap-0.5">
              <Button variant="ghost" size="sm" aria-label={`Open ${s.title}`} onClick={() => onOpen(s)}>
                Open
              </Button>
              <Button
                variant="ghost"
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
            <span className="text-[11px] text-faint">{statusLabel(s.status)}</span>
            {s.parent_id && <span className="text-[10.5px] text-faint">fork</span>}
            {s.archived_at && (
              <time dateTime={s.archived_at} className="text-[11px] text-faint">
                archived {timeAgo(s.archived_at)}
              </time>
            )}
          </span>
          {errors[s.id] && (
            <p role="alert" className="pt-1 text-[12px] text-warn">
              {errors[s.id]}
            </p>
          )}
        </li>
      ))}
    </ul>
  )
}
