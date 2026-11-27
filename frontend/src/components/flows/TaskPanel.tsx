import { useRef, useState, type RefObject } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError, mayHaveRun, newRequestId } from '../../api/client'
import type { AnswerProblem, TaskQuestion } from '../../api/types'
import { weightExpected } from '../../lib/flowInputs'
import {
  hopUntilText,
  isStaleEntry,
  STALE_ENTRY_MESSAGE,
  TASK_INVALID_ANSWER,
  TASK_NOT_OPEN,
} from '../../lib/flows'
import { toast } from '../../lib/toast'
import { usePassed } from '../../lib/usePassed'
import { ContextBlocks } from './ContextBlocks'
import type { ScanOffer } from './widgets/ScanField'
import { SchemaForm } from './SchemaForm'

export const ELSEWHERE = 'This question was answered elsewhere.'

/** Why the form is closed on a task the page may still list as open, and the task's `updated_at` then. */
export type Closed = { by: 'sent' | 'elsewhere'; at: string }

/**
 * #8062 — a task's context, its time limit and its answer form, with no run page around
 * it: the run's card and the station page both draw it. `open` false leaves out the limit
 * and the form. `offer` hands a scanned code to the form's scan field.
 */
export function TaskPanel({
  task,
  open,
  respond,
  card,
  onClosed,
  onSettled,
  offer,
}: {
  task: TaskQuestion
  open: boolean
  respond: (send: () => Promise<unknown>) => Promise<unknown>
  /** The element that goes when the task closes; with none, a refusal is also a toast. */
  card?: RefObject<HTMLElement | null>
  onClosed: (by: Closed['by']) => void
  onSettled: () => void
  offer?: ScanOffer
}) {
  const none = useRef<HTMLElement | null>(null)
  return (
    <>
      <ContextBlocks items={task.context} />
      {open && (
        <>
          {task.expires_at && <Limit at={task.expires_at} />}
          <AnswerForm task={task} respond={respond} card={card ?? none} onClosed={onClosed} onSettled={onSettled} offer={offer} />
        </>
      )}
    </>
  )
}

function AnswerForm({
  task,
  respond,
  card,
  onClosed,
  onSettled,
  offer,
}: {
  task: TaskQuestion
  offer?: ScanOffer
  respond: (send: () => Promise<unknown>) => Promise<unknown>
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
        offer={offer}
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
