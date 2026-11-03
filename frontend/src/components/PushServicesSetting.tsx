import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { SaveFooter, SettingBody, SettingCard } from './settings/form'

/** One host per line, blank lines dropped. */
const hostsOf = (text: string) =>
  text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')

/**
 * #815 — the push services the agent may send Web Push to (agent `routes/push.ts`
 * `/api/v1/ai/settings/push-services`). A browser's subscription names the endpoint the agent
 * POSTs to, so only these hosts are taken, and only at public addresses. The default is the
 * browsers' own services. Save is user-only; hidden without the agent service.
 */
export function PushServicesSetting() {
  const services = useAsync(() => api.getPushServices(), [])
  const [text, setText] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (services.error || !services.data) return null
  const current = services.data
  const value = text ?? current.hosts.join('\n')
  const changed = hostsOf(value).join('\n') !== current.hosts.join('\n')

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      services.setData(await api.putPushServices(hostsOf(value)))
      setText(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the push services')
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingCard title="Web Push">
      <form className="contents" onSubmit={(event) => void save(event)}>
        <SettingBody className="space-y-2">
          <label htmlFor="push-services" className="text-sm font-medium">
            Allowed push services
          </label>
          <textarea
            id="push-services"
            rows={6}
            className="w-full rounded-md border bg-transparent px-3 py-2 font-mono text-xs"
            value={value}
            onChange={(event) => setText(event.target.value)}
            aria-describedby="push-services-help"
          />
          <p id="push-services-help" className="text-xs text-muted-foreground">
            The hosts the assistant may send browser notifications to when every ScadBuddy tab is closed, one per line.{' '}
            <code>*.example.com</code> allows its subdomains. A browser on a push service not listed here gets no
            notifications with its tabs closed. Only public addresses are ever reached. The default list is the
            browsers' own services: {current.default.join(', ')}.
          </p>
        </SettingBody>
        <SaveFooter saving={saving} disabled={!changed} saved={saved && !changed ? 'Saved.' : null} error={error} />
      </form>
    </SettingCard>
  )
}
