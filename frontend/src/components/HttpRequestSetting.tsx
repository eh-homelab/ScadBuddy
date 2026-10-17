import { useState } from 'react'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { SettingBody, SettingCard, SwitchField } from './settings/form'

/**
 * #827 — lets the AI assistant make HTTP requests (its `http_request` tool, "curl") to
 * the internet and the local network. Stored by the agent service
 * (`/api/v1/ai/settings/http-request`), on by default.
 *
 * Hidden when the agent service or its database is not there. Only the user may flip it: it is a settings write, and an agent must
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
    <SettingCard title="AI HTTP requests">
      <SettingBody className="space-y-1.5">
        <SwitchField
          label="Let the assistant make HTTP requests"
          checked={enabled}
          disabled={saving}
          busy={saving}
          onCheckedChange={(next) => void toggle(next)}
          describedBy="http-request-help"
        />
        <p id="http-request-help" className="text-xs text-muted-foreground">
          Like curl: any http or https address, on the internet or your local network. Reading
          (GET, HEAD) happens at once; sending, changing or deleting anything (POST, PUT, PATCH,
          DELETE) waits for your approval. Every request is listed in AI activity.
        </p>
        {error && (
          <p role="alert" className="text-xs text-warn">
            {error}
          </p>
        )}
      </SettingBody>
    </SettingCard>
  )
}
