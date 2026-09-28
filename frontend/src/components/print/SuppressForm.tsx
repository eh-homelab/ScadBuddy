import { useId, useState, type FormEvent } from 'react'
import { api, ApiError } from '../../api/client'
import type { AnalyzerDiagnostic, ScopeRef } from '../../api/types'
import { scopeLabel, widerThanTemplate } from '../../lib/analyzers'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'

/** The backend's cap on a decision's reason (`DecisionCreate.reason`, max_length=500). */
const MAX_REASON = 500

function scopeValue(scope: ScopeRef): string {
  return `${scope.kind}\u0000${scope.key}`
}

interface Props {
  diagnostic: AnalyzerDiagnostic
  /** Where a decision about this print can be stored, broadest first (the report's). */
  scopes: ScopeRef[]
  onDone: () => void
  onCancel: () => void
}

/**
 * #284 "Suppress at a scope": like `#pragma warning disable`, a reason is required
 * (`DecisionCreate`, `backend/scadbuddy/api/analyzers.py:172-201`). The narrowest scope,
 * this print, is the default, as it is for a fix in simple mode (`FixRequest.scope`,
 * `analyzers.py:141-143`). `POST /analyzers/decisions` stores it; the run's `analyzers`
 * topic reads the report again.
 *
 * The chosen scope is checked against `scopes` on every render: the report is read
 * again while the form is open (another printer, other filaments), and a scope it no
 * longer lists falls back to this print rather than being posted stale. An `error`
 * finding suppressed wider than the template needs a confirmation, since the backend
 * asks for one only when the decision is enforced.
 *
 * Not offered: `enforced` (a broad decision overriding narrower ones, which for an
 * `error` rule also needs a confirmation) and `ignore`, which records no reason.
 */
export function SuppressForm({ diagnostic, scopes, onDone, onCancel }: Props) {
  const id = useId()
  const [choice, setChoice] = useState<string | undefined>(undefined)
  const scope = scopes.find((row) => scopeValue(row) === choice) ?? scopes.at(-1)
  const [confirmed, setConfirmed] = useState(false)
  const needsConfirm = diagnostic.severity === 'error' && scope !== undefined && widerThanTemplate(scope)
  const [everyInstance, setEveryInstance] = useState(false)
  const [reason, setReason] = useState('')
  const [saving, setSaving] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)

  const ready =
    scope !== undefined && reason.trim() !== '' && !saving && (!needsConfirm || confirmed)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!ready || !scope) return
    setSaving(true)
    setRefusal(null)
    try {
      await api.createDecision({
        diagnostic_id: diagnostic.id,
        instance: everyInstance ? null : diagnostic.key,
        kind: 'suppress',
        scope,
        reason: reason.trim(),
        enforced: false,
        confirm: false,
      })
      onDone()
    } catch (cause) {
      setRefusal(cause instanceof ApiError ? cause.detail : 'The suppression was not saved.')
      setSaving(false)
    }
  }

  return (
    <form
      aria-label={`Suppress ${diagnostic.id}`}
      onSubmit={(event) => void submit(event)}
      className="mt-2 space-y-2 rounded-[6px] border border-line bg-surface-2 p-2"
    >
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-scope`} className="text-[12px] text-muted">
          Scope
        </label>
        <select
          id={`${id}-scope`}
          value={scope ? scopeValue(scope) : ''}
          onChange={(event) => {
            setChoice(event.target.value)
            setConfirmed(false)
          }}
          className="sb-field"
        >
          {scopes.map((row) => (
            <option key={scopeValue(row)} value={scopeValue(row)} title={row.key}>
              {scopeLabel(row)}
            </option>
          ))}
        </select>
      </div>
      <label className="flex items-center gap-2 text-[12px] text-muted">
        <input
          type="checkbox"
          checked={everyInstance}
          onChange={(event) => setEveryInstance(event.target.checked)}
          className="accent-[var(--sb-accent)]"
        />
        Every {diagnostic.id} finding
      </label>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-reason`} className="text-[12px] text-muted">
          Reason
        </label>
        <textarea
          id={`${id}-reason`}
          required
          maxLength={MAX_REASON}
          rows={2}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          className="sb-field h-auto py-1.5"
        />
      </div>
      {needsConfirm && scope && (
        <label className="flex items-start gap-2 text-[12px] text-warn">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
            className="mt-0.5 accent-[var(--sb-accent)]"
          />
          {`${diagnostic.id} is a problem. Suppress it for ${scopeLabel(scope).toLowerCase()}, not only this template?`}
        </label>
      )}
      {refusal && (
        <p role="alert" className="text-[12px] text-warn">
          {refusal}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <Button size="sm" type="submit" disabled={!ready}>
          {saving && <Spinner />}
          Suppress
        </Button>
      </div>
    </form>
  )
}
