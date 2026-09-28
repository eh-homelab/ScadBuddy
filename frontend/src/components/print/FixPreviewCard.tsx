import { useId, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type {
  AnalysisRequest,
  AnalyzerDiagnostic,
  AnalyzerFix,
  FixPreview,
  ScopeRef,
  SettingChange,
} from '../../api/types'
import { scopeLabel, scopesForFinding } from '../../lib/analyzers'
import { NEW_TAB } from '../../lib/embed'
import { safeHttpUrl } from '../../lib/safeUrl'
import { useAsync } from '../../lib/useAsync'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'

/** Problem types `post_apply` refuses with (`backend/scadbuddy/api/analyzers.py:78-79`). */
const STALE_PROBLEM = 'https://scadbuddy.dev/problems/analyzer-fix-stale'
const UNVERIFIED_PROBLEM = 'https://scadbuddy.dev/problems/analyzer-fix-unverified'

/** Where an accepted line lands (AI spec §11; `FixTarget`, `analyzers/model.py`). */
const TARGET_LABEL: Record<SettingChange['target'], string> = {
  print_options: 'Print options',
  print_request: 'Print request',
  filament_overrides: 'Filament overrides',
  derived_process_preset: 'Derived process preset',
  project_settings_3mf: '3MF project settings',
}

function show(value: SettingChange['base']): string {
  return value === null || value === undefined ? 'none' : String(value)
}

function scopeValue(scope: ScopeRef): string {
  return `${scope.kind}\u0000${scope.key}`
}

function ChangeRow({ change }: { change: SettingChange }) {
  return (
    <li data-testid={`change-${change.setting}`} className="rounded-[4px] bg-surface-2 px-2 py-1">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <code className="sb-num text-ink">
          {change.setting}
          {change.slot_id !== null && change.slot_id !== undefined && ` (slot ${change.slot_id})`}
        </code>
        <span className="sb-num text-warn">{change.base_known ? show(change.base) : 'unknown'}</span>
        <span aria-hidden="true">→</span>
        <span className="sb-num text-ok">
          {show(change.proposed)}
          {change.unit ? ` ${change.unit}` : ''}
        </span>
        <span className="text-faint">in {TARGET_LABEL[change.target]}</span>
      </div>
      {!change.base_known && change.base_note && (
        <p className="mt-0.5 text-faint">{change.base_note}</p>
      )}
      {change.sources.length > 0 && (
        <p className="mt-0.5">
          {change.sources.map((source, index) => {
            const href = safeHttpUrl(source.url)
            return (
              <span key={`${source.url}-${index}`}>
                {index > 0 && ' · '}
                {href ? (
                  <a href={href} {...NEW_TAB} className="underline decoration-dotted underline-offset-2">
                    {source.title}
                  </a>
                ) : (
                  source.title
                )}
              </span>
            )
          })}
        </p>
      )}
    </li>
  )
}

interface Props {
  outputId: string
  /** The request the dialog would print with now; the preview was made for one of these. */
  request: AnalysisRequest
  diagnostic: AnalyzerDiagnostic
  fix: AnalyzerFix
  scopes: ScopeRef[]
  onApplied: () => void
  onClose: () => void
}

/**
 * #284 — a fix's diff, then apply: `POST /analyzers/fixes/preview` returns the diff and a
 * fingerprint of it, its scope, the print and the base; `POST /analyzers/fixes/apply`
 * must carry that fingerprint back with `confirm: true`, and refuses a moved one with a
 * 409 `analyzer-fix-stale` (`post_apply`, `backend/scadbuddy/api/analyzers.py:432`).
 *
 * The apply sends the dialog's current request, so a choice changed since the preview
 * is refused as stale; the card says so as soon as the request moves, before a click.
 * Clicking Apply on the diff shown is the confirmation. Applying records a decision and
 * sends nothing (`route_note`); the outward approval of AI spec §8.2 belongs to the send
 * that will one day consume it.
 *
 * "Accept for" lists only the scopes the backend resolves this finding at
 * (`scopesForFinding`), and the chosen one is checked against them on every render, as
 * `SuppressForm` does: a scope the report no longer lists falls back to this print.
 */
export function FixPreviewCard({
  outputId,
  request,
  diagnostic,
  fix,
  scopes: offered,
  onApplied,
  onClose,
}: Props) {
  const id = useId()
  const scopes = scopesForFinding(offered, diagnostic)
  const [choice, setChoice] = useState<string | undefined>(undefined)
  const scope = scopes.find((row) => scopeValue(row) === choice) ?? scopes.at(-1)
  const [refusedStale, setRefusedStale] = useState<string | null>(null)
  const [refusal, setRefusal] = useState<string | null>(null)
  const [applying, setApplying] = useState(false)
  const requestKey = JSON.stringify(request)

  const read = useAsync<{ preview: FixPreview; requestKey: string }>(
    async () => ({
      preview: await api.previewFix({
        target: { output_id: outputId },
        request,
        diagnostic_key: diagnostic.key,
        fix_id: fix.id,
        scope: scope ?? null,
      }),
      requestKey,
    }),
    [outputId, diagnostic.key, fix.id, scope?.kind, scope?.key],
  )
  const preview = read.data?.preview
  const moved = read.data !== undefined && read.data.requestKey !== requestKey
  const stale = refusedStale ?? (moved ? 'the print choices changed since this preview' : null)

  /** An apply's refusal is about the preview it was tried on; a new preview starts clean. */
  function clearRefusals() {
    setRefusedStale(null)
    setRefusal(null)
  }

  function previewAgain() {
    clearRefusals()
    read.reload()
  }

  async function apply() {
    if (!preview) return
    setApplying(true)
    setRefusal(null)
    try {
      await api.applyFix({
        target: { output_id: outputId },
        request,
        diagnostic_key: diagnostic.key,
        fix_id: fix.id,
        scope: preview.scope,
        fingerprint: preview.fingerprint,
        confirm: true,
      })
      onApplied()
    } catch (cause) {
      if (cause instanceof ApiError && cause.problem.type === STALE_PROBLEM) {
        setRefusedStale(cause.detail)
      } else if (cause instanceof ApiError && cause.problem.type === UNVERIFIED_PROBLEM) {
        const items = Array.isArray(cause.problem['to_verify']) ? cause.problem['to_verify'] : []
        setRefusal([cause.detail, ...items.map(String)].join(' — '))
      } else {
        setRefusal(cause instanceof ApiError ? cause.detail : 'The fix was not applied.')
      }
    } finally {
      setApplying(false)
    }
  }

  return (
    <div
      role="region"
      aria-label={`Preview of ${fix.title}`}
      data-testid={`fix-preview-${fix.id}`}
      className="mt-2 space-y-2 rounded-[6px] border border-line bg-surface-2 p-2 text-[12px] text-muted"
    >
      <p className="text-ink">{fix.description}</p>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-scope`}>Accept for</label>
        <select
          id={`${id}-scope`}
          value={scope ? scopeValue(scope) : ''}
          onChange={(event) => {
            clearRefusals()
            setChoice(event.target.value)
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

      {read.loading && (
        <p className="flex items-center gap-2">
          <Spinner /> Reading the diff
        </p>
      )}
      {read.error && (
        <p role="alert" className="text-warn">
          {read.error instanceof ApiError ? read.error.detail : 'The fix could not be previewed.'}
        </p>
      )}

      {preview && (
        <>
          <ul className="space-y-1" aria-label="Changes">
            {preview.fix.changes.map((change, index) => (
              <ChangeRow key={`${change.setting}-${change.slot_id ?? ''}-${index}`} change={change} />
            ))}
          </ul>
          {preview.blockers.length > 0 && (
            <div data-testid="fix-blockers">
              <p className="text-ink">Can&apos;t be applied until this is verified:</p>
              <ul className="mt-0.5 list-disc pl-4">
                {preview.blockers.map((blocker) => (
                  <li key={blocker}>{blocker}</li>
                ))}
              </ul>
            </div>
          )}
          <p className="text-faint">{preview.route_note}</p>
        </>
      )}

      {stale && (
        <p role="status" data-testid="fix-stale" className="text-warn">
          Stale: {stale}.{' '}
          <button type="button" onClick={previewAgain} className="underline underline-offset-2">
            Preview again
          </button>
        </p>
      )}
      {refusal && (
        <p role="alert" className="text-warn">
          {refusal}
        </p>
      )}

      <div className="flex justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={onClose} disabled={applying}>
          Close
        </Button>
        <Button
          size="sm"
          onClick={() => void apply()}
          disabled={!preview || !preview.applicable || stale !== null || applying || read.loading}
        >
          {applying && <Spinner />}
          Apply
        </Button>
      </div>
    </div>
  )
}
