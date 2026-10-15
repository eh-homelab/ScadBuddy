import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { SessionRuntime } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

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
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Assistant session runtime</h2>
      <form className="flex flex-col gap-3 p-4" onSubmit={(event) => void save(event)}>
        <div className="flex flex-wrap gap-4">
          {FIELDS.map(({ key, label, min, max }) => (
            <label key={key} className="flex flex-col gap-1 text-[13px]">
              {label}
              <input
                type="number"
                inputMode="numeric"
                min={min}
                max={max}
                step={1}
                required
                value={text(key)}
                onChange={(event) => setDraft((d) => ({ ...d, [key]: event.target.value }))}
                className="sb-field sb-num w-32"
                aria-describedby="session-runtime-help"
              />
            </label>
          ))}
        </div>
        <p id="session-runtime-help" className="text-[12px] text-muted">
          A replica keeps up to this many assistant processes warm between turns, each for the idle time given; 0
          starts every turn cold. A change applies to the warm processes at once; a running turn is never stopped by it.
          The park hold and the longest turn are read by later versions of the assistant.
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
