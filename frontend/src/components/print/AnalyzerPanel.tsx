import { useState } from 'react'
import { api, ApiError } from '../../api/client'
import type {
  AnalysisRequest,
  AnalyzerDiagnostic,
  AnalyzerSeverity,
  AnalyzerSource,
  ScopeRef,
} from '../../api/types'
import { SEVERITY_LABEL, describeLocation, partition, scopeLabel } from '../../lib/analyzers'
import { NEW_TAB } from '../../lib/embed'
import { safeHttpUrl } from '../../lib/safeUrl'
import { useAnalysis } from '../../lib/useAnalysis'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'
import { FixPreviewCard } from './FixPreviewCard'
import { SuppressForm } from './SuppressForm'

const TONE: Record<AnalyzerSeverity, string> = {
  error: 'border-warn/50 bg-warn/10 text-warn',
  warning: 'border-accent/50 bg-accent/10 text-accent',
  info: 'border-line-strong bg-surface-3 text-muted',
  hidden: 'border-line bg-surface-2 text-faint',
}

function SourceList({ sources }: { sources: AnalyzerSource[] }) {
  if (sources.length === 0) return null
  return (
    <ul className="mt-1.5 space-y-0.5 text-[12px]" aria-label="Sources">
      {sources.map((source, index) => {
        const href = safeHttpUrl(source.url)
        return (
          <li key={`${source.url}-${index}`} className="text-muted">
            {href ? (
              <a
                href={href}
                {...NEW_TAB}
                title={`Read ${source.accessed}`}
                className="underline decoration-dotted underline-offset-2 hover:text-ink"
              >
                {source.title}
              </a>
            ) : (
              source.title
            )}
            {': '}
            <q className="text-faint">{source.quote}</q>
          </li>
        )
      })}
    </ul>
  )
}

interface ItemProps {
  diagnostic: AnalyzerDiagnostic
  /** Where a decision can be stored; empty when no decision can be (no store). */
  scopes: ScopeRef[]
  outputId: string | undefined
  request: AnalysisRequest | null
  onChanged: () => void
}

function DiagnosticItem({ diagnostic, scopes, outputId, request, onChanged }: ItemProps) {
  const [suppressing, setSuppressing] = useState(false)
  const [previewing, setPreviewing] = useState<string | null>(null)
  const applied = diagnostic.decision
  const acceptedFix =
    applied?.decision.kind === 'accept'
      ? diagnostic.fixes?.find((row) => row.id === applied.decision.fix_id)
      : undefined
  const fixes = diagnostic.status === 'accepted' ? [] : (diagnostic.fixes ?? [])
  const previewed = fixes.find((row) => row.id === previewing)
  return (
    <li
      data-testid={`diagnostic-${diagnostic.key}`}
      className="rounded-[6px] border border-line bg-surface px-2.5 py-2"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className={`rounded-[4px] border px-1.5 text-[11px] ${TONE[diagnostic.severity]}`}>
          {SEVERITY_LABEL[diagnostic.severity]}
        </span>
        <code className="sb-num text-[12px] text-muted">{diagnostic.id}</code>
        <span className="text-[13px] text-ink">{diagnostic.title}</span>
      </div>
      <p className="mt-1 text-[12.5px] text-ink">{diagnostic.message}</p>
      {diagnostic.location && (
        <p className="mt-0.5 text-[12px] text-muted">
          Where: {describeLocation(diagnostic.location)}
        </p>
      )}
      {diagnostic.why && <p className="mt-0.5 text-[12px] text-faint">{diagnostic.why}</p>}
      <SourceList sources={diagnostic.sources} />
      {applied?.decision.kind === 'accept' && !applied.stale && (
        <p data-testid={`fix-accepted-${diagnostic.key}`} className="mt-1.5 text-[12px] text-ok">
          Fix accepted for {scopeLabel(applied.decision.scope)}
          {acceptedFix ? `: ${acceptedFix.title}` : ''}.
          <RemoveDecision diagnostic={diagnostic} onChanged={onChanged} />
        </p>
      )}
      {applied?.decision.kind === 'accept' && applied.stale && (
        // model.py AppliedDecision.stale: the diff changed, so the finding is open again (#284).
        <p data-testid={`fix-stale-${diagnostic.key}`} className="mt-1.5 text-[12px] text-warn">
          The fix accepted for {scopeLabel(applied.decision.scope)} is stale: its diff has changed
          since, so this finding is open again.
          <RemoveDecision diagnostic={diagnostic} onChanged={onChanged} />
        </p>
      )}
      {scopes.length > 0 && !suppressing && previewed === undefined && (
        <div className="mt-1.5 flex flex-wrap gap-2">
          {outputId !== undefined &&
            request !== null &&
            fixes.map((fix) => (
              <Button key={fix.id} size="sm" onClick={() => setPreviewing(fix.id)}>
                Preview fix: {fix.title}
              </Button>
            ))}
          <Button size="sm" variant="ghost" onClick={() => setSuppressing(true)}>
            Suppress…
          </Button>
        </div>
      )}
      {previewed && outputId !== undefined && request !== null && (
        <FixPreviewCard
          outputId={outputId}
          request={request}
          diagnostic={diagnostic}
          fix={previewed}
          scopes={scopes}
          onClose={() => setPreviewing(null)}
          onApplied={() => {
            setPreviewing(null)
            onChanged()
          }}
        />
      )}
      {suppressing && (
        <SuppressForm
          diagnostic={diagnostic}
          scopes={scopes}
          onCancel={() => setSuppressing(false)}
          onDone={() => {
            setSuppressing(false)
            onChanged()
          }}
        />
      )}
    </li>
  )
}

/** Removes the decision that set a finding aside, so it is open again. */
function RemoveDecision({ diagnostic, onChanged }: { diagnostic: AnalyzerDiagnostic; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)
  const applied = diagnostic.decision?.decision
  if (!applied) return null
  const verb = { accept: 'accepted fix', suppress: 'suppression', ignore: 'ignore' }[applied.kind]
  return (
    <>
      {' '}
      <button
        type="button"
        disabled={busy}
        aria-label={`Remove the ${verb} of ${diagnostic.id}`}
        onClick={() => {
          setBusy(true)
          setRefusal(null)
          api
            .deleteDecision(applied.id)
            .then(onChanged)
            .catch((cause: unknown) => {
              setRefusal(cause instanceof ApiError ? cause.detail : 'It was not removed.')
              setBusy(false)
            })
        }}
        className="underline underline-offset-2 hover:text-ink disabled:opacity-45"
      >
        Remove
      </button>
      {refusal && (
        <span role="alert" className="ml-1 text-warn">
          {refusal}
        </span>
      )}
    </>
  )
}

function setAsideReason(diagnostic: AnalyzerDiagnostic): string {
  const applied = diagnostic.decision?.decision
  if (!applied || (diagnostic.status !== 'suppressed' && diagnostic.status !== 'ignored')) {
    return 'hidden'
  }
  const reason = applied.reason ? `: ${applied.reason}` : ''
  return `${diagnostic.status} for ${scopeLabel(applied.scope)}${reason}`
}

interface Props {
  outputId: string | undefined
  /** The request the dialog would print with; `null` until it has one. */
  request: AnalysisRequest | null
  /** Printing every plate: the analyzers take one plate (`AnalysisRequest.plate_id`). */
  allPlates?: boolean
}

/**
 * #284 — the print analyzers (#461) over what the dialog would send, listed in the
 * dialog. Advisory throughout: no finding disables Print (`lib/analyzers.ts`).
 */
export function AnalyzerPanel({ outputId, request, allPlates = false }: Props) {
  const { report, error, checking, reload } = useAnalysis(outputId, request)
  const { shown, setAside } = partition(report?.diagnostics ?? [])
  const skipped = report?.skipped ?? []
  /** Decisions are stored in Postgres; without it the run says why (`decisions_reason`). */
  const decidable = report?.decisions_available ?? false
  const accepted = report?.accepted_changes?.length ?? 0

  return (
    <section
      aria-labelledby="print-checks-title"
      aria-busy={checking}
      data-testid="print-checks"
      className="rounded-[6px] border border-line bg-surface-2 px-3 py-2"
    >
      <div className="flex items-center gap-2">
        <h3 id="print-checks-title" className="text-[13px] text-ink">
          Checks
        </h3>
        {report && (
          <span data-testid="checks-headline" className="text-[12px] text-muted">
            {report.summary.headline}
          </span>
        )}
        {checking && <Spinner />}
      </div>

      {error && (
        <p role="alert" className="mt-1.5 text-[12px] text-warn">
          The checks could not run:{' '}
          {error instanceof ApiError ? error.detail : 'ScadBuddy did not answer.'}{' '}
          <button type="button" onClick={reload} className="underline underline-offset-2">
            Check again
          </button>
        </p>
      )}

      {shown.length > 0 && (
        <>
          <p className="mt-1 text-[12px] text-faint">
            Advisory: none of these stops Print.
          </p>
          <ul className="mt-2 space-y-2">
            {shown.map((diagnostic) => (
              <DiagnosticItem
                key={diagnostic.key}
                diagnostic={diagnostic}
                scopes={decidable ? (report?.scopes ?? []) : []}
                outputId={outputId}
                request={request}
                onChanged={reload}
              />
            ))}
          </ul>
        </>
      )}

      {accepted > 0 && (
        // backend/scadbuddy/api/analyzers.py:7-9: "Applying records a decision; it sends
        // nothing. … Nothing reads ``accepted_changes`` into a print yet."
        <p data-testid="accepted-changes" className="mt-1.5 text-[12px] text-faint">
          {accepted} accepted {accepted === 1 ? 'change is' : 'changes are'} recorded; nothing
          sends {accepted === 1 ? 'it' : 'them'} to Bambuddy yet.
        </p>
      )}

      {report && allPlates && (
        <p className="mt-1.5 text-[12px] text-faint">
          The mesh checks read plate 1; the plate-fit check reads every plate.
        </p>
      )}

      {skipped.length > 0 && (
        <details className="mt-1.5 text-[12px] text-muted">
          <summary className="cursor-pointer">
            {skipped.length} {skipped.length === 1 ? 'check' : 'checks'} did not run
          </summary>
          <ul className="mt-1 space-y-0.5 pl-3" data-testid="checks-skipped">
            {skipped.map((row) => (
              <li key={row.id}>
                <code className="sb-num">{row.id}</code> {row.title}:{' '}
                {row.missing.map((input) => input.reason ?? `no ${input.name}`).join('; ')}
              </li>
            ))}
          </ul>
        </details>
      )}

      {setAside.length > 0 && (
        <details className="mt-1.5 text-[12px] text-muted">
          <summary className="cursor-pointer">{setAside.length} not shown</summary>
          <ul className="mt-1 space-y-0.5 pl-3" data-testid="checks-set-aside">
            {setAside.map((diagnostic) => (
              <li key={diagnostic.key}>
                <code className="sb-num">{diagnostic.id}</code> {diagnostic.title} —{' '}
                {setAsideReason(diagnostic)}
                {decidable && <RemoveDecision diagnostic={diagnostic} onChanged={reload} />}
              </li>
            ))}
          </ul>
        </details>
      )}

      {report && !report.decisions_available && report.decisions_reason && (
        <p className="mt-1.5 text-[12px] text-faint">{report.decisions_reason}</p>
      )}
    </section>
  )
}
