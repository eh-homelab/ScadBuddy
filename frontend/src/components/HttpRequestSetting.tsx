import { useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { Spinner } from './ui/Spinner'

/**
 * #827 — lets the AI assistant make HTTP requests (its `http_request` tool, "curl") to
 * the internet and the local network. Stored by the agent service
 * (`/api/v1/ai/settings/http-request`), on by default.
 *
 * Hidden when the agent service or its database is not there, like the headless
 * browser switch. Only the user may flip it: it is a settings write, and an agent must
 * not grant itself network reach (AI design spec §8.1).
 */
export function HttpRequestSetting() {
  const setting = useAsync(() => api.getHttpRequestSetting(), [])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (setting.error || !setting.data) return null
  const enabled = setting.data.enabled

  async function toggle(next: boolean) {
    setSaving(true)
    setError(null)
    try {
      setting.setData(await api.putHttpRequestSetting(next))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the setting')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">AI HTTP requests</h2>
      <div className="p-4">
        <label className="flex items-start gap-2 text-[13px]" {...USER_ONLY}>
          <input
            type="checkbox"
            checked={enabled}
            disabled={saving}
            aria-busy={saving}
            onChange={(event) => void toggle(event.target.checked)}
            className="mt-0.5"
            aria-describedby="http-request-help"
          />
          Let the assistant make HTTP requests
          {saving && <Spinner />}
        </label>
        <p id="http-request-help" className="mt-1.5 text-[12px] text-muted">
          Like curl: any http or https address, on the internet or your local network. Reading
          (GET, HEAD) happens at once; sending, changing or deleting anything (POST, PUT, PATCH,
          DELETE) waits for your approval. Every request is listed in AI activity.
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
