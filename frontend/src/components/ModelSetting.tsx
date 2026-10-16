import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import { useAsync } from '../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { SaveFooter, SettingBody, SettingCard, SuggestInput } from './settings/form'

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
    <SettingCard title="Assistant model">
      <form className="contents" onSubmit={save}>
        <SettingBody className="space-y-3">
          <div className="grid gap-1.5">
            <Label htmlFor="assistant-model" className="font-normal">
              Claude model
            </Label>
            <SuggestInput
              id="assistant-model"
              type="text"
              value={text}
              onChange={(event) => {
                setDraft(event.target.value)
                setSaved(false)
              }}
              suggestions={ALIASES.map((alias) => ({ value: alias, label: `latest ${alias}` }))}
              onPick={(alias) => {
                setDraft(alias)
                setSaved(false)
              }}
              suggestionsLabel="Model aliases"
              placeholder="Claude Code’s default"
              spellCheck={false}
              autoCapitalize="off"
              autoComplete="off"
              className="sb-num w-72 max-w-full"
              aria-describedby="assistant-model-help"
            />
          </div>
          <p id="assistant-model-help" className="text-xs text-muted-foreground">
            An alias (<span className="sb-num">opus</span>, <span className="sb-num">sonnet</span>,{' '}
            <span className="sb-num">haiku</span>) follows that family&rsquo;s latest model; a full model id
            such as <span className="sb-num">claude-sonnet-5</span> stays on it. Leave it empty for Claude
            Code&rsquo;s default. Every chat uses it from its next reply, and Test connection checks it.
          </p>
        </SettingBody>
        <SaveFooter
          saving={saving}
          disabled={!changed}
          saved={
            saved && !changed
              ? `Saved. ${stored ? `Replies use ${stored}.` : 'Replies use Claude Code’s default.'}`
              : null
          }
          error={error}
        >
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
        </SaveFooter>
      </form>
    </SettingCard>
  )
}
