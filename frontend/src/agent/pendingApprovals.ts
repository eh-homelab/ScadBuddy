import { useEffect, useRef } from 'react'
import { dismissToast, getToasts, toast, TOAST_MS } from '../lib/toast'
import { traceAction } from '../lib/traceAction'
import type { PendingApproval } from './attention'
import { decisionBody, respond, RespondError, type RespondOutcome } from './respond'

/** Decides a pending approval through the one respond route (`respond.ts`), as the chat feed's cards do. */
export const decidePendingApproval = (a: PendingApproval, approve: boolean): Promise<RespondOutcome> =>
  traceAction('assistant.respond', { 'scadbuddy.respond_kind': 'approval' }, () => respond(a.id, decisionBody(approve, a.inputHash)))

/** Who prepared it, and with which tool: "print_output, asked by Claude Code". */
export function preparedBy(a: PendingApproval): string {
  return a.by ? `${a.tool}, asked by ${a.by}` : a.tool
}

/** Where a toast's Review goes: the page that lists every approval waiting with its whole card. */
export const APPROVALS_PAGE = '/activity'

/** The longest an approval's toast stays, and how long when the agent did not say when it expires (`approval_expiry_seconds`'s default). */
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
 * until the approval expires (at most {@link APPROVAL_TOAST_MS}), and goes once the
 * approval no longer waits (decided elsewhere, or expired). Each approval is toasted once per page,
 * and at most {@link MAX_APPROVAL_TOASTS} at once: past that, one toast says more wait, and the
 * rest get their own as places free up.
 */
export function useApprovalToasts(approvals: readonly PendingApproval[], refresh: () => void): void {
  const seen = useRef(new Set<string>())
  const shown = useRef(new Map<string, number>())
  const more = useRef<number | undefined>(undefined)
  // Runs on each read: `useAttention` sets a new list every poll, so a place freed by a dismissed
  // toast (its X or its timer) is filled by the next read, at most ATTENTION_POLL_MS later.
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
      // Past the cap it waits, unseen, for a place: a decided or dismissed toast frees one.
      if (shown.current.size >= MAX_APPROVAL_TOASTS) {
        overflow += 1
        continue
      }
      seen.current.add(approval.id)
      const left = approval.expiresAt ? new Date(approval.expiresAt).getTime() - Date.now() : NaN
      const deny = () => {
        shown.current.delete(approval.id)
        decidePendingApproval(approval, false).then(refresh, (err: unknown) => {
          // Not reached (not settled): it still waits, so a later read toasts it again.
          if (!(err instanceof RespondError && err.settled)) seen.current.delete(approval.id)
          refresh()
          toast({ title: notTakenText(err) })
        })
      }
      shown.current.set(
        approval.id,
        toast({
          title: 'Needs your approval',
          detail: approval.summary ? `${preparedBy(approval)}: ${approval.summary}` : preparedBy(approval),
          // Never past the default window: a far `expires_at` would overflow setTimeout, and Activity lists it anyway.
          ms: Number.isNaN(left) ? APPROVAL_TOAST_MS : Math.min(Math.max(left, TOAST_MS), APPROVAL_TOAST_MS),
          actions: [
            { label: 'Review', href: APPROVALS_PAGE, onClick: () => shown.current.delete(approval.id) },
            { label: 'Deny', userOnly: true, destructive: true, onClick: deny },
          ],
        }),
      )
    }
    // One toast says more wait while any does without its own (shown again on a later read if it timed out).
    if (overflow === 0) {
      if (more.current !== undefined) dismissToast(more.current)
      more.current = undefined
    } else if (!isShown(more.current)) {
      more.current = toast({
        title: 'More approvals wait for you',
        ms: APPROVAL_TOAST_MS,
        actions: [{ label: 'Review', href: APPROVALS_PAGE }],
      })
    }
  }, [approvals, refresh])
}
