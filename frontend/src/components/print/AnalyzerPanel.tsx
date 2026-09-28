import { ApiError } from '../../api/client'
import type {
  AnalysisRequest,
  AnalyzerDiagnostic,
  AnalyzerSeverity,
  AnalyzerSource,
} from '../../api/types'
import { SEVERITY_LABEL, describeLocation, partition, scopeLabel } from '../../lib/analyzers'
import { NEW_TAB } from '../../lib/embed'
import { safeHttpUrl } from '../../lib/safeUrl'
import { useAnalysis } from '../../lib/useAnalysis'
import { Spinner } from '../ui/Spinner'

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

function DiagnosticItem({ diagnostic }: { diagnostic: AnalyzerDiagnostic }) {
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
    </li>
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
              <DiagnosticItem key={diagnostic.key} diagnostic={diagnostic} />
            ))}
          </ul>
        </>
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
