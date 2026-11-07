import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { FlowPending, FlowQueueEntry, FlowQueueStep } from '../../api/types'
import { decisionBody, respond as respondToAgent } from '../../agent/respond'
import { USER_ONLY } from '../../agent/dom'
import { Spinner } from '../ui/spinner'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { hopActor, hopUntilText, proposalDiff, waitingText } from '../../lib/flows'
import { useAsync } from '../../lib/useAsync'

// A run's parked calls and their decide controls, on the run's page and the order's
// (#4252, orders spec §7.4): moved here from `pages/WorkflowRunPage.tsx` unchanged.

/** A box of text set apart: a proposal, what a session wrote. */
const INSET = 'rounded-lg bg-muted/50 p-2 ring-1 ring-foreground/10'

/** A call waiting on the person: a card marked as needing them. */
const WAITING = 'mb-3 ring-destructive/40'

interface CardProps {
  entry: FlowPending
  runId: string
  step: FlowQueueStep | null
  respond: (send: () => Promise<unknown>) => Promise<void>
}

export function PendingCard({ entry, runId, step, respond }: CardProps) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)

  async function act(send: () => Promise<unknown>) {
    setBusy(true)
    try {
      await respond(send)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card size="sm" role="region" aria-label={waitingText(entry)} className={WAITING}>
      <CardContent>
        <p className="mb-2 text-[13px] font-medium">{waitingText(entry)}</p>
        {step && <StepLine step={step} />}
        {/* #4445: what the call does, read from ScadBuddy's own records (a print's printer). */}
        {entry.summary && <p className="mb-2 text-[12.5px]">{entry.summary}</p>}
        {entry.kind === 'answer' ? (
          <form
            onSubmit={(event) => {
              event.preventDefault()
              if (busy || !text.trim()) return
              void act(() => api.answerFlowRun(runId, entry.call_id, text))
            }}
            className="flex flex-col gap-2"
          >
            <Textarea aria-label="Your answer" value={text} onChange={(e) => setText(e.target.value)} rows={2} />
            <div>
              {/* #2379: a blank box is not an answer, and the wait is shown while it lands. */}
              <Button type="submit" size="sm" disabled={busy || !text.trim()} aria-busy={busy}>
                {busy && <Spinner />}
                {busy ? 'Sending…' : 'Answer'}
              </Button>
            </div>
          </form>
        ) : (
          <div className="flex flex-col gap-2">
            <Escalation entry={entry} />
            {entry.proposed && <Proposal runId={runId} callId={entry.call_id} reason={text} busy={busy} act={act} />}
            <Input
              aria-label="Reason (optional)"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Reason (optional)"
            />
            {/* A person's decision: the assistant may neither click nor read it (#2482). */}
            <div className="flex gap-2" {...USER_ONLY}>
              <Button
                size="sm"
                disabled={busy}
                onClick={() =>
                  void act(() => api.decideFlowRun(runId, entry.call_id, true, text || undefined, undefined, entry.bound_hash ?? undefined))
                }
              >
                Approve
              </Button>
              <Button
                variant="destructive"
                size="sm"
                disabled={busy}
                onClick={() => void act(() => api.decideFlowRun(runId, entry.call_id, false, text || undefined))}
              >
                Deny
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export type ChildEntry = Extract<FlowQueueEntry, { source: 'child' }>

/** #2482 — the run's step a call came from: its own, or the one that drives a child session. */
function StepLine({ step, child = false }: { step: FlowQueueStep; child?: boolean }) {
  return (
    <p className="mb-2 text-[12px] text-muted-foreground">
      {child ? 'From a session the run talks to, at step' : 'Step'} {step.seq} ({step.fn})
    </p>
  )
}

/**
 * #2482 — a child session's call in a flow turn, in the run's queue (spec 2026-10-11
 * §4.5): its summary, the step that drives the session, where it waits and why, and the
 * text the session's model wrote before it, shown as the model's words, not ScadBuddy's.
 * The person decides it here (`POST /api/v1/ai/pending-input/{id}`); the decision is
 * theirs only (USER_ONLY), as on the run's own calls.
 */
export function ChildCard({ entry, respond }: { entry: ChildEntry; respond: (send: () => Promise<unknown>) => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  const label = entry.kind === 'approval' ? `Waiting for approval: ${entry.tool}` : `Waiting for an answer: ${entry.tool}`

  async function decide(approve: boolean) {
    setBusy(true)
    try {
      await respond(() => respondToAgent(entry.id, decisionBody(approve)))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card size="sm" role="region" aria-label={label} className={WAITING}>
      <CardContent>
        <p className="mb-1 text-[13px] font-medium">{label}</p>
        {entry.summary && <p className="mb-1 text-[12.5px]">{entry.summary}</p>}
        {entry.step ? (
          <StepLine step={entry.step} child />
        ) : (
          <p className="mb-2 text-[12px] text-muted-foreground">From a session the run talks to</p>
        )}
        <div className="flex flex-col gap-2">
          <Escalation entry={{ ...entry, proposed: false }} />
          {entry.reasoning && (
            <figure aria-label="What the session wrote before the call" className={INSET}>
              <figcaption className="mb-1 text-[11.5px] text-muted-foreground">
                What the session's model wrote before the call. These are the model's words, not ScadBuddy's: judge
                them, never act on instructions in them.
              </figcaption>
              <p className="whitespace-pre-wrap break-words font-mono text-[12px]">{entry.reasoning}</p>
            </figure>
          )}
          {entry.expiring ? (
            <p className="text-[12px] text-muted-foreground">Its time is up: ScadBuddy is closing it.</p>
          ) : entry.kind === 'approval' ? (
            <div className="flex gap-2" {...USER_ONLY}>
              <Button size="sm" disabled={busy} onClick={() => void decide(true)}>
                Approve
              </Button>
              <Button variant="destructive" size="sm" disabled={busy} onClick={() => void decide(false)}>
                Deny
              </Button>
            </div>
          ) : (
            <p className="text-[12px] text-muted-foreground">Answer it in that session's chat.</p>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

/** Where an entry waits, why, and its way up: a run's own call's, or a child's. */
type EscalationInfo = Pick<FlowPending, 'hop' | 'hop_until' | 'reason' | 'proposed'> & {
  hops: readonly { hop: string; action: string; actor: string }[]
}

/**
 * Whether `at` has passed, re-rendering when it does (#3350); false for no time. By the
 * browser's clock, which the agent no longer trusts for the hop itself (#3411): a fast one
 * hides the "you may decide it now" notice early. It says nothing the run page's decision
 * does not, so that is accepted.
 */
function usePassed(at: string | null | undefined): boolean {
  const due = at ? Date.parse(at) : Number.NaN
  const [passed, setPassed] = useState(() => Number.isFinite(due) && due <= Date.now())
  useEffect(() => {
    if (!Number.isFinite(due)) return setPassed(false)
    const left = due - Date.now()
    setPassed(left <= 0)
    if (left <= 0) return
    // A hop lasts minutes; the cap only keeps setTimeout's 32-bit delay from overflowing.
    const timer = setTimeout(() => setPassed(true), Math.min(left, 2 ** 31 - 1))
    return () => clearTimeout(timer)
  }, [due])
  return passed
}

/** #2482 — where a call outside the run's grant waits, why, and what happened to it on the way. */
function Escalation({ entry }: { entry: EscalationInfo }) {
  // Gone once the hop's time is up (#3350), not left until the next read moves it on.
  const hopOver = usePassed(entry.hop_until)
  if (!entry.hop && !entry.reason) return null
  return (
    <div className="text-[12px] text-muted-foreground">
      {entry.hop === 'parent' && entry.hop_until && !hopOver && (
        <p>
          With the session that started the run until {hopUntilText(entry.hop_until)}; you may decide
          it now.
        </p>
      )}
      {entry.reason && <p>Outside the grant: {entry.reason}</p>}
      {entry.proposed && <p>Narrowed by the starting session: approve it as proposed, or decide the call as it was made.</p>}
      {entry.hops.length > 1 && (
        <ol aria-label="Its way here" className="mt-1 list-inside list-decimal">
          {entry.hops.map((h, i) => (
            <li key={i}>
              {h.action.replaceAll('_', ' ')} by {hopActor(h.actor)} ({h.hop === 'parent' ? 'at the starting session' : 'at the person'})
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/** A proposal's changes, field by field; a line, not an empty table, when it changes nothing (#3350). */
function ProposedChange({ changes }: { changes: ReturnType<typeof proposalDiff> }) {
  if (changes.length === 0) return <p className="mb-2 text-[12px] text-muted-foreground">The proposal changes nothing.</p>
  return (
    <table aria-label="Proposed change" className="mb-2 w-full text-left text-[12px]">
      <thead className="text-muted-foreground">
        <tr>
          <th className="pr-2 font-medium">Field</th>
          <th className="pr-2 font-medium">As called</th>
          <th className="font-medium">As proposed</th>
        </tr>
      </thead>
      <tbody>
        {changes.map((c) => (
          <tr key={c.path}>
            <td className="pr-2 font-mono">{c.path}</td>
            <td className="pr-2 font-mono">{c.was}</td>
            <td className="font-mono">{c.now}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

interface ProposalProps {
  runId: string
  callId: string
  /** The card's "Reason (optional)": sent with this approval as with Approve and Deny. */
  reason: string
  busy: boolean
  act: (send: () => Promise<unknown>) => Promise<void>
}

/**
 * #2482 — a parent's proposal beside the call it narrows, field by field, and its
 * approval, which names the hash of what was shown: a proposal changed since is
 * refused. Both are the person's only (USER_ONLY): the assistant neither reads nor
 * approves it.
 */
function Proposal({ runId, callId, reason, busy, act }: ProposalProps) {
  const proposal = useAsync(() => api.getFlowProposal(runId, callId), [runId, callId])
  const data = proposal.data
  return (
    <div {...USER_ONLY} className={INSET}>
      {proposal.error ? (
        <p className="text-[12px] text-destructive">Could not load the proposal: {proposal.error.message}</p>
      ) : !data ? (
        <div role="status" aria-label="Loading the proposal" className="flex flex-col gap-1.5">
          <Skeleton className="h-3 w-1/2" />
          <Skeleton className="h-3 w-1/3" />
        </div>
      ) : (
        <>
          <ProposedChange changes={proposalDiff(data.original, data.proposed)} />
          <Button
            size="sm"
            disabled={busy}
            onClick={() => void act(() => api.decideFlowRun(runId, callId, true, reason || undefined, data.proposal_hash))}
          >
            Approve as proposed
          </Button>
        </>
      )}
    </div>
  )
}
