import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

/**
 * The longest query automatic Hindsight memory recalls with, in characters, stored by the
 * agent service (`/api/v1/ai/settings/memory`): the Claude Code Hindsight plugin's
 * `recallMaxQueryChars`, 800 by default, 0 for no cut. Both session modes read it at
 * each turn, so a change applies to the next message.
 *
 * Hidden when the agent service or its database is not there, like the other assistant
 * settings. Save is user-only.
 */
export function MemorySettingsSetting() {
  const settings = useAsync(() => api.getMemorySettings(), [])
  const [chars, setChars] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (settings.error || !settings.data) return null
  const current = settings.data
  const charsText = chars ?? String(current.recall_max_query_chars)
  const changed = Number(charsText) !== current.recall_max_query_chars

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      settings.setData(await api.putMemorySettings(Number(charsText)))
      setChars(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the memory setting')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Hindsight memory</h2>
      <form className="flex flex-col gap-3 p-4" onSubmit={(event) => void save(event)}>
        <label className="flex flex-col gap-1 text-[13px]">
          Recall query limit (characters)
          <input
            type="number"
            inputMode="numeric"
            min={0}
            max={current.max}
            step={1}
            required
            value={charsText}
            onChange={(event) => setChars(event.target.value)}
            className="sb-field sb-num w-32"
            aria-describedby="memory-settings-help"
          />
        </label>
        <p id="memory-settings-help" className="text-[12px] text-muted">
          With a <code>hindsight</code> plugin enabled, each message is recalled against the bank using its first
          this many characters, as the Claude Code Hindsight plugin does (<code>recallMaxQueryChars</code>).{' '}
          {current.default} is the default; 0 sends the whole message. The bank has its own limit in tokens (
          <code>HINDSIGHT_API_RECALL_MAX_QUERY_TOKENS</code>, 500 by default): a query it still finds too long is
          retried once, shorter. Applies from the next message.
        </p>
        <div className="flex items-center gap-3">
          <Button type="submit" variant="primary" size="sm" disabled={!changed || saving} aria-busy={saving} {...USER_ONLY}>
            {saving && <Spinner />}
            Save
          </Button>
          {saved && !changed && (
            <span role="status" className="text-[12px] text-ok">
              Saved. The next message uses it.
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
