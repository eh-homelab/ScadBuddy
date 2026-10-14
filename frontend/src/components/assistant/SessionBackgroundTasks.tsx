import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { BackgroundTask } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'

interface Props {
  sessionId: string
  /** Changes when the tasks may have changed (a turn ended, a tool call finished), so the list reads again. */
  refreshKey?: unknown
  /** The browser user owns the session: cancelling is offered. */
  canWrite: boolean
}

function when(iso: string | null): string {
  if (!iso) return ''
  const at = new Date(iso)
  return at.toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' })
}

/**
 * The jobs and wakeups this session's CronCreate and ScheduleWakeup left running: each
 * sends its prompt to the session when it is due and the session is idle. Nothing when
 * there are none running, or when they cannot be read.
 */
export function SessionBackgroundTasks({ sessionId, refreshKey, canWrite }: Props) {
  const { data, refresh, setData } = useAsync(() => api.listAiBackgroundTasks(sessionId), [sessionId])
  const readAt = useRef(refreshKey)
  useEffect(() => {
    if (Object.is(readAt.current, refreshKey)) return
    readAt.current = refreshKey
    refresh()
  }, [refreshKey, refresh])
  const [error, setError] = useState<string | null>(null)

  const active = (data?.tasks ?? []).filter((t) => t.state === 'active')
  if (active.length === 0 && !error) return null

  async function cancel(task: BackgroundTask) {
    setError(null)
    try {
      await api.cancelAiBackgroundTask(sessionId, task.id)
      if (data) setData({ tasks: data.tasks.map((t) => (t.id === task.id ? { ...t, state: 'cancelled' } : t)) })
      refresh()
    } catch (caught) {
      setError(`${task.id} was not cancelled: ${caught instanceof ApiError ? caught.detail : 'the assistant service did not answer'}`)
    }
  }

  return (
    <section aria-label="Scheduled in this session" className="shrink-0 border-b border-line px-3 py-1.5 text-[12px]">
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
      <ul className="space-y-1">
        {active.map((task) => (
          <li key={task.id} className="flex items-baseline gap-1.5">
            <span className="shrink-0 text-faint" title={task.kind === 'cron' ? 'CronCreate' : 'ScheduleWakeup'}>
              {task.schedule}
            </span>
            <span className="min-w-0 flex-1 truncate" title={task.prompt}>
              {task.prompt}
            </span>
            {task.next_at && (
              <span className="shrink-0 text-faint" title={task.next_at}>
                next {when(task.next_at)}
              </span>
            )}
            {canWrite && (
              <Button size="sm" variant="ghost" data-agent-user-only="" aria-label={`Cancel ${task.id}`} onClick={() => void cancel(task)}>
                Cancel
              </Button>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
