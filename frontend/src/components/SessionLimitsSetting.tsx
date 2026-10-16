import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { LabeledInput, SaveFooter, SettingBody, SettingCard } from './settings/form'

/** agent `sessions/manager.ts` MAX_SESSION_BUDGET_USD and MAX_SESSION_MAX_TURNS. */
const MAX_BUDGET_USD = 100
const MAX_TURNS = 200

/**
 * #790 — what a new assistant chat may spend in all and how many turns one reply may
 * take, stored by the agent service (`/api/v1/ai/settings/session-limits`). A change
 * applies to chats started after it; a chat that ran out is raised from the panel.
 *
 * Hidden when the agent service or its database is not there. Save is user-only: the budget spends money, and an agent must not
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
    <SettingCard title="Assistant chat limits">
      <form className="contents" onSubmit={(event) => void save(event)}>
        <SettingBody className="space-y-3">
          <div className="flex flex-wrap gap-4">
            <LabeledInput
              label="Session budget (USD)"
              type="number"
              inputMode="decimal"
              min={0.01}
              max={MAX_BUDGET_USD}
              step={0.01}
              required
              value={budgetText}
              onChange={(event) => setBudget(event.target.value)}
              className="w-36"
              aria-describedby="session-limits-help"
            />
            <LabeledInput
              label="Max turns per reply"
              type="number"
              inputMode="numeric"
              min={1}
              max={MAX_TURNS}
              step={1}
              required
              value={turnsText}
              onChange={(event) => setTurns(event.target.value)}
              className="w-36"
              aria-describedby="session-limits-help"
            />
          </div>
          <p id="session-limits-help" className="text-xs text-muted-foreground">
            The budget covers a whole chat, up to ${MAX_BUDGET_USD}; when a chat uses it, you can continue in a new
            chat or raise that chat&rsquo;s budget. Max turns caps the steps the assistant takes for one message.
            Changes apply to chats started from now on.
          </p>
        </SettingBody>
        <SaveFooter
          saving={saving}
          disabled={!changed}
          saved={saved && !changed ? 'Saved. New chats use these limits.' : null}
          error={error}
        />
      </form>
    </SettingCard>
  )
}
