import { useId, useState, type FormEvent } from 'react'
import { api, ApiError } from '../../api/client'
import type { AnalyzerDiagnostic, ScopeRef } from '../../api/types'
import { atOrNarrower, sameScope, scopeLabel, widerThanTemplate } from '../../lib/analyzers'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { Spinner } from '../ui/spinner'

/** The backend's cap on a decision's reason (`DecisionCreate.reason`, max_length=500). */
const MAX_REASON = 500

function scopeValue(scope: ScopeRef): string {
  return `${scope.kind}\u0000${scope.key}`
}

type Kind = 'suppress' | 'ignore'

/** What each decision kind does, shown under the choice (`runner.apply_decisions`). */
const KIND_HELP: Record<Kind, string> = {
  suppress: 'Sets the finding aside with a reason, as #pragma warning disable does.',
  ignore: 'Sets the finding aside and records no reason; one is optional.',
}

const KIND_VERB: Record<Kind, string> = { suppress: 'Suppress', ignore: 'Ignore' }

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
 * finding set aside wider than the template needs a confirmation here, and so does one
 * enforced at any scope: the backend asks (`confirm`, 428) only for an enforced
 * suppression of an `error` rule, and the form asks for both kinds alike, since an
 * ignore hides it as surely.
 *
 * A finding with an accepted fix is still listed, and the backend takes the narrowest
 * decision (`decisions.resolve`): a suppression wider than the acceptance would be
 * stored and never apply. So only the acceptance's scope and narrower ones are offered,
 * and at the acceptance's own scope the suppression is about this finding, as the
 * acceptance is (`post_apply` stores `instance=diagnostic.key`), so it replaces it
 * rather than losing to it.
 *
 * Both kinds `DecisionCreate` takes are offered (#4967): `suppress`, which needs a
 * reason, and `ignore`, which does not (`_well_formed`). `enforced` makes the decision
 * win over any narrower one (`decisions.resolve`), so it is offered only above this
 * print, the narrowest scope, where nothing is narrower; the panel does not remove an
 * enforced decision (`RemoveButton`).
 */
export function SuppressForm({ diagnostic, scopes: offered, onDone, onCancel }: Props) {
  const id = useId()
  const applied = diagnostic.decision
  const accepted = applied?.decision.kind === 'accept' && !applied.stale ? applied.decision : undefined
  const scopes = accepted ? atOrNarrower(offered, accepted.scope) : offered
  const [choice, setChoice] = useState<string | undefined>(undefined)
  const scope = scopes.find((row) => scopeValue(row) === choice) ?? scopes.at(-1)
  const [kind, setKind] = useState<Kind>('suppress')
  const [enforcedChosen, setEnforced] = useState(false)
  const canEnforce = scope !== undefined && scope.kind !== 'print'
  const enforced = enforcedChosen && canEnforce
  const [confirmed, setConfirmed] = useState(false)
  const needsConfirm =
    diagnostic.severity === 'error' && scope !== undefined && (enforced || widerThanTemplate(scope))
  const [everyInstanceChosen, setEveryInstance] = useState(false)
  const replacesAccepted = accepted !== undefined && scope !== undefined && sameScope(scope, accepted.scope)
  const everyInstance = everyInstanceChosen && !replacesAccepted
  const [reason, setReason] = useState('')
  const [saving, setSaving] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)

  const ready =
    scope !== undefined &&
    (kind === 'ignore' || reason.trim() !== '') &&
    !saving &&
    (!needsConfirm || confirmed)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!ready || !scope) return
    setSaving(true)
    setRefusal(null)
    try {
      await api.createDecision({
        diagnostic_id: diagnostic.id,
        instance: everyInstance ? null : diagnostic.key,
        kind,
        scope,
        reason: reason.trim() || null,
        enforced,
        confirm: needsConfirm && confirmed,
      })
      onDone()
    } catch (cause) {
      setRefusal(cause instanceof ApiError ? cause.detail : 'The decision was not saved.')
      setSaving(false)
    }
  }

  return (
    <Card size="sm" className="mt-2">
    <CardContent>
    <form
      aria-label={`Suppress ${diagnostic.key}`}
      onSubmit={(event) => void submit(event)}
      className="space-y-2"
    >
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-kind`} className="text-[12px] font-normal text-muted-foreground">
          Decision
        </Label>
        <Select
          value={kind}
          onValueChange={(next) => {
            setKind(next as Kind)
            setConfirmed(false)
          }}
        >
          <SelectTrigger id={`${id}-kind`} size="sm" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="suppress">Suppress</SelectItem>
            <SelectItem value="ignore">Ignore</SelectItem>
          </SelectContent>
        </Select>
        <p className="text-[12px] text-muted-foreground">{KIND_HELP[kind]}</p>
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-scope`} className="text-[12px] font-normal text-muted-foreground">
          Scope
        </Label>
        <Select
          value={scope ? scopeValue(scope) : undefined}
          onValueChange={(next) => {
            setChoice(next)
            setConfirmed(false)
          }}
        >
          <SelectTrigger id={`${id}-scope`} size="sm" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {scopes.map((row) => (
              <SelectItem key={scopeValue(row)} value={scopeValue(row)}>
                {scopeLabel(row)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {accepted && (
        <p className="text-[12px] text-muted-foreground">
          {replacesAccepted
            ? `This replaces the fix accepted for ${scopeLabel(accepted.scope).toLowerCase()}, so it is about this finding only.`
            : `A fix is accepted for ${scopeLabel(accepted.scope).toLowerCase()}; a wider decision would not apply, so only that scope and narrower ones are offered.`}
        </p>
      )}
      <div className="flex items-center gap-2">
        <Checkbox
          id={`${id}-every`}
          checked={everyInstance}
          disabled={replacesAccepted}
          onCheckedChange={(next) => setEveryInstance(next === true)}
        />
        <Label htmlFor={`${id}-every`} className="text-[12px] font-normal text-muted-foreground">
          Every {diagnostic.id} finding
        </Label>
      </div>
      {canEnforce && (
        <div className="flex items-start gap-2">
          <Checkbox
            id={`${id}-enforce`}
            checked={enforced}
            onCheckedChange={(next) => {
              setEnforced(next === true)
              setConfirmed(false)
            }}
            className="mt-0.5"
          />
          <div className="flex flex-col gap-0.5">
            <Label htmlFor={`${id}-enforce`} className="text-[12px] font-normal text-muted-foreground">
              Enforce over narrower scopes
            </Label>
            {enforced && (
              <p className="text-[12px] text-muted-foreground">
                It wins over any decision made for a narrower scope, and is not removable from
                the print dialog.
              </p>
            )}
          </div>
        </div>
      )}
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-reason`} className="text-[12px] font-normal text-muted-foreground">
          {kind === 'ignore' ? 'Reason (optional)' : 'Reason'}
        </Label>
        <Textarea
          id={`${id}-reason`}
          required={kind === 'suppress'}
          maxLength={MAX_REASON}
          rows={2}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          className="min-h-0 text-[13px]"
        />
      </div>
      {needsConfirm && scope && (
        <div className="flex items-start gap-2">
          <Checkbox
            id={`${id}-confirm`}
            checked={confirmed}
            onCheckedChange={(next) => setConfirmed(next === true)}
            className="mt-0.5"
          />
          <Label htmlFor={`${id}-confirm`} className="text-[12px] leading-snug font-normal text-destructive">
            {enforced
              ? `${diagnostic.id} is a problem. ${KIND_VERB[kind]} it for ${scopeLabel(scope).toLowerCase()} over every narrower decision?`
              : `${diagnostic.id} is a problem. ${KIND_VERB[kind]} it for ${scopeLabel(scope).toLowerCase()}, not only this template?`}
          </Label>
        </div>
      )}
      {refusal && (
        <p role="alert" className="text-[12px] text-destructive">
          {refusal}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <Button size="sm" type="submit" disabled={!ready}>
          {saving && <Spinner />}
          {KIND_VERB[kind]}
        </Button>
      </div>
    </form>
    </CardContent>
    </Card>
  )
}
