import { useState, type ReactNode } from 'react'
import { api, ApiError } from '../../api/client'
import type {
  AnalysisRequest,
  AnalysisTarget,
  AnalyzerDecision,
  AnalyzerDiagnostic,
  AnalyzerSeverity,
  AnalyzerSource,
  ScopeRef,
} from '../../api/types'
import {
  SEVERITY_LABEL,
  describeLocation,
  partition,
  scopeLabel,
  scopesForFinding,
  widerThanTemplate,
} from '../../lib/analyzers'
import { NEW_TAB } from '../../lib/embed'
import { safeHttpUrl } from '../../lib/safeUrl'
import { useAnalysis } from '../../lib/useAnalysis'
import { ChevronRightIcon } from 'lucide-react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { FixPreviewCard } from './FixPreviewCard'
import { SuppressForm } from './SuppressForm'

/** A severity's badge (`Badge`), in the callout colours `PrintVerdict` uses. */
const TONE: Record<AnalyzerSeverity, string> = {
  error: 'border-destructive/40 bg-destructive/10 text-destructive',
  warning: 'border-accent/50 bg-accent/10 text-accent',
  info: 'border-border bg-ui-muted text-muted-foreground',
  hidden: 'border-border bg-transparent text-muted-foreground',
}

/** A text-sized link button, for the actions inside a sentence. */
const INLINE = 'h-auto p-0 text-[12px] text-inherit underline underline-offset-2 hover:text-foreground'

function SourceList({ sources }: { sources: AnalyzerSource[] }) {
  if (sources.length === 0) return null
  return (
    <ul className="mt-1.5 space-y-0.5 text-[12px]" aria-label="Sources">
      {sources.map((source, index) => {
        const href = safeHttpUrl(source.url)
        return (
          <li key={`${source.url}-${index}`} className="text-muted">
            {href ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <a
                    href={href}
                    {...NEW_TAB}
                    aria-description={`Read ${source.accessed}`}
                    className="underline decoration-dotted underline-offset-2 hover:text-foreground"
                  >
                    {source.title}
                  </a>
                </TooltipTrigger>
                <TooltipContent>Read {source.accessed}</TooltipContent>
              </Tooltip>
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
  /** Whether decisions are stored at all, and why not when they are not (#2927). */
  decisions: { available: boolean; reason: string | null }
  target: AnalysisTarget | undefined
  request: AnalysisRequest | null
  onChanged: () => void
}

function DiagnosticItem({ diagnostic, scopes, decisions, target, request, onChanged }: ItemProps) {
  const [suppressing, setSuppressing] = useState(false)
  const [previewing, setPreviewing] = useState<string | null>(null)
  const applied = diagnostic.decision
  const acceptedFix =
    applied?.decision.kind === 'accept'
      ? diagnostic.fixes?.find((row) => row.id === applied.decision.fix_id)
      : undefined
  const fixes = diagnostic.status === 'accepted' ? [] : (diagnostic.fixes ?? [])
  const previewed = fixes.find((row) => row.id === previewing)
  /** A preview changes nothing, so it needs no decisions store; only Apply does. */
  const canPreview = target !== undefined && request !== null && fixes.length > 0
  return (
    <li data-testid={`diagnostic-${diagnostic.key}`}>
      <Card size="sm" className="gap-0 py-2">
      <CardContent className="px-3">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <Badge variant="outline" className={cn('h-auto rounded-md px-1.5 text-[11px]', TONE[diagnostic.severity])}>
          {SEVERITY_LABEL[diagnostic.severity]}
        </Badge>
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
      {!suppressing && previewed === undefined && (canPreview || scopes.length > 0) && (
        <div className="mt-1.5 flex flex-wrap gap-2">
          {canPreview &&
            fixes.map((fix) => (
              <Button key={fix.id} type="button" size="sm" variant="outline" onClick={() => setPreviewing(fix.id)}>
                Preview fix: {fix.title}
              </Button>
            ))}
          {scopes.length > 0 && (
            <Button type="button" size="sm" variant="ghost" onClick={() => setSuppressing(true)}>
              Suppress…
            </Button>
          )}
        </div>
      )}
      {previewed && target !== undefined && request !== null && (
        <FixPreviewCard
          target={target}
          request={request}
          diagnostic={diagnostic}
          fix={previewed}
          scopes={scopes}
          decisions={decisions}
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
      </CardContent>
      </Card>
    </li>
  )
}

/**
 * Removes the decision that set a finding aside, so it is open again. Keyed by the
 * decision, so the next one deciding the same finding (a wider suppression beneath the
 * one removed) starts with its own state. An enforced decision was made to override
 * narrower ones (`post_decision`), so it is not removed from here; one wider than the
 * template reaches other models' prints, so removing it is confirmed first.
 */
function RemoveDecision({ diagnostic, onChanged }: { diagnostic: AnalyzerDiagnostic; onChanged: () => void }) {
  const applied = diagnostic.decision?.decision
  if (!applied) return null
  return <RemoveButton key={applied.id} decision={applied} diagnostic={diagnostic} onChanged={onChanged} />
}

interface RemoveProps {
  decision: AnalyzerDecision
  diagnostic: AnalyzerDiagnostic
  onChanged: () => void
}

function RemoveButton({ decision, diagnostic, onChanged }: RemoveProps) {
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)
  const verb = { accept: 'accepted fix', suppress: 'suppression', ignore: 'ignore' }[decision.kind]
  if (decision.enforced) {
    return <span className="ml-1 text-faint">(enforced, so not removable from here)</span>
  }
  const wide = widerThanTemplate(decision.scope)

  function remove() {
    setBusy(true)
    setConfirming(false)
    setRefusal(null)
    api
      .deleteDecision(decision.id)
      .then(onChanged)
      .catch((cause: unknown) => {
        setRefusal(cause instanceof ApiError ? cause.detail : 'It was not removed.')
        setBusy(false)
      })
  }

  return (
    <>
      {' '}
      <Button
        type="button"
        variant="link"
        disabled={busy}
        aria-label={`Remove the ${verb} of ${diagnostic.key}`}
        onClick={wide ? () => setConfirming(true) : remove}
        className={INLINE}
      >
        Remove
      </Button>
      {/* One wider than the template reaches other models' prints: confirmed first. */}
      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{`Remove the ${verb} of ${diagnostic.key}?`}</AlertDialogTitle>
            <AlertDialogDescription>
              Remove it for {scopeLabel(decision.scope).toLowerCase()}? It applies beyond this template.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel aria-label={`Keep the ${verb} of ${diagnostic.key}`}>Keep</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              aria-label={`Confirm removing the ${verb} of ${diagnostic.key}`}
              onClick={remove}
            >
              Remove
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      {refusal && (
        <span role="alert" className="ml-1 text-destructive">
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
  /** The output, or the library file (#1753), the dialog prints. */
  target: AnalysisTarget | undefined
  /** The request the dialog would print with; `null` until it has one. */
  request: AnalysisRequest | null
  /** Printing several plates (#3652): the mesh checks still read one (`AnalysisRequest.plate_id`). */
  allPlates?: boolean
  /** #755, #760 — the run's own refusals, listed first: the one check that holds Print. */
  children?: ReactNode
}

/**
 * #284 — the print analyzers (#461) over what the dialog would send, listed in the
 * dialog. Advisory throughout: no finding disables Print (`lib/analyzers.ts`). The
 * nozzle verdict passed as `children` is not an analyzer finding, and its errors do.
 */
export function AnalyzerPanel({ target, request, allPlates = false, children }: Props) {
  const { report, error, checking, reload } = useAnalysis(target, request)
  const { shown, setAside } = partition(report?.diagnostics ?? [])
  const skipped = report?.skipped ?? []
  /** Decisions are stored in Postgres; without it the run says why (`decisions_reason`). */
  const decidable = report?.decisions_available ?? false
  const accepted = report?.accepted_changes?.length ?? 0

  return (
    <Card
      size="sm"
      role="region"
      aria-labelledby="print-checks-title"
      aria-busy={checking}
      data-testid="print-checks"
      className="gap-0"
    >
      <CardContent>
      <div className="flex items-center gap-2">
        <h3 id="print-checks-title" className="text-[13px] font-medium text-foreground">
          Checks
        </h3>
        {report && (
          <span data-testid="checks-headline" className="text-[12px] text-muted-foreground">
            {report.summary.headline}
          </span>
        )}
        {checking && <Skeleton aria-hidden="true" className="h-3 w-16" data-testid="checks-checking" />}
      </div>

      {children}

      {error && (
        <p role="alert" className="mt-1.5 text-[12px] text-destructive">
          The checks could not run:{' '}
          {error instanceof ApiError ? error.detail : 'ScadBuddy did not answer.'}{' '}
          <Button type="button" variant="link" onClick={reload} className={INLINE}>
            Check again
          </Button>
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
                scopes={decidable && report ? scopesForFinding(report.scopes, diagnostic) : []}
                decisions={{ available: decidable, reason: report?.decisions_reason ?? null }}
                target={target}
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
          The mesh checks read plate {request?.plate_id ?? 1}; the plate-fit and filament checks read{' '}
          {request?.plate_ids ? 'each chosen plate' : 'every plate'}.
        </p>
      )}

      {skipped.length > 0 && (
        <Disclosure summary={`${skipped.length} ${skipped.length === 1 ? 'check' : 'checks'} did not run`}>
          <ul className="mt-1 space-y-0.5 pl-3" data-testid="checks-skipped">
            {skipped.map((row) => (
              <li key={row.id}>
                <code className="sb-num">{row.id}</code> {row.title}:{' '}
                {row.missing.map((input) => input.reason ?? `no ${input.name}`).join('; ')}
              </li>
            ))}
          </ul>
        </Disclosure>
      )}

      {setAside.length > 0 && (
        <Disclosure summary={`${setAside.length} not shown`}>
          <ul className="mt-1 space-y-0.5 pl-3" data-testid="checks-set-aside">
            {setAside.map((diagnostic) => (
              <li key={diagnostic.key}>
                <code className="sb-num">{diagnostic.id}</code> {diagnostic.title} —{' '}
                {setAsideReason(diagnostic)}
                {decidable && <RemoveDecision diagnostic={diagnostic} onChanged={reload} />}
              </li>
            ))}
          </ul>
        </Disclosure>
      )}

      {report && !report.decisions_available && report.decisions_reason && (
        <p className="mt-1.5 text-[12px] text-faint">{report.decisions_reason}</p>
      )}
      </CardContent>
    </Card>
  )
}

/** A list folded under its count, opened by a click or Enter on the count. */
function Disclosure({ summary, children }: { summary: string; children: ReactNode }) {
  return (
    <Collapsible className="mt-1.5 text-[12px] text-muted-foreground">
      <CollapsibleTrigger className="group/disclosure flex cursor-pointer items-center gap-1 rounded-sm outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
        <ChevronRightIcon
          aria-hidden="true"
          className="size-3.5 transition-transform group-data-[state=open]/disclosure:rotate-90"
        />
        {summary}
      </CollapsibleTrigger>
      <CollapsibleContent>{children}</CollapsibleContent>
    </Collapsible>
  )
}
