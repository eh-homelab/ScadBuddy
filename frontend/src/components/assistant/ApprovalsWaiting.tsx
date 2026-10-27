import { useContext, useState } from 'react'
import { ApprovalsWaitingContext, type PendingApproval } from '../../agent/attention'
import { decidePendingApproval, preparedBy } from '../../agent/pendingApprovals'
import { RespondError, type RespondOutcome } from '../../agent/respond'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { RiskBadge } from './badges'

const expiryText = (at: string | undefined) => {
  const when = at ? new Date(at) : undefined
  return when && !Number.isNaN(when.getTime()) ? `Expires at ${when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}.` : ''
}

type CardState =
  | { kind: 'pending'; error?: string }
  | { kind: 'sending' }
  | { kind: 'done'; outcome: RespondOutcome }
  | { kind: 'closed'; reason: string }

function PendingApprovalCard({ approval, onDecided }: { approval: PendingApproval; onDecided: () => void }) {
  const [state, setState] = useState<CardState>({ kind: 'pending' })
  const headingId = `pending-approval-${approval.id}`
  const decide = (approve: boolean) => {
    setState({ kind: 'sending' })
    decidePendingApproval(approval.id, approve).then(
      (outcome) => {
        setState({ kind: 'done', outcome })
        onDecided()
      },
      (err: unknown) => {
        if (err instanceof RespondError && err.settled) {
          setState({ kind: 'closed', reason: err.reason ?? 'the request is no longer waiting' })
          onDecided()
        } else setState({ kind: 'pending', error: err instanceof Error ? err.message : String(err) })
      },
    )
  }
  return (
    <Card
      size="sm"
      role="region"
      aria-labelledby={headingId}
      className="gap-1.5 bg-destructive/5 px-3 text-[13px] ring-destructive/40"
      data-testid="pending-approval"
    >
      <div className="flex items-center gap-2">
        <RiskBadge risk="outward" />
        <h3 id={headingId} className="text-[13px] font-semibold">
          Needs your approval
        </h3>
      </div>
      <p className="font-mono text-[12px] break-all">{preparedBy(approval)}</p>
      {approval.summary && <p className="break-words">{approval.summary}</p>}
      {state.kind === 'pending' ? (
        <>
          <div className="mt-0.5 flex items-center gap-2">
            {/* Human-only: the browser bridge (#254) must never let an agent press these. */}
            <Button size="sm" data-agent-user-only="" onClick={() => decide(true)}>
              Approve
            </Button>
            <Button variant="destructive" size="sm" data-agent-user-only="" onClick={() => decide(false)}>
              Deny
            </Button>
            <span className="text-xs text-muted-foreground">{expiryText(approval.expiresAt)}</span>
          </div>
          {state.error && (
            <p role="alert" className="text-xs text-warn">
              {state.error}
            </p>
          )}
        </>
      ) : (
        <p className="text-xs text-muted-foreground" role="status">
          {state.kind === 'sending'
            ? 'Sending your answer…'
            : state.kind === 'closed'
              ? `Your decision was not taken: ${state.reason}.`
              : state.outcome === 'approved'
                ? 'Approved. The client that prepared it can now confirm it.'
                : 'Denied.'}
        </p>
      )}
    </Card>
  )
}

/**
 * #4813 — the approvals prepared outside any session (an external MCP client's
 * `pending_approval`), which the header's badge counts and no chat feed shows: each with
 * the chat feed's Approve and Deny. Read from the badge's own poll
 * (`ApprovalsWaitingContext`), never a second one. Nothing when none waits.
 */
export function ApprovalsWaiting({ className }: { className?: string }) {
  const waiting = useContext(ApprovalsWaitingContext)
  if (!waiting || waiting.approvals.length === 0) return null
  return (
    <section aria-label="Approvals waiting for you" className={className}>
      <ul className="flex flex-col gap-2">
        {waiting.approvals.map((approval) => (
          <li key={approval.id}>
            <PendingApprovalCard approval={approval} onDecided={waiting.refresh} />
          </li>
        ))}
      </ul>
    </section>
  )
}
