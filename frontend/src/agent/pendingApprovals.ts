import { useEffect, useRef } from 'react'
import { dismissToast, toast, TOAST_MS } from '../lib/toast'
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

/**
 * #4813 — a toast for each approval that starts waiting while the tab is open, so a person at the
 * screen need not go looking. It offers Review (the Activity page, where the card shows the whole
 * summary) and Deny, never Approve: its detail is clipped, and nobody approves what they could not
 * read in full. Denying runs nothing, so it is safe from a clipped line. It stays until the approval expires
 * ({@link APPROVAL_TOAST_MS} when it does not say), and goes once the approval no longer waits (decided
 * elsewhere, or expired). Each approval is toasted once per page.
 */
export function useApprovalToasts(approvals: readonly PendingApproval[], refresh: () => void): void {
  const seen = useRef(new Set<string>())
  const shown = useRef(new Map<string, number>())
  useEffect(() => {
    const waiting = new Set(approvals.map((a) => a.id))
    for (const [id, toastId] of shown.current) {
      if (waiting.has(id)) continue
      dismissToast(toastId)
      shown.current.delete(id)
    }
    for (const approval of approvals) {
      if (seen.current.has(approval.id)) continue
      seen.current.add(approval.id)
      const left = approval.expiresAt ? new Date(approval.expiresAt).getTime() - Date.now() : NaN
      const deny = () => {
        shown.current.delete(approval.id)
        decidePendingApproval(approval.id, false).then(refresh, (err: unknown) => {
          refresh()
          const why = err instanceof RespondError ? (err.reason ?? err.message) : String(err)
          toast({ title: `Your decision was not taken: ${why}` })
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
  }, [approvals, refresh])
}
