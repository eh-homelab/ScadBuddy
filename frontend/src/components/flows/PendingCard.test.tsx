import { act, screen, waitFor, within } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { printRunPoll } from '../../api/client'
import type { FlowPending, TaskQuestion } from '../../api/types'
import { taskTopic } from '../../lib/flows'
import { fakeRealtime } from '../../lib/realtime.fake'
import { getToasts } from '../../lib/toast'
import { acceptOperation } from '../../mocks/features/operations'
import { PROMPT_TASK_ID, setTask, taskAnswers, taskQuestion } from '../../mocks/features/tasks'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { PendingCard } from './PendingCard'

// #7364 — a run's parked prompt, answered from its task's schema (mocks/features/tasks.ts).

const KEY = /^[0-9a-f]{32}$/
const ANSWER_URL = '/api/v1/tasks/:id/answer'
const NOT_OPEN = 'https://scadbuddy.dev/problems/tasks/not-open'
const ELSEWHERE = 'This question was answered elsewhere.'

function pending(task: TaskQuestion | null): FlowPending {
  return {
    call_id: task?.id ?? PROMPT_TASK_ID,
    kind: 'answer',
    fn: 'prompt',
    prompt: task?.title ?? 'Swap to pink?',
    since: '2026-10-10T09:02:00Z',
    hop: null,
    hop_until: null,
    reason: null,
    proposed: false,
    hops: [],
    summary: null,
    bound_hash: null,
    task,
  }
}

const question = (fields: Partial<TaskQuestion> = {}): TaskQuestion => ({ ...taskQuestion(PROMPT_TASK_ID)!, ...fields })

/** The card as a run's page draws it; `again` is the page read again, with the task as given. */
function draw(task: TaskQuestion | null = question()) {
  const respond = vi.fn(async (send: () => Promise<unknown>) => {
    await send()
  })
  const card = (next: TaskQuestion | null) => {
    const entry = pending(next)
    return <PendingCard key={entry.call_id} entry={entry} runId="run-prompt" step={null} respond={respond} />
  }
  const view = renderPage(card(task))
  return { ...view, respond, again: (next: TaskQuestion | null = question()) => view.rerender(card(next)) }
}

const email = () => screen.getByLabelText(/^Email/)
const noForm = () => expect(screen.queryByRole('button', { name: 'Answer' })).toBeNull()

async function answer(user: UserEvent, address: string) {
  await user.clear(email())
  await user.type(email(), address)
  await user.click(screen.getByRole('button', { name: 'Answer' }))
}

/** The answer route refused, once or always, with `status` and the problem's `extra`. */
function refuse(status: number, extra: Record<string, unknown> = {}, options: { once?: boolean } = {}) {
  const sent: { key: string | null; body: unknown }[] = []
  server.use(
    http.post(
      ANSWER_URL,
      async ({ request }) => {
        sent.push({ key: request.headers.get('Idempotency-Key'), body: await request.json() })
        return HttpResponse.json(
          { title: 'Refused', status, detail: `refused ${status}`, ...extra },
          { status, headers: { 'Content-Type': 'application/problem+json' } },
        )
      },
      options,
    ),
  )
  return sent
}

const stops: (() => void)[] = []

/** How many times the task was read. */
function countReads() {
  const reads: string[] = []
  const onStart = ({ request }: { request: Request }) => {
    if (request.method === 'GET' && request.url.endsWith(`/tasks/${PROMPT_TASK_ID}`)) reads.push(request.url)
  }
  server.events.on('request:start', onStart)
  stops.push(() => server.events.removeListener('request:start', onStart))
  return reads
}

const poll = { ...printRunPoll }
beforeEach(() => {
  // What `fakeRealtime`'s signal advances.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  printRunPoll.intervalMs = 1
  printRunPoll.reattempts = 1
})
afterEach(() => {
  vi.useRealTimers()
  Object.assign(printRunPoll, poll)
  for (const stop of stops.splice(0)) stop()
})

describe('PendingCard, a prompt (#7364)', () => {
  it('keeps the text answer for a question with no task', () => {
    draw(null)
    expect(screen.getByRole('textbox', { name: 'Your answer' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Waiting for your answer: Swap to pink?' })).toBeInTheDocument()
  })

  it('asks for the schema’s fields, sends them with a key, and reads the task and the run again', async () => {
    const reads = countReads()
    const { user, respond } = draw()
    expect(screen.queryByRole('textbox', { name: 'Your answer' })).toBeNull()
    await waitFor(() => expect(reads.length).toBeGreaterThan(0))
    const before = reads.length
    await user.click(screen.getByRole('checkbox', { name: /Also send a text message/ }))
    await answer(user, 'jo@example.com')
    expect(await screen.findByText('Answered')).toBeInTheDocument()
    noForm()
    expect(taskAnswers()).toEqual([
      {
        id: PROMPT_TASK_ID,
        key: expect.stringMatching(KEY),
        body: { answer: { email: 'jo@example.com', language: 'English', also_sms: true } },
      },
    ])
    expect(respond).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(reads.length).toBeGreaterThan(before))
    expect(getToasts().map((t) => [t.title, t.detail])).toEqual([['Answer sent', 'Where do we send the pickup note?']])
  })

  it('shows the title, the context and the limit as text, never as markup or links', async () => {
    const until = new Date(Date.now() + 60_000).toISOString()
    setTask(PROMPT_TASK_ID, {
      title: '<b>Who</b> is **it**?',
      context: [{ note: 'see [this](https://example.com)\n<img src=x onerror=alert(1)>', count: 3, none: null, items: ['a', 2], deep: [{ x: 1 }] }, { url: 'https://example.com/a' }],
      expires_at: until,
    })
    const { container } = draw(question())
    const card = screen.getByRole('region', { name: 'Waiting for your answer: <b>Who</b> is **it**?' })
    const context = within(card).getByRole('group', { name: 'Context' })
    expect(within(context).getAllByRole('term').map((t) => t.textContent)).toEqual(['note', 'count', 'none', 'items', 'deep', 'url'])
    expect(within(context).getAllByRole('definition').map((d) => d.textContent)).toEqual([
      'see [this](https://example.com)\n<img src=x onerror=alert(1)>',
      '3',
      '—',
      'a, 2',
      '[{"x":1}]',
      'https://example.com/a',
    ])
    expect(container.querySelector('a, img, b')).toBeNull()
    expect(card).toHaveTextContent(/Answer by /)
  })

  it('shows no context and no limit when the task has none', () => {
    setTask(PROMPT_TASK_ID, { context: [], expires_at: null })
    const { container } = draw()
    expect(screen.queryByRole('group', { name: 'Context' })).toBeNull()
    expect(container).not.toHaveTextContent('Answer by')
  })

  it('says the limit has passed', () => {
    setTask(PROMPT_TASK_ID, { expires_at: new Date(Date.now() - 60_000).toISOString() })
    draw()
    expect(screen.getByText('Its time limit has passed.')).toBeInTheDocument()
  })

  it('says so only then, even for a limit longer than one timer', async () => {
    fakeRealtime()
    const days = 86_400_000
    setTask(PROMPT_TASK_ID, { expires_at: new Date(Date.now() + 40 * days).toISOString() })
    draw()
    await act(() => vi.advanceTimersByTimeAsync(30 * days))
    expect(screen.getByText(/^Answer by /)).toBeInTheDocument()
    await act(() => vi.advanceTimersByTimeAsync(11 * days))
    expect(screen.getByText('Its time limit has passed.')).toBeInTheDocument()
  })

  it('keeps the form filled when the answer does not fit, and sends the next one with its own key', async () => {
    const { user } = draw()
    await answer(user, 'jo')
    await waitFor(() => expect(email()).toHaveAccessibleDescription("The customer's address not a valid email"))
    expect(email()).toHaveValue('jo')
    expect(email()).toHaveAttribute('aria-invalid', 'true')
    expect(email()).toHaveFocus()
    expect(getToasts()).toEqual([])
    await answer(user, 'jo@example.com')
    expect(await screen.findByText('Answered')).toBeInTheDocument()
    const [first, second] = taskAnswers()
    expect(second!.key).toMatch(KEY)
    expect(second!.key).not.toBe(first!.key)
  })

  it('closes on a 409 `not-open`, and an open task on the page does not draw the form again', async () => {
    const realtime = fakeRealtime()
    refuse(409, { type: NOT_OPEN, state: 'open' })
    const { user, again, respond } = draw()
    await answer(user, 'jo@example.com')
    expect(await screen.findByText(ELSEWHERE)).toBeInTheDocument()
    expect(respond).toHaveBeenCalledTimes(1)
    noForm()
    again(question())
    await realtime.signal(taskTopic(PROMPT_TASK_ID), 'task.changed')
    noForm()
    expect(screen.getByRole('status')).toHaveTextContent(ELSEWHERE)

    setTask(PROMPT_TASK_ID, { state: 'expired' })
    await realtime.signal(taskTopic(PROMPT_TASK_ID), 'task.changed')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Timed out'), { timeout: 3000 })
    noForm()
  })

  it.each([
    ['answered', 'Answered'],
    ['expired', 'Timed out'],
    ['cancelled', 'Cancelled'],
  ] as const)('draws no form for a task that is %s', (state, said) => {
    setTask(PROMPT_TASK_ID, { state })
    draw(question())
    noForm()
    expect(screen.getByText(said)).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Where do we send the pickup note?' })).toBeInTheDocument()
  })

  it('reads the task when it opens, so a page that is behind draws no form', async () => {
    const behind = question()
    setTask(PROMPT_TASK_ID, { state: 'answered' })
    draw(behind)
    expect(await screen.findByText('Answered')).toBeInTheDocument()
    noForm()
  })

  it('reads the task on `task.changed`, and a newer page wins over an older read', async () => {
    const realtime = fakeRealtime()
    const { again } = draw()
    expect(realtime.following()).toContain(`task:${PROMPT_TASK_ID}`)
    setTask(PROMPT_TASK_ID, { state: 'cancelled' })
    await realtime.signal(taskTopic(PROMPT_TASK_ID), 'task.changed')
    expect(await screen.findByText('Cancelled', {}, { timeout: 3000 })).toBeInTheDocument()
    noForm()
    // A Reset opened it again, and the page was read since.
    again(question({ state: 'open', updated_at: new Date(Date.now() + 5000).toISOString() }))
    expect(screen.getByRole('button', { name: 'Answer' })).toBeInTheDocument()
  })

  it('asks again when a task answered elsewhere is opened again', async () => {
    refuse(409, { type: NOT_OPEN, state: 'open' }, { once: true })
    const { user, again } = draw()
    await answer(user, 'jo@example.com')
    expect(await screen.findByText(ELSEWHERE)).toBeInTheDocument()
    const later = (ms: number) => new Date(Date.now() + ms).toISOString()
    again(question({ state: 'answered', updated_at: later(1000) }))
    noForm()
    again(question({ state: 'open', updated_at: later(2000) }))
    expect(email()).toHaveValue('')
  })

  it.each([
    [403, {}, 'refused 403'],
    [404, {}, 'refused 404'],
    [409, { type: 'https://scadbuddy.dev/problems/stale-entry' }, 'This request is out of date. The run has moved on.'],
    [413, {}, 'refused 413'],
    [503, {}, 'refused 503'],
  ])('keeps the form filled and says why on another refusal (%s)', async (status, extra, said) => {
    refuse(status, extra)
    const { user } = draw()
    await answer(user, 'jo@example.com')
    expect(await screen.findByRole('alert')).toHaveTextContent(said)
    expect(email()).toHaveValue('jo@example.com')
    expect(screen.getByRole('button', { name: 'Answer' })).toBeEnabled()
  })

  it('sends one answer while it is in flight', async () => {
    let release!: () => void
    const landed = new Promise<void>((resolve) => (release = resolve))
    const sent: unknown[] = []
    server.use(
      http.post(ANSWER_URL, async ({ request }) => {
        sent.push(await request.json())
        await landed
        return HttpResponse.json({ title: 'Refused', status: 403, detail: 'no' }, { status: 403 })
      }),
    )
    const { user } = draw()
    await answer(user, 'jo@example.com')
    const button = await screen.findByRole('button', { name: 'Sending…' })
    expect(button).toBeDisabled()
    await user.click(button)
    await user.type(email(), '{Enter}')
    release()
    expect(await screen.findByRole('button', { name: 'Answer' })).toBeEnabled()
    expect(sent).toHaveLength(1)
  })

  it('sends an answer that got no reply again with its key, and a changed one with a new key', async () => {
    const keys: (string | null)[] = []
    server.use(
      http.post(ANSWER_URL, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return HttpResponse.error()
      }),
    )
    const { user } = draw()
    await answer(user, 'jo@example.com')
    await screen.findByRole('alert')
    await user.click(screen.getByRole('button', { name: 'Answer' }))
    await waitFor(() => expect(keys).toHaveLength(4))
    expect(new Set(keys).size).toBe(1)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Answer' })).toBeEnabled())
    await answer(user, 'sam@example.com')
    await waitFor(() => expect(keys).toHaveLength(6))
    expect(keys[4]).toBe(keys[5])
    expect(keys[4]).not.toBe(keys[0])
  })

  it('sends a refused answer again with a new key', async () => {
    const sent = refuse(503)
    const { user } = draw()
    await answer(user, 'jo@example.com')
    await screen.findByRole('alert')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Answer' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Answer' }))
    await waitFor(() => expect(sent).toHaveLength(2))
    expect(sent[0]!.key).not.toBe(sent[1]!.key)
  })

  it('follows a 202 to its end, then reads the task', async () => {
    const reads = countReads()
    server.use(
      http.post(ANSWER_URL, () =>
        acceptOperation('task_answer', 'flow:run-prompt', { run_id: 'run-prompt', task_id: PROMPT_TASK_ID, outcome: 'answered' }),
      ),
    )
    const { user } = draw()
    await waitFor(() => expect(reads.length).toBeGreaterThan(0))
    const before = reads.length
    await answer(user, 'jo@example.com')
    // The row may say open a moment longer: the card does not.
    expect(await screen.findByText('Answered')).toBeInTheDocument()
    await waitFor(() => expect(reads.length).toBeGreaterThan(before))
    noForm()
  })

  it('closes when the followed operation says the task is not open', async () => {
    const op = { id: 'op-task', kind: 'task_answer', subject: 'flow:run-prompt', created_at: '2026-10-10T09:00:00Z' }
    server.use(
      http.post(ANSWER_URL, () => HttpResponse.json({ ...op, status: 'running', repeated: false }, { status: 202 })),
      http.get('/api/v1/operations/op-task', () =>
        HttpResponse.json({
          ...op,
          status: 'failed',
          finished_at: op.created_at,
          error: { type: NOT_OPEN, title: 'Conflict', status: 409, detail: 'That was answered already. Read the task again.' },
        }),
      ),
    )
    const { user } = draw()
    await answer(user, 'jo@example.com')
    expect(await screen.findByText(ELSEWHERE)).toBeInTheDocument()
    noForm()
  })

  it('says why when the followed operation fails another way', async () => {
    server.use(http.post(ANSWER_URL, () => acceptOperation('task_answer', 'flow:run-prompt', {}, 'The run is busy.')))
    const { user } = draw()
    await answer(user, 'jo@example.com')
    expect(await screen.findByRole('alert')).toHaveTextContent('The run is busy.')
    expect(email()).toHaveValue('jo@example.com')
  })

  it('keeps what the person typed when the page is read again, and drops it once the task is not open', async () => {
    const realtime = fakeRealtime()
    const { user, again } = draw()
    await user.type(email(), 'jo@')
    again(structuredClone(question()))
    setTask(PROMPT_TASK_ID, {})
    await realtime.signal(taskTopic(PROMPT_TASK_ID), 'task.changed')
    await act(() => vi.advanceTimersByTimeAsync(500))
    expect(email()).toHaveValue('jo@')
    setTask(PROMPT_TASK_ID, { state: 'answered' })
    again(question())
    noForm()
    setTask(PROMPT_TASK_ID, { state: 'open', updated_at: new Date(Date.now() + 1000).toISOString() })
    again(question())
    expect(email()).toHaveValue('')
  })

  it('gives another task its own form', async () => {
    const { user, again } = draw()
    await user.type(email(), 'jo@')
    again(question({ id: '00000000-0000-4000-8000-000000000000' }))
    expect(email()).toHaveValue('')
  })

  it('keeps the form when the task cannot be read', async () => {
    const read = vi.fn()
    server.use(
      http.get('/api/v1/tasks/:id', () => {
        read()
        return HttpResponse.json({ title: 'Unavailable', status: 503 }, { status: 503 })
      }),
    )
    draw()
    await waitFor(() => expect(read).toHaveBeenCalled())
    expect(email()).toBeInTheDocument()
  })
})
