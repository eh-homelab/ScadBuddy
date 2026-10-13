import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { RenderLimits } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Spinner } from './ui/Spinner'

/** agent `src/tools/renderLimits.ts` MAX_RENDER_*. */
const MAX_CONCURRENT = 64
const MAX_GLOBAL = 256
const MAX_PER_WINDOW = 10_000

type Row = { principal: string; concurrent: string }
type Draft = { per_principal: string; global: string; per_window: string; overrides: Row[] }

function draftOf(limits: RenderLimits): Draft {
  return {
    per_principal: String(limits.per_principal),
    global: String(limits.global),
    per_window: String(limits.per_window),
    overrides: limits.overrides.map((o) => ({ principal: o.principal, concurrent: String(o.concurrent) })),
  }
}

function limitsOf(draft: Draft): RenderLimits {
  return {
    per_principal: Number(draft.per_principal),
    global: Number(draft.global),
    per_window: Number(draft.per_window),
    overrides: draft.overrides.map((o) => ({ principal: o.principal.trim(), concurrent: Number(o.concurrent) })),
  }
}

/**
 * How many of the assistant's renders run at once: each caller's (`browser` is you and
 * every chat you start, sub-agents included; an MCP token is `token:<id>`), with
 * overrides by caller, and all of them together. Stored by the agent service
 * (`/api/v1/ai/settings/render-limits`), which reads it live: a change applies at once,
 * to renders already waiting too. A render past a limit waits its turn rather than
 * failing, so there is no cap on how many may queue.
 *
 * Hidden when the agent service or its database is not there, like the chat limits.
 * Save is user-only: an agent must not raise its own limits (AI design spec §8.1).
 */
export function RenderLimitsSetting() {
  const limits = useAsync(() => api.getRenderLimits(), [])
  // Token names for the caller suggestions, asked for only once a caller field is
  // used (the MCP token routes are not asked while the assistant is unavailable).
  const [wantTokens, setWantTokens] = useState(false)
  const tokens = useAsync(() => (wantTokens ? api.listMcpTokens().catch(() => null) : Promise.resolve(null)), [wantTokens])
  const [draft, setDraft] = useState<Draft | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  if (limits.error || !limits.data) return null
  const current = draft ?? draftOf(limits.data)
  const changed = JSON.stringify(limitsOf(current)) !== JSON.stringify(limits.data)
  const edit = (next: Partial<Draft>) => {
    setSaved(false)
    setDraft({ ...current, ...next })
  }
  const editRow = (index: number, next: Partial<Row>) =>
    edit({ overrides: current.overrides.map((row, i) => (i === index ? { ...row, ...next } : row)) })

  async function save(event: FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      limits.setData(await api.putRenderLimits(limitsOf(current)))
      setDraft(null)
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : 'Could not save the render limits')
    } finally {
      setSaving(false)
    }
  }

  const callers = [
    { id: 'browser', label: 'You, and every chat you start' },
    ...(tokens.data?.tokens ?? []).filter((t) => t.status === 'active').map((t) => ({ id: `token:${t.id}`, label: `MCP token: ${t.name}` })),
  ]

  return (
    <section className="mt-4 rounded-[6px] border border-line bg-surface">
      <h2 className="border-b border-line px-4 py-2.5 text-[13px] font-medium">Assistant render limits</h2>
      <form className="flex flex-col gap-3 p-4" onSubmit={(event) => void save(event)}>
        <div className="flex flex-wrap gap-4">
          <label className="flex flex-col gap-1 text-[13px]">
            Renders at once, per caller
            <input
              type="number"
              inputMode="numeric"
              min={1}
              max={MAX_CONCURRENT}
              step={1}
              required
              value={current.per_principal}
              onChange={(event) => edit({ per_principal: event.target.value })}
              className="sb-field sb-num w-32"
              aria-describedby="render-limits-help"
            />
          </label>
          <label className="flex flex-col gap-1 text-[13px]">
            Renders at once, in all
            <input
              type="number"
              inputMode="numeric"
              min={0}
              max={MAX_GLOBAL}
              step={1}
              required
              value={current.global}
              onChange={(event) => edit({ global: event.target.value })}
              className="sb-field sb-num w-32"
              aria-describedby="render-limits-help"
            />
          </label>
          <label className="flex flex-col gap-1 text-[13px]">
            Renders per 10 min, per caller
            <input
              type="number"
              inputMode="numeric"
              min={0}
              max={MAX_PER_WINDOW}
              step={1}
              required
              value={current.per_window}
              onChange={(event) => edit({ per_window: event.target.value })}
              className="sb-field sb-num w-32"
              aria-describedby="render-limits-help"
            />
          </label>
        </div>

        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-[13px]">Per-caller overrides</legend>
          <datalist id="render-limit-callers">
            {callers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </datalist>
          {current.overrides.map((row, index) => (
            <div key={index} className="flex flex-wrap items-end gap-2">
              <label className="flex flex-col gap-1 text-[12px]">
                Caller
                <input
                  type="text"
                  list="render-limit-callers"
                  required
                  value={row.principal}
                  onChange={(event) => editRow(index, { principal: event.target.value })}
                  onFocus={() => setWantTokens(true)}
                  className="sb-field w-72"
                  placeholder="browser, token:<id>, oidc:<issuer>#<sub>"
                />
              </label>
              <label className="flex flex-col gap-1 text-[12px]">
                At once
                <input
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={MAX_CONCURRENT}
                  step={1}
                  required
                  value={row.concurrent}
                  onChange={(event) => editRow(index, { concurrent: event.target.value })}
                  className="sb-field sb-num w-24"
                />
              </label>
              <Button
                type="button"
                size="sm"
                onClick={() => edit({ overrides: current.overrides.filter((_, i) => i !== index) })}
                aria-label={`Remove the override for ${row.principal || 'this caller'}`}
                {...USER_ONLY}
              >
                Remove
              </Button>
            </div>
          ))}
          <div>
            <Button
              type="button"
              size="sm"
              onClick={() =>
                edit({ overrides: [...current.overrides, { principal: '', concurrent: current.per_principal }] })
              }
              {...USER_ONLY}
            >
              Add override
            </Button>
          </div>
        </fieldset>

        <p id="render-limits-help" className="text-[12px] text-muted">
          A render past a limit waits its turn instead of failing, so the assistant and its sub-agents can queue as
          many as they need. &ldquo;In all&rdquo; counts every caller together; 0 means no limit beyond the render
          workers&rsquo; own. The per-10-minute cap stops an assistant stuck in a loop; 0 turns it off. Changes apply
          at once, to renders already waiting too.
        </p>
        <div className="flex items-center gap-3">
          <Button type="submit" variant="primary" size="sm" disabled={!changed || saving} aria-busy={saving} {...USER_ONLY}>
            {saving && <Spinner />}
            Save
          </Button>
          {saved && !changed && (
            <span role="status" className="text-[12px] text-ok">
              Saved. The assistant uses these limits now.
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
