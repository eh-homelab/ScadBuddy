import { HttpResponse, http } from 'msw'
import { resolveCodeText } from './filament'
import type { AnswerProblem, Task, TaskQuestion } from '../../api/types'

/**
 * #7364 — a flow prompt's task (backend `api/tasks.py`): the read, and the answer. The
 * check here is a rough stand-in for the server's (`tasks/validate.py`): a field left out,
 * a name the schema does not have, a value's type, an `enum`, and an `email` or
 * `date-time` that is not one. A task that is not open is the backend's 409 `not-open`.
 * Every answer is kept with its `Idempotency-Key` for tests (`taskAnswers`), and the same
 * key again returns the task as it stands.
 */

/** The task `run-prompt` (`flows.ts`) waits on. */
export const PROMPT_TASK_ID = '5f0c7a52-3c1d-4c53-9d0e-7b1a2f6e8c41'

const INVALID = 'https://scadbuddy.dev/problems/tasks/invalid-answer'
const NOT_OPEN = 'https://scadbuddy.dev/problems/tasks/not-open'
const at = (minutes: number) => new Date(Date.UTC(2026, 9, 10, 9, minutes)).toISOString()

function defaults(): Task[] {
  return [
    {
      id: PROMPT_TASK_ID,
      run_id: 'run-prompt',
      title: 'Where do we send the pickup note?',
      schema: {
        type: 'object',
        properties: {
          email: { type: 'string', format: 'email', title: 'Email', description: "The customer's address" },
          pickup_at: { type: 'string', format: 'date-time', title: 'Pickup time' },
          language: { type: 'string', enum: ['English', 'French'], default: 'English', title: 'Language' },
          also_sms: { type: 'boolean', title: 'Also send a text message' },
        },
        required: ['email'],
      },
      context: [
        {
          order: 'SB-1042',
          customer: 'Jo Park',
          items: ['name keychain', 'cable clip'],
          note: 'Prefers a pickup after work.\nCall first if the print is late.',
          pickup_instructions_from_the_storefront_order_form: 'Ring the side bell',
        },
        {
          history: [
            'Oct 2: order placed',
            'Oct 3: deposit paid',
            'Oct 5: first proof sent',
            'Oct 6: customer asked for a thicker loop',
            'Oct 6: second proof sent',
            'Oct 7: proof approved',
            'Oct 8: keychain printed',
            'Oct 9: clip printed',
            'Oct 9: clip reprinted, the first one warped',
            'Oct 10: both parts checked',
            'Oct 10: packed',
          ].join('\n'),
        },
      ],
      place: null,
      ui: null,
      expires_at: new Date(Date.now() + 2 * 3_600_000).toISOString(),
      state: 'open',
      answer: null,
      answered_by: null,
      admitted_by: null,
      created_at: at(2),
      updated_at: at(2),
    },
  ]
}

let tasks = defaults()
let answers: { id: string; key: string | null; body: unknown }[] = []
const seen = new Set<string>()
const listeners = new Set<(task: Task) => void>()

export function reset(): void {
  tasks = defaults()
  answers = []
  seen.clear()
}

/** Called with each task an answer ends: its run moves on. */
export function onTaskAnswered(listener: (task: Task) => void): void {
  listeners.add(listener)
}

/** Tests: every answer sent, in order. */
export function taskAnswers(): readonly { id: string; key: string | null; body: unknown }[] {
  return answers
}

/** Tests: a task as the server has it now; `updated_at` moves on with it. */
export function setTask(id: string, fields: Partial<Task>): void {
  tasks = tasks.map((task) => (task.id === id ? { ...task, updated_at: new Date().toISOString(), ...fields } : task))
}

/** Tests: adds a task, as the server would hold it. */
export function addTask(task: Partial<Task> & Pick<Task, 'id' | 'title'>): Task {
  const made: Task = {
    run_id: 'run-station',
    schema: { type: 'object', properties: {} },
    context: [],
    place: null,
    ui: null,
    expires_at: null,
    state: 'open',
    answer: null,
    answered_by: null,
    admitted_by: null,
    created_at: at(1),
    updated_at: at(1),
    ...task,
  }
  tasks = [...tasks, made]
  return made
}

/** A task as a run's page has it: without its answer. Null for a call that has none. */
export function taskQuestion(id: string): TaskQuestion | null {
  const found = tasks.find((task) => task.id === id)
  if (!found) return null
  const { answer: _answer, answered_by: _by, ...question } = found
  return question
}

function problem(status: number, detail: string, extra: Record<string, unknown> = {}) {
  return HttpResponse.json(
    { title: 'Refused', status, detail, ...extra },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

const FITS: Record<string, (value: unknown) => boolean> = {
  string: (value) => typeof value === 'string',
  integer: (value) => Number.isInteger(value),
  number: (value) => typeof value === 'number',
  boolean: (value) => typeof value === 'boolean',
  array: Array.isArray,
}
const FORMATS: Record<string, RegExp> = {
  email: /^(?=.{1,254}$)[^\s@,;<>()[\]\\"\p{Cc}]+@[^\s@,;<>()[\]\\".\p{Cc}]+(\.[^\s@,;<>()[\]\\".\p{Cc}]+)+$/u,
  date: /^\d{4}-\d{2}-\d{2}$/,
  'date-time': /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/,
}

function problemsOf(schema: Task['schema'], answer: Record<string, unknown>): AnswerProblem[] {
  const properties = (schema['properties'] ?? {}) as Record<string, Record<string, unknown>>
  const found: AnswerProblem[] = []
  for (const name of (schema['required'] ?? []) as string[]) {
    if (!(name in answer)) found.push({ field: '', message: `${name} is required` })
  }
  for (const [name, value] of Object.entries(answer)) {
    const prop = properties[name]
    if (!prop) {
      found.push({ field: '', message: 'has a field the schema does not allow' })
      continue
    }
    const type = String(prop['type'])
    const format = FORMATS[String(prop['format'])]
    if (!FITS[type]?.(value)) found.push({ field: name, message: `must be ${type}` })
    else if (Array.isArray(prop['enum']) && !prop['enum'].includes(value)) {
      found.push({ field: name, message: 'not one of the allowed values' })
    } else if (format && !format.test(String(value))) {
      found.push({ field: name, message: `not a valid ${String(prop['format'])}` })
    }
  }
  return found
}

export const handlers = [
  http.get('/api/v1/tasks', ({ request }) => {
    const query = new URL(request.url).searchParams
    const state = query.get('state') ?? 'open'
    const place = query.get('place')
    const code = query.get('code')
    const resolved = code ? resolveCodeText(code) : null
    if (code && !resolved) return problem(422, 'That is not a code.')
    // The server's rule: the code is one of a scan field's `codes`, or its kind one of `kinds`.
    const waits = (task: Task) =>
      Object.values((task.schema['properties'] ?? {}) as Record<string, Record<string, unknown>>).some((prop) => {
        const expects = prop['expects'] as { kinds?: string[]; codes?: string[] } | undefined
        return (
          prop['x-sb-widget'] === 'scan' &&
          !!expects &&
          !!resolved &&
          (!!expects.kinds?.includes(resolved.kind) || !!expects.codes?.some((one) => one.toUpperCase() === resolved.code))
        )
      })
    const found = tasks
      .filter((task) => task.state === state && (!place || task.place === place) && (!code || (task.state === 'open' && waits(task))))
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
      .map(({ answer: _answer, answered_by: _by, ...question }) => question)
    return HttpResponse.json({ tasks: found })
  }),
  http.get('/api/v1/tasks/:id', ({ params }) => {
    const found = tasks.find((task) => task.id === params['id'])
    return found ? HttpResponse.json(found) : problem(404, 'No such task.')
  }),
  http.post('/api/v1/tasks/:id/answer', async ({ request, params }) => {
    const id = String(params['id'])
    const key = request.headers.get('Idempotency-Key')
    const body = (await request.json()) as { answer: Record<string, unknown> }
    answers.push({ id, key, body })
    const found = tasks.find((task) => task.id === id)
    if (!found) return problem(404, 'No such task.')
    if (key && seen.has(key)) return HttpResponse.json(found)
    if (found.state !== 'open') return problem(409, `The task is ${found.state}.`, { type: NOT_OPEN, state: found.state })
    const problems = problemsOf(found.schema, body.answer)
    if (problems.length > 0) return problem(422, 'The answer does not fit the question.', { type: INVALID, problems })
    if (key) seen.add(key)
    setTask(id, { state: 'answered', answer: body.answer, answered_by: 'browser' })
    for (const listener of listeners) listener(found)
    return HttpResponse.json(tasks.find((task) => task.id === id))
  }),
]
