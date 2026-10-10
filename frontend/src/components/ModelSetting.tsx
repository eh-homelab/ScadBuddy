import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

/** Claude Code's model aliases: each names that family's latest model. */
const ALIASES = ['opus', 'sonnet', 'haiku'] as const

/**
 * #1917 — the Claude model every assistant turn and Settings' connection test use,
 * stored by the agent service (`/api/v1/ai/settings/model`). Empty is Claude Code's
 * own default. A change applies from the next turn, in every chat.
 *
 * Hidden when the agent service or its database is not there, like the chat limits.
 * Save is user-only: the model sets what a turn costs, and an agent must not choose
 * its own (AI design spec §8.1).
 */
export function ModelSetting() {
  const setting = useAsync(() => api.getModelSetting(), [])
  const [draft, setDraft] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (setting.error || !setting.data) return null
  const stored = setting.data.model
  const text = draft ?? stored ?? ''
  const changed = text.trim() !== (stored ?? '')

  async function store(model: string | null) {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      setting.setData(await api.putModelSetting(model))
      setDraft(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the model')
    } finally {
      setSaving(false)
    }
  }

  function save(event: FormEvent) {
    event.preventDefault()
    void store(text.trim() || null)
  }

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Assistant model</h2>
      <form className="flex flex-col gap-3 p-4" onSubmit={save}>
        <label className="flex flex-col gap-1 text-[13px]">
          Claude model
          <input
            type="text"
            list="assistant-model-options"
            value={text}
            onChange={(event) => {
              setDraft(event.target.value)
              setSaved(false)
            }}
            placeholder="Claude Code’s default"
            spellCheck={false}
            autoCapitalize="off"
            autoComplete="off"
            className="sb-field sb-num w-72 max-w-full"
            aria-describedby="assistant-model-help"
          />
          <datalist id="assistant-model-options">
            {ALIASES.map((alias) => (
              <option key={alias} value={alias} />
            ))}
          </datalist>
        </label>
        <p id="assistant-model-help" className="text-[12px] text-muted">
          An alias (<span className="sb-num">opus</span>, <span className="sb-num">sonnet</span>,{' '}
          <span className="sb-num">haiku</span>) follows that family&rsquo;s latest model; a full model id
          such as <span className="sb-num">claude-sonnet-5</span> stays on it. Leave it empty for Claude
          Code&rsquo;s default. Every chat uses it from its next reply, and Test connection checks it.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" variant="primary" size="sm" disabled={!changed || saving} aria-busy={saving} {...USER_ONLY}>
            {saving && <Spinner />}
            Save
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={stored === null || saving}
            onClick={() => void store(null)}
            {...USER_ONLY}
          >
            Use the default
          </Button>
          {saved && !changed && (
            <span role="status" className="text-[12px] text-ok">
              Saved. {stored ? `Replies use ${stored}.` : 'Replies use Claude Code’s default.'}
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
