import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { printRunPoll } from '../../api/client'
import type { FlowPending, TaskQuestion } from '../../api/types'
import { taskTopic } from '../../lib/flows'
import { bridge } from '../../agent/bridge'
import { useGlobalAgentTools } from '../../agent/global'
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
    people_only: false,
  }
}

const question = (fields: Partial<TaskQuestion> = {}): TaskQuestion => ({ ...taskQuestion(PROMPT_TASK_ID)!, ...fields })

/** The assistant's tools, as the app shell has them. */
function Tools() {
  useGlobalAgentTools()
  return null
}

/**
 * The card as a run's page draws it; `again` is the page read again, with the task as given
 * at an execution of the run.
 */
function draw(task: TaskQuestion | null = question()) {
  const respond = vi.fn(async (send: () => Promise<unknown>) => {
    await send()
  })
  const card = (next: TaskQuestion | null, execution: string) => {
    const entry = pending(next)
    return (
      <>
        <Tools />
        <button type="button">Elsewhere</button>
        <PendingCard key={entry.call_id} entry={entry} runId="run-prompt" execution={execution} step={null} respond={respond} />
      </>
    )
  }
  const view = renderPage(card(task, 'wr-1'))
  return {
    ...view,
    respond,
    again: (next: TaskQuestion | null = question(), execution = 'wr-1') => view.rerender(card(next, execution)),
  }
}

/** The answer route, held until `release` is called. */
function hold() {
  let release!: (reply: Response) => void
  const reply = new Promise<Response>((resolve) => (release = resolve))
  const sent = vi.fn()
  server.use(
    http.post(ANSWER_URL, async () => {
      sent()
      return reply
    }),
  )
  return { release, sent }
}

const refusal = (status: number, extra: Record<string, unknown> = {}) =>
  HttpResponse.json(
    { title: 'Refused', status, detail: `refused ${status}`, ...extra },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )

/** A followed operation that failed with `error`. */
function failsLater(error: Record<string, unknown>) {
  const op = { id: 'op-task', kind: 'task_answer', subject: 'flow:run-prompt', created_at: '2026-10-10T09:00:00Z' }
  server.use(
    http.post(ANSWER_URL, () => HttpResponse.json({ ...op, status: 'running', repeated: false }, { status: 202 })),
    http.get('/api/v1/operations/op-task', () =>
      HttpResponse.json({ ...op, status: 'failed', finished_at: op.created_at, error: { title: 'Conflict', ...error } }),
    ),
  )
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
        return refusal(status, extra)
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

  it.each(['a@b', 'jo@', 'jo@example..com', 'jo@doe@example.com'])('is told that %s is not a valid email', async (address) => {
    const { user } = draw()
    await answer(user, address)
    await waitFor(() => expect(email()).toHaveAccessibleDescription("The customer's address not a valid email"))
    expect(email()).toHaveValue(address)
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

  it('names the assistant that answered, and the run grant that let it (#7471)', async () => {
    setTask(PROMPT_TASK_ID, { state: 'answered', answer: {}, answered_by: 'session:s-9', admitted_by: 'grant-1' })
    draw(question({ state: 'answered' }))
    expect(await screen.findByText("under the run's grant")).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /assistant/i })).toHaveAttribute('href', expect.stringContaining('s-9'))
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

  it('asks again when the question is open again, with no other state seen', async () => {
    refuse(409, { type: NOT_OPEN, state: 'open' }, { once: true })
    const { user, again } = draw()
    await answer(user, 'jo@example.com')
    expect(await screen.findByText(ELSEWHERE)).toBeInTheDocument()
    again(question({ updated_at: new Date(Date.now() + 1000).toISOString() }))
    expect(email()).toHaveValue('')
  })

  it('asks again at another execution of the run, with the same row', async () => {
    refuse(409, { type: NOT_OPEN, state: 'open' }, { once: true })
    const { user, again } = draw()
    await answer(user, 'jo@example.com')
    expect(await screen.findByText(ELSEWHERE)).toBeInTheDocument()
    again(question(), 'wr-1')
    noForm()
    again(question(), 'wr-2')
    expect(email()).toHaveValue('')
  })

  it('never sends the key of another execution', async () => {
    const keys: (string | null)[] = []
    server.use(
      http.post(ANSWER_URL, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return HttpResponse.error()
      }),
    )
    const { user, again } = draw()
    await answer(user, 'jo@example.com')
    await screen.findByRole('alert')
    again(question(), 'wr-2')
    await answer(user, 'jo@example.com')
    await waitFor(() => expect(keys).toHaveLength(4))
    expect(keys[0]).toBe(keys[1])
    expect(keys[2]).toBe(keys[3])
    expect(keys[2]).not.toBe(keys[0])
  })

  it('moves to the card when the form closes, and says why where a status already was', async () => {
    refuse(409, { type: NOT_OPEN, state: 'open' })
    const { user } = draw()
    const status = screen.getByRole('status')
    expect(status).toBeEmptyDOMElement()
    await answer(user, 'jo@example.com')
    await waitFor(() => expect(status).toHaveTextContent(ELSEWHERE))
    expect(status).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Where do we send the pickup note?' })).toHaveFocus()
  })

  it('moves to the card when the question ends while the person types', async () => {
    const { user, again } = draw()
    await user.type(email(), 'jo')
    again(question({ state: 'expired', updated_at: new Date(Date.now() + 1000).toISOString() }))
    expect(screen.getByRole('region', { name: 'Where do we send the pickup note?' })).toHaveFocus()
  })

  it('leaves the focus where the person is', async () => {
    const { user, again } = draw()
    const ended = question({ state: 'expired', updated_at: new Date(Date.now() + 1000).toISOString() })
    await user.type(email(), 'jo')
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' })
    await user.click(elsewhere)
    again(ended)
    expect(elsewhere).toHaveFocus()
  })

  it('does not take back a focus the person moved away', async () => {
    const { user, again } = draw()
    await user.type(email(), 'jo')
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' })
    await user.click(elsewhere)
    act(() => elsewhere.blur())
    again(question({ state: 'expired', updated_at: new Date(Date.now() + 1000).toISOString() }))
    expect(document.body).toHaveFocus()
  })

  it('does not take back a focus the person left for no control', async () => {
    const { user, again } = draw()
    await user.type(email(), 'jo')
    await user.click(document.body)
    expect(document.body).toHaveFocus()
    again(question({ state: 'expired', updated_at: new Date(Date.now() + 1000).toISOString() }))
    expect(document.body).toHaveFocus()
  })

  it('keeps the focus for a person whose window is elsewhere', async () => {
    const { user, again } = draw()
    await user.type(email(), 'jo')
    const windowFocus = vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    stops.push(() => windowFocus.mockRestore())
    fireEvent.blur(email())
    again(question({ state: 'expired', updated_at: new Date(Date.now() + 1000).toISOString() }))
    expect(screen.getByRole('region', { name: 'Where do we send the pickup note?' })).toHaveFocus()
  })

  it('moves to the card without scrolling the page', async () => {
    const { user, again } = draw()
    await user.type(email(), 'jo')
    const focus = vi.spyOn(screen.getByRole('region'), 'focus')
    again(question({ state: 'expired', updated_at: new Date(Date.now() + 1000).toISOString() }))
    expect(focus.mock.calls).toEqual([[{ preventScroll: true }]])
  })

  it('takes no focus from a page the person has not touched', () => {
    const { again } = draw()
    again(question({ state: 'expired', updated_at: new Date(Date.now() + 1000).toISOString() }))
    expect(document.body).toHaveFocus()
  })

  it.each([
    ['not-open', 409, { type: NOT_OPEN, state: 'answered' }, ELSEWHERE],
    ['invalid', 422, { type: 'https://scadbuddy.dev/problems/tasks/invalid-answer', problems: [] }, 'refused 422'],
    ['stale', 409, { type: 'https://scadbuddy.dev/problems/stale-entry' }, 'This request is out of date. The run has moved on.'],
    ['another', 503, {}, 'refused 503'],
  ])('says a refusal that comes after the card has gone in a toast (%s)', async (_, status, extra, said) => {
    const { release, sent } = hold()
    const { user, unmount } = draw()
    await answer(user, 'jo@example.com')
    await waitFor(() => expect(sent).toHaveBeenCalled())
    unmount()
    expect(getToasts()).toEqual([])
    release(refusal(status, extra))
    await waitFor(() =>
      expect(getToasts().map((t) => [t.title, t.detail])).toEqual([[said, 'Where do we send the pickup note?']]),
    )
  })

  it('says an answer landed after the card has gone', async () => {
    const { release, sent } = hold()
    const { user, unmount } = draw()
    await answer(user, 'jo@example.com')
    await waitFor(() => expect(sent).toHaveBeenCalled())
    unmount()
    release(HttpResponse.json({ ...question(), state: 'answered', answer: {}, answered_by: 'browser' }))
    await waitFor(() => expect(getToasts().map((t) => t.title)).toEqual(['Answer sent']))
  })

  it.each([
    ['refused as answered', 409, { type: NOT_OPEN, state: 'answered' }, ELSEWHERE, []],
    ['refused another way', 503, {}, 'Answered', ['refused 503']],
  ])('follows a question answered while an answer is in flight, then %s', async (_, status, extra, said, toasts) => {
    const realtime = fakeRealtime()
    const { release, sent } = hold()
    const { user } = draw()
    await answer(user, 'jo@example.com')
    await waitFor(() => expect(sent).toHaveBeenCalled())
    setTask(PROMPT_TASK_ID, { state: 'answered' })
    await realtime.signal(taskTopic(PROMPT_TASK_ID), 'task.changed')
    expect(await screen.findByText('Answered', {}, { timeout: 3000 })).toBeInTheDocument()
    noForm()
    release(refusal(status, extra))
    await waitFor(() => expect(getToasts().map((t) => t.title)).toEqual(toasts))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(said))
    noForm()
  })

  it('gives each of two questions its own draft and key', async () => {
    const sent: { url: string; key: string | null }[] = []
    server.use(
      http.post(ANSWER_URL, ({ request }) => {
        sent.push({ url: new URL(request.url).pathname, key: request.headers.get('Idempotency-Key') })
        return HttpResponse.error()
      }),
    )
    const other = question({ id: '00000000-0000-4000-8000-000000000000', title: 'And the other one?' })
    const { user } = renderPage(
      <>
        {[question(), other].map((task) => (
          <PendingCard
            key={task.id}
            entry={pending(task)}
            runId="run-prompt"
            execution="wr-1"
            step={null}
            respond={async (send) => void (await send())}
          />
        ))}
      </>,
    )
    const cards = [/pickup note/, /other one/].map((name) => within(screen.getByRole('region', { name })))
    await user.type(cards[0]!.getByLabelText(/^Email/), 'jo@example.com')
    expect(cards[1]!.getByLabelText(/^Email/)).toHaveValue('')
    await user.type(cards[1]!.getByLabelText(/^Email/), 'jo@example.com')
    for (const card of cards) {
      await user.click(card.getByRole('button', { name: 'Answer' }))
      await card.findByRole('alert')
    }
    expect(sent.map((s) => s.url)).toEqual([
      `/api/v1/tasks/${PROMPT_TASK_ID}/answer`,
      `/api/v1/tasks/${PROMPT_TASK_ID}/answer`,
      `/api/v1/tasks/${other.id}/answer`,
      `/api/v1/tasks/${other.id}/answer`,
    ])
    expect(new Set(sent.map((s) => s.key)).size).toBe(2)
    expect(sent[0]!.key).toBe(sent[1]!.key)
  })

  it('has a context a keyboard reaches', async () => {
    const { user } = draw()
    await user.tab()
    await user.tab()
    expect(screen.getByRole('group', { name: 'Context' })).toHaveFocus()
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
    failsLater({ type: NOT_OPEN, status: 409, detail: 'That was answered already. Read the task again.' })
    const { user } = draw()
    await answer(user, 'jo@example.com')
    expect(await screen.findByText(ELSEWHERE)).toBeInTheDocument()
    noForm()
  })

  it('says the run has moved on when the followed operation does', async () => {
    failsLater({ type: 'https://scadbuddy.dev/problems/stale-entry', status: 409, detail: 'Another execution.' })
    const { user } = draw()
    await answer(user, 'jo@example.com')
    expect(await screen.findByRole('alert')).toHaveTextContent('This request is out of date. The run has moved on.')
    expect(email()).toHaveValue('jo@example.com')
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

// Spec §6.6: a person answers a flow's question, never the assistant through the person's tab.
describe('PendingCard and the assistant', () => {
  async function refused(tool: string, args: unknown) {
    await waitFor(() => expect(bridge.liveNames()).toContain('click'))
    const outcome = await bridge.call(tool, args)
    expect(!outcome.ok && outcome.error.code).toBe('refused')
    return !outcome.ok && outcome.error.message
  }

  it('may not fill or send a prompt’s form, or read its draft', async () => {
    const { user } = draw()
    await user.type(email(), 'draft@example.com')
    expect(await refused('fill', { label: 'Email *email', value: 'agent@example.com' })).toBe('This is for the user to fill in.')
    await refused('fill', { label: 'Languagelanguage', value: 'French' })
    await refused('click', { role: 'button', name: 'Answer' })
    await refused('click', { role: 'checkbox', name: 'Also send a text messagealso_sms' })
    // Settings' own tool does not reach a run's page.
    expect((await bridge.call('set_field', { field: 'bambuddy_url', value: 'x' })).ok).toBe(false)
    expect(email()).toHaveValue('draft@example.com')
    expect(taskAnswers()).toEqual([])
    const snapshot = bridge.snapshot()
    expect(JSON.stringify(snapshot)).not.toContain('draft@example.com')
    expect(snapshot.elements).toContainEqual(expect.objectContaining({ name: 'Answer', userOnly: true }))
  })

  it('may not pick from a prompt’s open list', async () => {
    const { user } = draw()
    const language = screen.getByRole('combobox', { name: /Language/ })
    await user.click(language)
    await screen.findByRole('option', { name: 'French' })
    await refused('click', { role: 'option', name: 'French' })
    await user.click(screen.getByRole('option', { name: 'French' }))
    expect(language).toHaveTextContent('French')
  })

  it('may not fill or send a text answer, or read its draft; the person may', async () => {
    const sent: unknown[] = []
    server.use(
      http.post('/api/v1/workflow-runs/:id/answer', async ({ request }) => {
        sent.push(await request.json())
        return HttpResponse.json({})
      }),
    )
    const { user } = draw(null)
    const box = screen.getByRole('textbox', { name: 'Your answer' })
    await user.type(box, 'my draft')
    await refused('fill', { label: 'Your answer', value: 'agent' })
    await refused('click', { role: 'button', name: 'Answer' })
    expect(box).toHaveValue('my draft')
    expect(JSON.stringify(bridge.snapshot())).not.toContain('my draft')
    fireEvent.submit(box.closest('form')!)
    await waitFor(() => expect(sent).toEqual([expect.objectContaining({ answer: 'my draft' })]))
  })
})
