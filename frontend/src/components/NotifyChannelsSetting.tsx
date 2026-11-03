import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import type { NotifyChannel } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { Button } from '@/components/ui/button'
import { LabeledInput, SaveFooter, SettingBody, SettingCard } from './settings/form'

/** Human-only: the browser bridge (#254) must never let an agent press these. */
const USER_ONLY = { 'data-agent-user-only': '' }

const KIND_LABEL: Record<NotifyChannel['kind'], string> = { ntfy: 'ntfy', home_assistant: 'Home Assistant' }

const message = (caught: unknown, fallback: string) => (caught instanceof ApiError ? caught.detail : fallback)

/**
 * #815 — where the assistant's attention requests go when nobody is at a browser: an ntfy
 * topic, or a Home Assistant notify service (agent `routes/notifyChannels.ts`). Each is told
 * the request's reason and a link to its session, never what the assistant wrote. Tokens are
 * kept sealed by the agent and never shown again, only their last four characters. Every
 * button is user-only; hidden without the agent service.
 */
export function NotifyChannelsSetting() {
  const list = useAsync(() => api.listNotifyChannels(), [])
  const [kind, setKind] = useState<NotifyChannel['kind']>('ntfy')
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [target, setTarget] = useState('')
  const [token, setToken] = useState('')
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [results, setResults] = useState<Record<string, string>>({})

  if (list.error || !list.data) return null
  const channels = list.data.channels
  const replace = (next: NotifyChannel[]) => list.setData({ channels: next })

  async function add(event: FormEvent) {
    event.preventDefault()
    setAdding(true)
    setError(null)
    try {
      const made = await api.createNotifyChannel({
        kind,
        name,
        url,
        ...(kind === 'home_assistant' ? { target } : {}),
        ...(token ? { token } : {}),
      })
      replace([...channels, made])
      setName('')
      setUrl('')
      setTarget('')
      setToken('')
    } catch (caught) {
      setError(message(caught, 'Could not add the channel'))
    } finally {
      setAdding(false)
    }
  }

  async function act(channel: NotifyChannel, what: 'test' | 'toggle' | 'remove') {
    try {
      if (what === 'test') {
        const result = await api.testNotifyChannel(channel.id)
        setResults((r) => ({ ...r, [channel.id]: result.ok ? 'Sent.' : `Not sent: ${result.detail ?? 'it failed'}` }))
      } else if (what === 'toggle') {
        const updated = await api.updateNotifyChannel(channel.id, { enabled: !channel.enabled })
        replace(channels.map((c) => (c.id === channel.id ? updated : c)))
      } else {
        await api.deleteNotifyChannel(channel.id)
        replace(channels.filter((c) => c.id !== channel.id))
      }
    } catch (caught) {
      setResults((r) => ({ ...r, [channel.id]: message(caught, 'It failed') }))
    }
  }

  const ha = kind === 'home_assistant'
  return (
    <SettingCard title="Notification channels">
      <SettingBody className="space-y-3">
        <p className="text-xs text-muted-foreground">
          When the assistant needs you, these are told too, for when you are away from the browser. They get the reason
          and a link to the session, never what the assistant wrote.
        </p>
        {channels.length > 0 && (
          <ul className="space-y-2">
            {channels.map((channel) => (
              <li key={channel.id} aria-label={channel.name} className="rounded-md border p-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{channel.name}</span>
                  <span className="text-xs text-muted-foreground">
                    {KIND_LABEL[channel.kind]} · {channel.url}
                    {channel.target ? ` · ${channel.target}` : ''}
                    {channel.has_token ? ` · token ${channel.token_last4 ? `…${channel.token_last4}` : 'set'}` : ''}
                    {channel.enabled ? '' : ' · off'}
                  </span>
                </div>
                {channel.last_error && <p className="text-xs text-warn">Last send failed: {channel.last_error}</p>}
                <div className="mt-1 flex flex-wrap gap-2">
                  <Button size="sm" variant="outline" {...USER_ONLY} onClick={() => void act(channel, 'test')}>
                    Send a test
                  </Button>
                  <Button size="sm" variant="ghost" {...USER_ONLY} onClick={() => void act(channel, 'toggle')}>
                    {channel.enabled ? 'Turn off' : 'Turn on'}
                  </Button>
                  <Button size="sm" variant="ghost" {...USER_ONLY} onClick={() => void act(channel, 'remove')}>
                    Remove
                  </Button>
                  {results[channel.id] && (
                    <span role="status" className="self-center text-xs">
                      {results[channel.id]}
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </SettingBody>
      <form className="contents" onSubmit={(event) => void add(event)}>
        <SettingBody className="grid gap-3 sm:grid-cols-2">
          <div className="grid gap-1.5">
            <label htmlFor="notify-channel-kind" className="text-sm">
              Kind
            </label>
            <select
              id="notify-channel-kind"
              className="h-9 rounded-md border bg-transparent px-2 text-sm"
              value={kind}
              onChange={(event) => setKind(event.target.value as NotifyChannel['kind'])}
            >
              <option value="ntfy">ntfy</option>
              <option value="home_assistant">Home Assistant</option>
            </select>
          </div>
          <LabeledInput label="Name" value={name} onChange={(event) => setName(event.target.value)} required maxLength={100} />
          <LabeledInput
            label={ha ? 'Home Assistant URL' : 'ntfy topic URL'}
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder={ha ? 'https://homeassistant.local:8123' : 'https://ntfy.sh/<topic>'}
            required
          />
          {ha && (
            <LabeledInput
              label="Notify service"
              value={target}
              onChange={(event) => setTarget(event.target.value)}
              placeholder="mobile_app_<device>"
            />
          )}
          <LabeledInput
            label={ha ? 'Long-lived access token' : 'Access token (optional)'}
            type="password"
            autoComplete="off"
            value={token}
            onChange={(event) => setToken(event.target.value)}
          />
        </SettingBody>
        <SaveFooter saving={adding} disabled={name.trim() === '' || url.trim() === ''} error={error} label="Add channel" />
      </form>
    </SettingCard>
  )
}
