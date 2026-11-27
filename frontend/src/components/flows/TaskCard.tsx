import { useEffect, useRef, useState, type FocusEvent, type RefObject } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError, mayHaveRun, newRequestId } from '../../api/client'
import type { AnswerProblem, TaskQuestion } from '../../api/types'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import {
  hopUntilText,
  isStaleEntry,
  STALE_ENTRY_MESSAGE,
  TASK_INVALID_ANSWER,
  TASK_NOT_OPEN,
  taskTopic,
  waitingText,
} from '../../lib/flows'
import { toast } from '../../lib/toast'
import { useAsync } from '../../lib/useAsync'
import { useLatest } from '../../lib/useLatest'
import { usePassed } from '../../lib/usePassed'
import { AnsweredBy, INSET, PeopleOnly, StepLine, WAITING, type CardProps } from './pendingParts'
import { weightExpected } from '../../lib/flowInputs'
import { SchemaForm } from './SchemaForm'

const STATE_TEXT: Record<Exclude<TaskQuestion['state'], 'open'>, string> = {
  answered: 'Answered',
  expired: 'Timed out',
  cancelled: 'Cancelled',
}

const ELSEWHERE = 'This question was answered elsewhere.'

/** Why the form is closed on a task the page may still list as open, and the task's `updated_at` then. */
type Closed = { by: 'sent' | 'elsewhere'; at: string }

function closedText({ state, updated_at }: TaskQuestion, closed: Closed | null): string | null {
  // An open row newer than the one closed here was opened again.
  const by = closed && !(state === 'open' && Date.parse(updated_at) > Date.parse(closed.at)) ? closed.by : null
  if (by === 'elsewhere' && (state === 'open' || state === 'answered')) return ELSEWHERE
  if (state !== 'open') return STATE_TEXT[state]
  return by && STATE_TEXT.answered
}

/**
 * #7364 — a run's prompt: the question, what the flow gave with it, and the form of its
 * schema. The flow's script wrote all of it, so it is shown as text. The page and the
 * card each read the task; the newer read is shown. Once the form is closed here, a read
 * that says `open` draws it again only when its row is newer.
 */
export function TaskCard({ entry, task: listed, step, respond }: CardProps & { task: TaskQuestion }) {
  const read = useAsync(() => api.getTask(listed.id), [listed.id], [taskTopic(listed.id)])
  const task: TaskQuestion =
    read.data && Date.parse(read.data.updated_at) >= Date.parse(listed.updated_at) ? read.data : listed
  const [closed, setClosed] = useState<Closed | null>(null)
  // An answer closes the task as it is then, not as it was when the answer was sent.
  const at = useLatest(task.updated_at)
  const said = closedText(task, closed)
  const open = said === null
  const card = useRef<HTMLDivElement>(null)
  const inside = useRef(false)

  // The form takes the focus it holds with it: the card has it then.
  useEffect(() => {
    if (!open && inside.current && document.activeElement === document.body) {
      card.current?.focus({ preventScroll: true })
    }
  }, [open])

  function onBlur({ target, relatedTarget }: FocusEvent) {
    // With no next target the person left for a blank part of the page, unless the window
    // lost the focus or the button it was on is now disabled.
    const left = relatedTarget
      ? !card.current?.contains(relatedTarget)
      : document.hasFocus() && !target.matches(':disabled')
    if (left) inside.current = false
  }

  const heading = open ? waitingText({ ...entry, prompt: task.title }) : task.title

  return (
    <Card
      ref={card}
      size="sm"
      role="region"
      aria-label={heading}
      tabIndex={-1}
      onFocus={() => (inside.current = true)}
      onBlur={onBlur}
      className={cn('outline-none focus-visible:ring-3 focus-visible:ring-ring/50', open ? WAITING : 'mb-3')}
    >
      <CardContent>
        <p className="mb-2 text-[13px] font-medium break-words">{heading}</p>
        {step && <StepLine step={step} />}
        {entry.summary && <p className="mb-2 text-[12.5px]">{entry.summary}</p>}
        {open && entry.people_only && <PeopleOnly />}
        <Context items={task.context} />
        {open && (
          <>
            {task.expires_at && <Limit at={task.expires_at} />}
            <AnswerForm
              task={task}
              respond={respond}
              card={card}
              onClosed={(by) => setClosed({ by, at: at.current })}
              onSettled={() => read.refresh()}
            />
          </>
        )}
        {/* Always here: a status is announced when its text changes. */}
        <p role="status" className="text-[12.5px] text-muted-foreground">
          {said}
        </p>
        {/* #7471: an agent's answer names it; a person's is the person's own. */}
        {task.state === 'answered' && /^(session|token):/.test(read.data?.answered_by ?? '') && (
          <p className="text-[12.5px] text-muted-foreground">
            <AnsweredBy by={read.data?.answered_by} admittedBy={read.data?.admitted_by} />
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function AnswerForm({
  task,
  respond,
  card,
  onClosed,
  onSettled,
}: {
  task: TaskQuestion
  respond: CardProps['respond']
  card: RefObject<HTMLElement | null>
  onClosed: (by: Closed['by']) => void
  onSettled: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [problems, setProblems] = useState<AnswerProblem[]>()
  const [error, setError] = useState<string>()
  const form = useRef<HTMLDivElement>(null)
  /** The last answer that may have landed, and its key: the same answer follows it. */
  const unsure = useRef<{ body: string; key: string } | null>(null)

  async function send(answer: Record<string, unknown>) {
    const body = JSON.stringify(answer)
    const key = unsure.current?.body === body ? unsure.current.key : newRequestId()
    setBusy(true)
    setProblems(undefined)
    setError(undefined)
    // It refuses here, beside the form; the page only reads the run again.
    await respond(async () => {
      try {
        await api.answerTask(task.id, answer, key)
        unsure.current = null
        toast({ title: 'Answer sent', detail: task.title })
        onClosed('sent')
      } catch (caught) {
        unsure.current = mayHaveRun(caught) ? { body, key } : null
        const type = caught instanceof ApiError ? caught.problem.type : undefined
        const ended = type === TASK_NOT_OPEN
        const why = ended ? ELSEWHERE : refusalOf(caught)
        if (ended) onClosed('elsewhere')
        else if (type === TASK_INVALID_ANSWER) setProblems(problemsOf(caught as ApiError))
        else setError(why)
        // What would show it has gone.
        if (!(ended ? card : form).current) toast({ title: why, detail: task.title })
      }
    })
    setBusy(false)
    onSettled()
  }

  return (
    // A person's answer (spec §6.6): the assistant cannot press or fill the form, or read the
    // text typed in it. A checkbox's state stays in its snapshot, as for every user-only control.
    <div ref={form} {...USER_ONLY}>
      <SchemaForm
        answer
        schema={task.schema}
        submitLabel="Answer"
        busyLabel="Sending…"
        busy={busy}
        problems={problems}
        error={error}
        taskId={task.id}
        expect={weightExpected(task.context)}
        onSubmit={(answer) => void send(answer)}
      />
    </div>
  )
}

function refusalOf(caught: unknown): string {
  if (isStaleEntry(caught)) return STALE_ENTRY_MESSAGE
  return caught instanceof ApiError ? caught.detail : String(caught)
}

/** A 422's problems; its detail when it names none, so that the refusal is always said. */
function problemsOf(error: ApiError): AnswerProblem[] {
  const named = error.problem['problems']
  return Array.isArray(named) && named.length > 0 ? (named as AnswerProblem[]) : [{ field: '', message: error.detail }]
}

/** When the question stops waiting. */
function Limit({ at }: { at: string }) {
  const passed = usePassed(at)
  return (
    <p className="mb-3 text-[12px] text-muted-foreground">
      {passed ? 'Its time limit has passed.' : `Answer by ${hopUntilText(at)}.`}
    </p>
  )
}

/** A value as text: a list of plain values as they read, anything deeper as JSON. */
function contextText(value: unknown): string {
  if (typeof value === 'string') return value
  if (value === null) return '—'
  const plain = Array.isArray(value) && value.length > 0 && value.every((item) => typeof item !== 'object')
  return plain ? value.join(', ') : JSON.stringify(value)
}

/** What the flow gave with the question: each object's fields, as text and never as markup. */
function Context({ items }: { items: TaskQuestion['context'] }) {
  const shown = items.filter((item) => Object.keys(item).length > 0)
  if (shown.length === 0) return null
  return (
    <div
      role="group"
      aria-label="Context"
      // It scrolls, so the keyboard must reach it.
      tabIndex={0}
      className={`mb-3 flex max-h-64 flex-col gap-2 overflow-y-auto outline-none focus-visible:ring-3 focus-visible:ring-ring/50 ${INSET}`}
    >
      {shown.map((item, i) => (
        <dl key={i} className="grid grid-cols-[fit-content(40%)_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-[12.5px] not-first:border-t not-first:pt-2">
          {Object.entries(item).map(([name, value]) => (
            <div key={name} className="contents">
              <dt className="min-w-0 wrap-anywhere text-muted-foreground">{name}</dt>
              <dd className="min-w-0 wrap-anywhere whitespace-pre-wrap">
                {contextText(value)}
              </dd>
            </div>
          ))}
        </dl>
      ))}
    </div>
  )
}
