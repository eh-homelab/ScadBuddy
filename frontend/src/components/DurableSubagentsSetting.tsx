import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

/**
 * Where a durable segment's subagents reach ScadBuddy's tools, stored by the agent service
 * (`/api/v1/ai/settings/durable-subagents`). Unset, agent-durable must share the agent's pod:
 * the agent answers that route on loopback only. Running agent-durable as its own Deployment
 * needs both fields: the URL it gives its subagents, and the agent-durable pods' range.
 *
 * Hidden when the agent service or its database is not there, like the session mode.
 * Save is user-only: it widens who may call the agent.
 */
export function DurableSubagentsSetting() {
  const setting = useAsync(() => api.getDurableSubagents(), [])
  const [url, setUrl] = useState<string | null>(null)
  const [peers, setPeers] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (setting.error || !setting.data) return null
  const current = setting.data
  const urlText = url ?? current.url ?? ''
  const peersText = peers ?? current.peers.join(', ')
  const parsedPeers = peersText
    .split(/[\s,]+/)
    .map((p) => p.trim())
    .filter(Boolean)
  const nextUrl = urlText.trim() || null
  const changed = nextUrl !== current.url || parsedPeers.join(',') !== current.peers.join(',')

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      setting.setData(await api.putDurableSubagents({ url: nextUrl, peers: parsedPeers }))
      setUrl(null)
      setPeers(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the durable subagents setting')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Durable subagents</h2>
      <form className="flex flex-col gap-3 p-4" onSubmit={(event) => void save(event)}>
        <label className="flex flex-col gap-1 text-[13px]">
          Tools URL
          <input
            type="url"
            value={urlText}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="http://127.0.0.1:8081/internal/durable/subagent-mcp"
            className="sb-field"
            aria-describedby="durable-subagents-help"
          />
        </label>
        <label className="flex flex-col gap-1 text-[13px]">
          Allowed worker addresses
          <input
            type="text"
            value={peersText}
            onChange={(event) => setPeers(event.target.value)}
            placeholder="loopback only"
            className="sb-field"
            aria-describedby="durable-subagents-help"
          />
        </label>
        <p id="durable-subagents-help" className="text-[12px] text-muted">
          A durable session&apos;s subagents call ScadBuddy&apos;s tools on the assistant service. Leave both empty when
          the durable worker runs beside the assistant (the same pod). When it runs on its own, set the URL it reaches
          the assistant at, and the addresses or CIDR ranges it calls from, separated by commas (no range broader than
          a /8, or an IPv6 /32). Each call still needs a token for one running turn, and a network policy that lets
          only the durable worker reach the assistant&apos;s port is required: a pod range also holds the ingress&apos;s
          pods. Applies from the next turn.
        </p>
        <div className="flex items-center gap-3">
          <Button type="submit" variant="primary" size="sm" disabled={!changed || saving} aria-busy={saving} {...USER_ONLY}>
            {saving && <Spinner />}
            Save
          </Button>
          {saved && !changed && (
            <span role="status" className="text-[12px] text-ok">
              Saved.
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
