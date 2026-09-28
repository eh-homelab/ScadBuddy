import { useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { Spinner } from './ui/Spinner'

/**
 * #349 — lets the AI agent drive ScadBuddy's own UI in a headless browser in the agent
 * container, for sessions that have no tab of yours (another agent over MCP, evals).
 * Stored by the agent service (`/api/v1/ai/settings/headless-browser`), off by default.
 *
 * Hidden when the agent service or its database is not there: the read fails, and
 * there is nothing to switch on. Applied at once, like the WebMCP switch, and only the
 * user may flip it: it is a settings write, and an agent must not grant itself a
 * browser (AI design spec §5.3, §8.1).
 */
export function HeadlessBrowserSetting() {
  const setting = useAsync(() => api.getHeadlessBrowserSetting(), [])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (setting.error || !setting.data) return null
  const enabled = setting.data.enabled

  async function toggle(next: boolean) {
    setSaving(true)
    setError(null)
    try {
      setting.setData(await api.putHeadlessBrowserSetting(next))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the setting')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">
        AI headless browser
      </h2>
      <div className="p-4">
        <label className="flex items-start gap-2 text-[13px]" {...USER_ONLY}>
          <input
            type="checkbox"
            checked={enabled}
            disabled={saving}
            aria-busy={saving}
            onChange={(event) => void toggle(event.target.checked)}
            className="mt-0.5"
            aria-describedby="headless-browser-help"
          />
          Let AI sessions use ScadBuddy in a headless browser
          {saving && <Spinner />}
        </label>
        <p id="headless-browser-help" className="mt-1.5 text-[12px] text-muted">
          For sessions without a tab of yours, such as another agent working over MCP. The
          browser runs in the agent service and can only open ScadBuddy itself. Anything it
          would send, print, delete or change in Settings still waits for your approval.
        </p>
        {error && (
          <p role="alert" className="mt-1.5 text-[12px] text-warn">
            {error}
          </p>
        )}
      </div>
    </section>
  )
}
