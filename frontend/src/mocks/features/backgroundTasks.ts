/**
 * A session's background tasks, as the agent's `routes/backgroundTasks.ts` serves them:
 * the list (active first) and an owner's cancel, kept in memory per session.
 */
import { HttpResponse, http } from 'msw'
import type { BackgroundTask } from '../../api/types'

const state = { tasks: new Map<string, BackgroundTask[]>() }

export function reset(): void {
  state.tasks.clear()
}

/** Puts a task in a session, as CronCreate or ScheduleWakeup would have (for tests). */
export function seedBackgroundTask(sessionId: string, task: Partial<BackgroundTask> & Pick<BackgroundTask, 'id'>): void {
  const list = state.tasks.get(sessionId) ?? []
  list.push({
    kind: 'cron',
    description: task.prompt ?? 'check the build',
    schedule: 'Every 5 minutes',
    prompt: 'check the build',
    state: 'active',
    next_at: new Date(Date.now() + 5 * 60_000).toISOString(),
    fired: 0,
    last_fired_at: null,
    expires_at: null,
    detail: null,
    created_at: new Date().toISOString(),
    ...task,
  })
  state.tasks.set(sessionId, list)
}

export const handlers = [
  http.get('/api/v1/ai/sessions/:id/background-tasks', ({ params }) => {
    const tasks = state.tasks.get(String(params.id)) ?? []
    return HttpResponse.json({ tasks: [...tasks.filter((t) => t.state === 'active'), ...tasks.filter((t) => t.state !== 'active')] })
  }),
  http.delete('/api/v1/ai/sessions/:id/background-tasks/:taskId', ({ params }) => {
    const task = (state.tasks.get(String(params.id)) ?? []).find((t) => t.id === params.taskId && t.state === 'active')
    if (!task) return HttpResponse.json({ detail: `no active background task ${String(params.taskId)} in this session` }, { status: 404 })
    task.state = 'cancelled'
    task.detail = 'cancelled by the user in the panel'
    task.next_at = null
    return HttpResponse.json({ task })
  }),
]
