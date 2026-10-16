import { useState, type FormEvent } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { RenderLimits } from '../api/types'
import { useAsync } from '../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { LabeledInput, SaveFooter, SettingBody, SettingCard, SuggestInput } from './settings/form'

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
 * failing, up to the tool's own wait; the agent refuses a call once 100 of that caller's
 * renders are waiting, or past the per-10-minute cap, which counts waiting renders.
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
    { id: 'anonymous', label: 'Every anonymous MCP session, together' },
    ...(tokens.data?.tokens ?? []).filter((t) => t.status === 'active').map((t) => ({ id: `token:${t.id}`, label: `MCP token: ${t.name}` })),
  ]

  return (
    <SettingCard title="Assistant render limits">
      <form className="contents" onSubmit={(event) => void save(event)}>
        <SettingBody className="space-y-3">
          <div className="flex flex-wrap gap-4">
            <LabeledInput
              label="Renders at once, per caller"
              type="number"
              inputMode="numeric"
              min={1}
              max={MAX_CONCURRENT}
              step={1}
              required
              value={current.per_principal}
              onChange={(event) => edit({ per_principal: event.target.value })}
              className="w-48"
              aria-describedby="render-limits-help"
            />
            <LabeledInput
              label="Renders at once, in all"
              type="number"
              inputMode="numeric"
              min={0}
              max={MAX_GLOBAL}
              step={1}
              required
              value={current.global}
              onChange={(event) => edit({ global: event.target.value })}
              className="w-48"
              aria-describedby="render-limits-help"
            />
            <LabeledInput
              label="Renders per 10 min, per caller"
              type="number"
              inputMode="numeric"
              min={0}
              max={MAX_PER_WINDOW}
              step={1}
              required
              value={current.per_window}
              onChange={(event) => edit({ per_window: event.target.value })}
              className="w-48"
              aria-describedby="render-limits-help"
            />
          </div>

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm">Per-caller overrides</legend>
            {current.overrides.map((row, index) => (
              <div key={index} className="flex flex-wrap items-end gap-2">
                <div className="grid gap-1.5">
                  <Label htmlFor={`render-limit-caller-${index}`} className="text-xs font-normal">
                    Caller
                  </Label>
                  <SuggestInput
                    id={`render-limit-caller-${index}`}
                    type="text"
                    required
                    value={row.principal}
                    onChange={(event) => editRow(index, { principal: event.target.value })}
                    onFocus={() => setWantTokens(true)}
                    onOpen={() => setWantTokens(true)}
                    suggestions={callers.map((c) => ({ value: c.id, label: c.label }))}
                    onPick={(principal) => editRow(index, { principal })}
                    suggestionsLabel={`Known callers for override ${index + 1}`}
                    className="w-72 max-w-full"
                    placeholder="browser, token:<id>, oidc:<issuer>#<sub>"
                  />
                </div>
                <div className="grid gap-1.5">
                  <Label htmlFor={`render-limit-at-once-${index}`} className="text-xs font-normal">
                    At once
                  </Label>
                  <Input
                    id={`render-limit-at-once-${index}`}
                    type="number"
                    inputMode="numeric"
                    min={1}
                    max={MAX_CONCURRENT}
                    step={1}
                    required
                    value={row.concurrent}
                    onChange={(event) => editRow(index, { concurrent: event.target.value })}
                    className="sb-num w-24"
                  />
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
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
                variant="outline"
                onClick={() =>
                  edit({ overrides: [...current.overrides, { principal: '', concurrent: current.per_principal }] })
                }
                {...USER_ONLY}
              >
                Add override
              </Button>
            </div>
          </fieldset>

          <p id="render-limits-help" className="text-xs text-muted-foreground">
            A render past a limit waits its turn instead of failing, so the assistant and its sub-agents can queue
            renders (up to 100 per caller). &ldquo;In all&rdquo; counts every caller together; 0 means no limit beyond
            the render workers&rsquo; own. The per-10-minute cap stops an assistant stuck in a loop and counts renders
            still waiting; 0 turns it off. Changes apply at once, to renders already waiting too.
          </p>
        </SettingBody>
        <SaveFooter
          saving={saving}
          disabled={!changed}
          saved={saved && !changed ? 'Saved. The assistant uses these limits now.' : null}
          error={error}
        />
      </form>
    </SettingCard>
  )
}
