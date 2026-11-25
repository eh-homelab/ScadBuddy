import type { FlowPending, FlowQueueStep } from '../../api/types'
import { answererLabel, sessionHref } from '../../lib/flows'

// What every card of a run's parked calls shares.

/** A box of text set apart: a proposal, what a session wrote. */
export const INSET = 'rounded-lg bg-muted/50 p-2 ring-1 ring-foreground/10'

/** A call waiting on the person: a card marked as needing them. */
export const WAITING = 'mb-3 ring-destructive/40'

export interface CardProps {
  entry: FlowPending
  runId: string
  /** The run's execution now (`workflow_run_id`), which a Reset changes; null when not known. */
  execution: string | null
  step: FlowQueueStep | null
  respond: (send: () => Promise<unknown>) => Promise<void>
}

/** #2482 — the run's step a call came from: its own, or the one that drives a child session. */
export function StepLine({ step, child = false }: { step: FlowQueueStep; child?: boolean }) {
  return (
    <p className="mb-2 text-[12px] text-muted-foreground">
      {child ? 'From a session the run talks to, at step' : 'Step'} {step.seq} ({step.fn})
    </p>
  )
}

/** #7471 — who answered a flow's question; the assistant links its session. */
export function AnsweredBy({ by }: { by: string | null | undefined }) {
  const { text, sessionId } = answererLabel(by)
  return sessionId ? (
    <a href={sessionHref(sessionId)} className="underline underline-offset-2">
      {text}
    </a>
  ) : (
    <span>{text}</span>
  )
}

/** #7471 — a question the flow asked of people only: no assistant answers it, whatever the settings say. */
export function PeopleOnly() {
  return <p className="mb-2 text-[12px] text-muted-foreground">The flow asks this of people only.</p>
}
