import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { SessionMode } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

/**
 * #1056 — the mode a new assistant chat gets when its start chooses none, stored by the
 * agent service (`/api/v1/ai/settings/session-mode`). Durable chats run on Temporal and
 * survive a restart; a chat's mode is fixed when it starts, so a change applies to chats
 * started after it. Hidden when the agent service or its database is not there, like the
 * session limits. Save is user-only: the mode decides where the assistant's work runs.
 */
export function SessionModeSetting() {
  const setting = useAsync(() => api.getSessionMode(), [])
  const [picked, setPicked] = useState<SessionMode | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (setting.error || !setting.data) return null
  const current = setting.data.mode
  const mode = picked ?? current

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      setting.setData(await api.putSessionMode(mode))
      setPicked(null)
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
          Default mode for new chats
          <select
            value={mode}
            onChange={(event) => setPicked(event.target.value as SessionMode)}
            className="sb-field w-40"
            aria-describedby="session-mode-help"
          >
            <option value="classic">Classic</option>
            <option value="durable">Durable</option>
          </select>
        </label>
        <p id="session-mode-help" className="text-[12px] text-muted">
          Durable chats survive restarts and their approvals wait as long as needed, but plugins are not available
          in them. A chat&rsquo;s mode is fixed when it starts. A mode chosen under Advanced in a new chat is
          remembered on that browser and overrides this default. Changes apply to chats started from now on.
        </p>
        <div className="flex items-center gap-3">
          <Button type="submit" variant="primary" size="sm" disabled={mode === current || saving} aria-busy={saving} {...USER_ONLY}>
            {saving && <Spinner />}
            Save
          </Button>
          {saved && mode === current && (
            <span role="status" className="text-[12px] text-ok">
              Saved. New chats start in {current} mode unless one is chosen.
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
