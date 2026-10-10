import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { SessionModeSetting as Setting } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

/**
 * Plan 5d — the mode a new assistant session gets when whoever starts it names none,
 * stored by the agent service (`/api/v1/ai/settings/session-mode`). Durable until set.
 * A session that got durable from here on an agent service that cannot run one now
 * runs classic and says why; one that asked for durable (the composer's picker, an
 * MCP caller) is refused instead.
 *
 * Hidden when the agent service or its database is not there, like the chat limits.
 * Save is user-only: it changes how every new session runs.
 */
export function SessionModeSetting() {
  const setting = useAsync(() => api.getSessionMode(), [])
  const [mode, setMode] = useState<Setting['mode'] | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (setting.error || !setting.data) return null
  const current = setting.data
  const chosen = mode ?? current.mode
  const changed = chosen !== current.mode

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      setting.setData(await api.putSessionMode(chosen))
      setMode(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the session mode')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Assistant session mode</h2>
      <form className="flex flex-col gap-3 p-4" onSubmit={(event) => void save(event)}>
        <label className="flex flex-col gap-1 text-[13px]">
          Default session mode
          <select
            value={chosen}
            onChange={(event) => setMode(event.target.value as Setting['mode'])}
            className="sb-field w-40"
            aria-describedby="session-mode-help"
          >
            <option value="durable">Durable</option>
            <option value="classic">Classic</option>
          </select>
        </label>
        <p id="session-mode-help" className="text-[12px] text-muted">
          Durable sessions run as Temporal workflows: a turn survives a restart of the assistant, and an approval or
          question waits for you as long as it needs. Classic sessions run inside the assistant service, and can be
          forked. A new chat uses this default unless you pick a mode under Session mode in the composer. Applies to
          sessions started from now on.
        </p>
        {!current.durable_available && (
          <p data-testid="session-mode-unavailable" className="text-[12px] text-warn">
            Durable sessions cannot start right now, so new chats that use this default run as Classic
            {current.durable_unavailable_reason ? `: ${current.durable_unavailable_reason}.` : '.'}
          </p>
        )}
        <div className="flex items-center gap-3">
          <Button type="submit" variant="primary" size="sm" disabled={!changed || saving} aria-busy={saving} {...USER_ONLY}>
            {saving && <Spinner />}
            Save
          </Button>
          {saved && !changed && (
            <span role="status" className="text-[12px] text-ok">
              Saved. New sessions use it.
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
