import { useEffect, useRef, useState, type FocusEvent } from 'react'
import { api } from '../../api/client'
import type { TaskQuestion } from '../../api/types'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { taskTopic, waitingText } from '../../lib/flows'
import { useAsync } from '../../lib/useAsync'
import { useLatest } from '../../lib/useLatest'
import { AnsweredBy, PeopleOnly, StepLine, WAITING, type CardProps } from './pendingParts'
import { ELSEWHERE, TaskPanel, type Closed } from './TaskPanel'

const STATE_TEXT: Record<Exclude<TaskQuestion['state'], 'open'>, string> = {
  answered: 'Answered',
  expired: 'Timed out',
  cancelled: 'Cancelled',
}

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
        <TaskPanel
          task={task}
          open={open}
          respond={respond}
          card={card}
          onClosed={(by) => setClosed({ by, at: at.current })}
          onSettled={() => read.refresh()}
        />
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
