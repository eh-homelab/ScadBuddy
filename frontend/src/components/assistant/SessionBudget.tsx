import { useId, useState, type FormEvent } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { BUDGET_WARNING, budgetUsed, type SessionState } from '../../agent/chat/state'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'

/** agent `sessions/manager.ts` MAX_SESSION_BUDGET_USD: no chat's budget goes past it. */
const MAX_BUDGET_USD = 100

/** Money as the panel shows it: dollars, rounded to cents. */
export function usd(amount: number): string {
  return `$${amount.toFixed(2)}`
}

/**
 * #790 — "$0.74 of $1.00" in the session header, with a bar, and a warning from
 * BUDGET_WARNING (80%) until the budget is spent. Nothing for a session whose log
 * never said its budget (one started before #790 that has not run a turn since).
 */
export function BudgetMeter({ session }: { session: SessionState }) {
  const used = budgetUsed(session)
  if (!session.budget || used === undefined) return null
  const close = used >= BUDGET_WARNING && !session.budgetSpent
  const percent = Math.min(Math.round(used * 100), 100)
  return (
    <div className="flex w-full items-center gap-2" data-testid="session-budget">
      <div
        role="meter"
        aria-label="Budget used"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-valuetext={`${usd(session.budget.costUsd)} of ${usd(session.budget.budgetUsd)}`}
        className="h-1 w-16 overflow-hidden rounded-full bg-surface-3"
      >
        <div className={`h-full ${used >= BUDGET_WARNING ? 'bg-warn' : 'bg-accent'}`} style={{ width: `${percent}%` }} />
      </div>
      <span className="sb-num text-faint">
        {usd(session.budget.costUsd)} of {usd(session.budget.budgetUsd)}
      </span>
      {close && <span className="text-warn">This chat is close to its budget.</span>}
    </div>
  )
}

interface SpentProps {
  session: SessionState
  /** "Continue in a new chat": fork, then show the fork. Rejects with the reason to show. */
  onContinue: () => Promise<void>
  /** Adds `addUsd` to this chat's budget. Rejects with the reason to show. */
  onRaise: (addUsd: number) => Promise<void>
  onStartNew: () => void
}

/**
 * #790 — the one message a chat that used its budget shows, instead of the agent's
 * errors, and what to do about it. Raising is user-only (it spends money, and an agent
 * driving the tab must not raise its own budget), so the button, the amount and its
 * confirmation carry USER_ONLY.
 */
export function BudgetSpent({ session, onContinue, onRaise, onStartNew }: SpentProps) {
  const [busy, setBusy] = useState<'continue' | 'raise' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [raising, setRaising] = useState(false)
  const budget = session.budget?.budgetUsd
  const room = budget === undefined ? MAX_BUDGET_USD : Math.max(MAX_BUDGET_USD - budget, 0)
  const [amount, setAmount] = useState(() => Math.min(budget ?? 1, room).toFixed(2))
  const amountId = useId()

  async function run(which: 'continue' | 'raise', action: () => Promise<void>) {
    setBusy(which)
    setError(null)
    try {
      await action()
      if (which === 'raise') setRaising(false)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(null)
    }
  }

  const raise = (event: FormEvent) => {
    event.preventDefault()
    void run('raise', () => onRaise(Number(amount)))
  }

  return (
    <div role="region" aria-label="Chat budget" className="rounded-[6px] border border-warn/40 bg-warn/5 p-2.5 text-[12.5px]">
      <p className="font-medium">
        {budget === undefined ? 'This chat used its budget.' : `This chat used its ${usd(budget)} budget.`}
      </p>
      <p className="mt-0.5 text-muted">Continue in a new chat with this conversation and a fresh budget, or raise this one.</p>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <Button size="sm" variant="primary" disabled={busy !== null} aria-busy={busy === 'continue'} onClick={() => void run('continue', onContinue)}>
          {busy === 'continue' && <Spinner />}
          Continue in a new chat
        </Button>
        {room > 0 && (
          <Button size="sm" disabled={busy !== null} aria-expanded={raising} onClick={() => setRaising((r) => !r)} {...USER_ONLY}>
            Raise this chat&rsquo;s budget
          </Button>
        )}
        <Button size="sm" variant="ghost" disabled={busy !== null} onClick={onStartNew}>
          Start a new chat
        </Button>
      </div>
      {raising && (
        <form onSubmit={raise} className="mt-2 flex flex-wrap items-center gap-1.5" {...USER_ONLY}>
          <label htmlFor={amountId}>Add (USD)</label>
          <input
            id={amountId}
            type="number"
            inputMode="decimal"
            min={0.01}
            max={room}
            step={0.01}
            required
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
            className="sb-field sb-num w-24"
          />
          <Button type="submit" size="sm" variant="primary" disabled={busy !== null} aria-busy={busy === 'raise'}>
            {busy === 'raise' && <Spinner />}
            Raise
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" className="mt-1.5 text-warn">
          {error}
        </p>
      )}
    </div>
  )
}
