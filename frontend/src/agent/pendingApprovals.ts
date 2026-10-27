import { useEffect, useRef } from 'react'
import { dismissToast, getToasts, toast, TOAST_MS } from '../lib/toast'
import { traceAction } from '../lib/traceAction'
import type { PendingApproval } from './attention'
import { decisionBody, respond, RespondError, type RespondOutcome } from './respond'

/** Decides a pending approval through the one respond route (`respond.ts`), as the chat feed's cards do. */
export const decidePendingApproval = (id: string, approve: boolean): Promise<RespondOutcome> =>
  traceAction('assistant.respond', { 'scadbuddy.respond_kind': 'approval' }, () => respond(id, decisionBody(approve)))

/** Who prepared it, and with which tool: "print_output, asked by Claude Code". */
export function preparedBy(a: PendingApproval): string {
  return a.by ? `${a.tool}, asked by ${a.by}` : a.tool
}

/** Where a toast's Review goes: the page that lists every approval waiting with its whole card. */
export const APPROVALS_PAGE = '/activity'

/** How long an approval's toast stays when the agent did not say when it expires (`approval_expiry_seconds`'s default). */
const APPROVAL_TOAST_MS = 10 * 60_000

/** The most approval toasts shown at once; past it, one toast says more wait, and links to them all. */
export const MAX_APPROVAL_TOASTS = 3

/** Why a decision was not taken, as the toast and the card both say it. */
export function notTakenText(err: unknown): string {
  const why = err instanceof RespondError ? (err.reason ?? (err.settled ? 'the request is no longer waiting' : err.message)) : err instanceof Error ? err.message : String(err)
  return `Your decision was not taken: ${why.replace(/\.$/, '')}.`
}

const isShown = (toastId: number | undefined) => toastId !== undefined && getToasts().some((one) => one.id === toastId)

/**
 * #4813 — a toast for each approval waiting (one already waiting when the page loads included), so
 * a person at the screen need not go looking. It offers Review (the Activity page, where the card
 * shows the whole summary) and Deny, never Approve: its detail is clipped, and nobody approves what
 * they could not read in full. Denying runs nothing, so it is safe from a clipped line. It stays
 * until the approval expires ({@link APPROVAL_TOAST_MS} when it does not say), and goes once the
 * approval no longer waits (decided elsewhere, or expired). Each approval is toasted once per page,
 * and at most {@link MAX_APPROVAL_TOASTS} at once: past that, one toast says more wait.
 */
export function useApprovalToasts(approvals: readonly PendingApproval[], refresh: () => void): void {
  const seen = useRef(new Set<string>())
  const shown = useRef(new Map<string, number>())
  const more = useRef<number | undefined>(undefined)
  useEffect(() => {
    const waiting = new Set(approvals.map((a) => a.id))
    for (const [id, toastId] of shown.current) {
      if (waiting.has(id) && isShown(toastId)) continue
      dismissToast(toastId)
      shown.current.delete(id)
    }
    let overflow = 0
    for (const approval of approvals) {
      if (seen.current.has(approval.id)) continue
      seen.current.add(approval.id)
      if (shown.current.size >= MAX_APPROVAL_TOASTS) {
        overflow += 1
        continue
      }
      const left = approval.expiresAt ? new Date(approval.expiresAt).getTime() - Date.now() : NaN
      const deny = () => {
        shown.current.delete(approval.id)
        decidePendingApproval(approval.id, false).then(refresh, (err: unknown) => {
          refresh()
          toast({ title: notTakenText(err) })
        })
      }
      shown.current.set(
        approval.id,
        toast({
          title: 'Needs your approval',
          detail: approval.summary ? `${preparedBy(approval)}: ${approval.summary}` : preparedBy(approval),
          ms: Number.isNaN(left) ? APPROVAL_TOAST_MS : Math.max(left, TOAST_MS),
          actions: [
            { label: 'Review', href: APPROVALS_PAGE, onClick: () => shown.current.delete(approval.id) },
            { label: 'Deny', userOnly: true, destructive: true, onClick: deny },
          ],
        }),
      )
    }
    if (approvals.length === 0) {
      if (more.current !== undefined) dismissToast(more.current)
      more.current = undefined
    } else if (overflow > 0 && !isShown(more.current)) {
      more.current = toast({
        title: 'More approvals wait for you',
        ms: APPROVAL_TOAST_MS,
        actions: [{ label: 'Review', href: APPROVALS_PAGE }],
      })
    }
  }, [approvals, refresh])
}
