import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import type { SessionModeSetting as Setting } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { Label } from '@/components/ui/label'
import { ChoiceSelect, SaveFooter, SettingBody, SettingCard } from './settings/form'

/**
 * Plan 5d — the mode a new assistant session gets when whoever starts it names none,
 * stored by the agent service (`/api/v1/ai/settings/session-mode`). Durable until set.
 * A session that got durable from here on an agent service that cannot run one now
 * runs classic and says why; one that asked for durable (the composer's picker, an
 * MCP caller) is refused instead.
 *
 * Hidden when the agent service or its database is not there, like the chat limits.
 * Save is user-only: it changes how every new session runs.
 */
export function SessionModeSetting() {
  const setting = useAsync(() => api.getSessionMode(), [])
  const [mode, setMode] = useState<Setting['mode'] | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (setting.error || !setting.data) return null
  const current = setting.data
  const chosen = mode ?? current.mode
  const changed = chosen !== current.mode

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      setting.setData(await api.putSessionMode(chosen))
      setMode(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the session mode')
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingCard title="Assistant session mode">
      <form className="contents" onSubmit={(event) => void save(event)}>
        <SettingBody className="space-y-3">
          <div className="grid gap-1.5">
            <Label htmlFor="session-mode" className="font-normal">
              Default session mode
            </Label>
            <ChoiceSelect
              id="session-mode"
              value={chosen}
              onValueChange={(next) => setMode(next as Setting['mode'])}
              choices={[
                { value: 'durable', label: 'Durable' },
                { value: 'classic', label: 'Classic' },
              ]}
              className="w-40"
              aria-describedby="session-mode-help"
            />
          </div>
          <p id="session-mode-help" className="text-xs text-muted-foreground">
            Both modes have the same tools, plugins, skills, subagents and settings, except that a durable subagent
            has only ScadBuddy&apos;s own tools, and a durable session leaves out the push notifications an approved plugin
            package may name. Durable sessions run as Temporal
            workflows: a turn survives a restart of the assistant, and an approval or question waits for you as long as
            it needs. Classic sessions run inside the assistant service, and can be forked. A new chat uses this default
            unless you pick a mode under Session mode in the composer. Applies to sessions started from now on.
          </p>
          {!current.durable_available && (
            <p data-testid="session-mode-unavailable" className="text-xs text-warn">
              Durable sessions cannot start right now, so new chats that use this default run as Classic
              {current.durable_unavailable_reason ? `: ${current.durable_unavailable_reason}.` : '.'}
            </p>
          )}
        </SettingBody>
        <SaveFooter
          saving={saving}
          disabled={!changed}
          saved={saved && !changed ? 'Saved. New sessions use it.' : null}
          error={error}
        />
      </form>
    </SettingCard>
  )
}
