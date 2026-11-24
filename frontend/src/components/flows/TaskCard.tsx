import { useRef, useState } from 'react'
import { api, ApiError, mayHaveRun, newRequestId } from '../../api/client'
import type { AnswerProblem, TaskQuestion } from '../../api/types'
import { Card, CardContent } from '@/components/ui/card'
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
import { usePassed } from '../../lib/usePassed'
import { INSET, StepLine, WAITING, type CardProps } from './pendingParts'
import { SchemaForm } from './SchemaForm'

const STATE_TEXT: Record<Exclude<TaskQuestion['state'], 'open'>, string> = {
  answered: 'Answered',
  expired: 'Timed out',
  cancelled: 'Cancelled',
}

/** Why the form is closed on a task the page may still list as open. */
type Closed = { by: 'sent' | 'elsewhere'; left: boolean }

function closedText(state: TaskQuestion['state'], closed: Closed | null): string | null {
  if (closed?.by === 'elsewhere' && (state === 'open' || state === 'answered')) return 'This question was answered elsewhere.'
  if (state !== 'open') return STATE_TEXT[state]
  return closed ? STATE_TEXT.answered : null
}

/**
 * #7364 — a run's prompt: the question, what the flow gave with it, and the form of its
 * schema. The flow's script wrote all of it, so it is shown as text. The page and the
 * card each read the task; the newer read is shown. Once the form is closed here, an older
 * read that says `open` does not draw it again; only a task seen to end and open again does.
 */
export function TaskCard({ entry, task: listed, step, respond }: CardProps & { task: TaskQuestion }) {
  const read = useAsync(() => api.getTask(listed.id), [listed.id], [taskTopic(listed.id)])
  const task: TaskQuestion =
    read.data && Date.parse(read.data.updated_at) >= Date.parse(listed.updated_at) ? read.data : listed
  const [closed, setClosed] = useState<Closed | null>(null)
  if (closed && !closed.left && task.state !== 'open') setClosed({ ...closed, left: true })
  if (closed?.left && task.state === 'open') setClosed(null)
  const said = closedText(task.state, closed)
  const heading = said === null ? waitingText({ ...entry, prompt: task.title }) : task.title

  return (
    <Card size="sm" role="region" aria-label={heading} className={said === null ? WAITING : 'mb-3'}>
      <CardContent>
        <p className="mb-2 text-[13px] font-medium break-words">{heading}</p>
        {step && <StepLine step={step} />}
        {entry.summary && <p className="mb-2 text-[12.5px]">{entry.summary}</p>}
        <Context items={task.context} />
        {said === null ? (
          <>
            {task.expires_at && <Limit at={task.expires_at} />}
            <AnswerForm
              task={task}
              respond={respond}
              onClosed={(by) => setClosed({ by, left: false })}
              onSettled={() => read.refresh()}
            />
          </>
        ) : (
          // A sent answer is told by its toast, which outlives the card.
          <p role={closed?.by === 'sent' ? undefined : 'status'} className="text-[12.5px] text-muted-foreground">
            {said}
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function AnswerForm({
  task,
  respond,
  onClosed,
  onSettled,
}: {
  task: TaskQuestion
  respond: CardProps['respond']
  onClosed: (by: Closed['by']) => void
  onSettled: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [problems, setProblems] = useState<AnswerProblem[]>()
  const [error, setError] = useState<string>()
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
        if (type === TASK_NOT_OPEN) onClosed('elsewhere')
        else if (type === TASK_INVALID_ANSWER) setProblems(problemsOf(caught as ApiError))
        else if (isStaleEntry(caught)) setError(STALE_ENTRY_MESSAGE)
        else setError(caught instanceof ApiError ? caught.detail : String(caught))
      }
    })
    setBusy(false)
    onSettled()
  }

  return (
    <SchemaForm
      answer
      schema={task.schema}
      submitLabel="Answer"
      busyLabel="Sending…"
      busy={busy}
      problems={problems}
      error={error}
      onSubmit={(answer) => void send(answer)}
    />
  )
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
    <div role="group" aria-label="Context" className={`mb-3 flex flex-col gap-2 ${INSET}`}>
      {shown.map((item, i) => (
        <dl key={i} className="grid grid-cols-[fit-content(40%)_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-[12.5px] not-first:border-t not-first:pt-2">
          {Object.entries(item).map(([name, value]) => (
            <div key={name} className="contents">
              <dt className="break-words text-muted-foreground">{name}</dt>
              <dd className="break-words whitespace-pre-wrap">
                {contextText(value)}
              </dd>
            </div>
          ))}
        </dl>
      ))}
    </div>
  )
}
