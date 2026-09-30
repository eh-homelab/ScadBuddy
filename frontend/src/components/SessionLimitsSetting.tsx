import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

/** agent `sessions/manager.ts` MAX_SESSION_BUDGET_USD and MAX_SESSION_MAX_TURNS. */
const MAX_BUDGET_USD = 100
const MAX_TURNS = 200

/**
 * #790 — what a new assistant chat may spend in all and how many turns one reply may
 * take, stored by the agent service (`/api/v1/ai/settings/session-limits`). A change
 * applies to chats started after it; a chat that ran out is raised from the panel.
 *
 * Hidden when the agent service or its database is not there, like the headless
 * browser switch. Save is user-only: the budget spends money, and an agent must not
 * raise its own (AI design spec §8.1).
 */
export function SessionLimitsSetting() {
  const limits = useAsync(() => api.getSessionLimits(), [])
  const [budget, setBudget] = useState<string | null>(null)
  const [turns, setTurns] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (limits.error || !limits.data) return null
  const current = limits.data
  const budgetText = budget ?? current.budget_usd.toFixed(2)
  const turnsText = turns ?? String(current.max_turns)
  const changed = Number(budgetText) !== current.budget_usd || Number(turnsText) !== current.max_turns

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      limits.setData(await api.putSessionLimits({ budget_usd: Number(budgetText), max_turns: Number(turnsText) }))
      setBudget(null)
      setTurns(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the limits')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Assistant chat limits</h2>
      <form className="flex flex-col gap-3 p-4" onSubmit={(event) => void save(event)}>
        <div className="flex flex-wrap gap-4">
          <label className="flex flex-col gap-1 text-[13px]">
            Session budget (USD)
            <input
              type="number"
              inputMode="decimal"
              min={0.01}
              max={MAX_BUDGET_USD}
              step={0.01}
              required
              value={budgetText}
              onChange={(event) => setBudget(event.target.value)}
              className="sb-field sb-num w-32"
              aria-describedby="session-limits-help"
            />
          </label>
          <label className="flex flex-col gap-1 text-[13px]">
            Max turns per reply
            <input
              type="number"
              inputMode="numeric"
              min={1}
              max={MAX_TURNS}
              step={1}
              required
              value={turnsText}
              onChange={(event) => setTurns(event.target.value)}
              className="sb-field sb-num w-32"
              aria-describedby="session-limits-help"
            />
          </label>
        </div>
        <p id="session-limits-help" className="text-[12px] text-muted">
          The budget covers a whole chat, up to ${MAX_BUDGET_USD}; when a chat uses it, you can continue in a new
          chat or raise that chat&rsquo;s budget. Max turns caps the steps the assistant takes for one message.
          Changes apply to chats started from now on.
        </p>
        <div className="flex items-center gap-3">
          <Button type="submit" variant="primary" size="sm" disabled={!changed || saving} aria-busy={saving} {...USER_ONLY}>
            {saving && <Spinner />}
            Save
          </Button>
          {saved && !changed && (
            <span role="status" className="text-[12px] text-ok">
              Saved. New chats use these limits.
            </span>
          )}
        </div>
        {error && (
          <p role="alert" className="text-[12px] text-warn">
            {error}
          </p>
        )}
      </form>
    </section>
  )
}
