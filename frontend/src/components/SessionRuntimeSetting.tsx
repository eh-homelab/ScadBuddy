import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import type { SessionRuntime } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { LabeledInput, SaveFooter, SettingBody, SettingCard } from './settings/form'

/** agent `sessions/runtimeSettings.ts` RUNTIME_BOUNDS (unified runtime spec §13). */
const FIELDS: { key: keyof SessionRuntime; label: string; min: number; max: number }[] = [
  { key: 'park_hold_seconds', label: 'Park hold (seconds)', min: 30, max: 3600 },
  { key: 'warm_max_per_replica', label: 'Warm processes per replica', min: 0, max: 32 },
  { key: 'warm_idle_seconds', label: 'Warm process idle time (seconds)', min: 60, max: 86400 },
  { key: 'turn_max_seconds', label: 'Longest turn (seconds)', min: 300, max: 86400 },
]

/**
 * The session runtime settings, stored by the agent service
 * (`/api/v1/ai/settings/session-runtime`): how long a parked call is held, how many
 * warm assistant processes a replica keeps (0 starts every turn cold) and for how
 * long, and the longest a turn may run. The warm cache applies a change at once.
 *
 * Hidden when the agent service or its database is not there. Save is user-only,
 * like the limits beside it (AI design spec §8.1).
 */
export function SessionRuntimeSetting() {
  const runtime = useAsync(() => api.getSessionRuntime(), [])
  const [draft, setDraft] = useState<Partial<Record<keyof SessionRuntime, string>>>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (runtime.error || !runtime.data) return null
  const current = runtime.data
  const text = (key: keyof SessionRuntime) => draft[key] ?? String(current[key])
  const changes: Partial<SessionRuntime> = {}
  for (const { key } of FIELDS) if (Number(text(key)) !== current[key]) changes[key] = Number(text(key))
  const changed = Object.keys(changes).length > 0

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      runtime.setData(await api.putSessionRuntime(changes))
      setDraft({})
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the runtime settings')
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingCard title="Assistant session runtime">
      <form className="contents" onSubmit={(event) => void save(event)}>
        <SettingBody className="space-y-3">
          <div className="flex flex-wrap gap-4">
            {FIELDS.map(({ key, label, min, max }) => (
              <LabeledInput
                key={key}
                label={label}
                type="number"
                inputMode="numeric"
                min={min}
                max={max}
                step={1}
                required
                value={text(key)}
                onChange={(event) => setDraft((d) => ({ ...d, [key]: event.target.value }))}
                className="w-48"
                aria-describedby="session-runtime-help"
              />
            ))}
          </div>
          <p id="session-runtime-help" className="text-xs text-muted-foreground">
            A replica keeps up to this many assistant processes warm between turns, each for the idle time given; 0
            starts every turn cold. A change applies to the warm processes at once; a running turn is never stopped by it.
            The park hold and the longest turn are read by later versions of the assistant.
          </p>
        </SettingBody>
        <SaveFooter saving={saving} disabled={!changed} saved={saved && !changed ? 'Saved.' : null} error={error} />
      </form>
    </SettingCard>
  )
}
