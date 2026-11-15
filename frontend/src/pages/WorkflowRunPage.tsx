import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import type { FlowRunView, FlowStep } from '../api/types'
import { FlowScript } from '../components/flows/FlowScript'
import { ForgetRun } from '../components/flows/ForgetRun'
import { GrantPanel } from '../components/flows/GrantPanel'
import { ResetPanel } from '../components/flows/ResetPanel'
import { ChildCard, type ChildEntry, PendingCard } from '../components/flows/PendingCard'
import { ChevronRightIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import {
  actorText,
  FLOW_POLL_MS,
  FLOW_STATUS_LABEL,
  flowPath,
  flowRunPath,
  flowRunTopic,
  flowTitle,
  isStaleEntry,
  STALE_ENTRY_MESSAGE,
} from '../lib/flows'
import { formatBytes, formatDuration, timeAgo } from '../lib/format'
import { temporalWorkflowUrl } from '../lib/temporal'
import { useAsync } from '../lib/useAsync'
import { useReadOnPendingChanges } from '../lib/pendingChanges'
import { usePollWhileUnavailable } from '../lib/usePollWhileUnavailable'
import { ATTENTION_POLL_MS } from '../agent/attention'

/** A box of text set apart: a result. */
const INSET = 'rounded-lg bg-muted/50 p-2 ring-1 ring-foreground/10'

/**
 * #1057 — one flow run (`/workflows/runs/:id`, plan 2026-10-09 Task F1): its steps, and
 * what it waits on, answered or approved here (`POST …/answer`, `…/decide`). Live over
 * `workflow-run:<id>`, and read every 5 s while the socket is down. #2350 — the version
 * it ran and that version's script, who started it, a Reset (preview first, then a
 * confirmation), and forgetting it. #2481 — its grant, when it has one (`GrantPanel`).
 */
export function WorkflowRunPage() {
  const { id = '' } = useParams()
  const view = useAsync(() => api.getFlowRun(id), [id], [flowRunTopic(id)])
  // #2482 — the run's one queue, from the agent: the steps beside the run's own calls,
  // and its child sessions' calls, which the backend's view does not hold.
  const queue = useAsync(() => api.getFlowRunQueue(id), [id], [flowRunTopic(id)])
  const settings = useAsync(() => api.getSettings(), []).data
  usePollWhileUnavailable(() => view.refresh(), FLOW_POLL_MS)
  // A child's call is decided or ends in its own session, which no run topic announces:
  // the agent's pending-input stream does (#265).
  useReadOnPendingChanges(
    (queue.data?.entries ?? []).some((e) => e.source === 'child'),
    () => queue.refresh(),
    ATTENTION_POLL_MS,
  )
  const [notice, setNotice] = useState<string>()

  const run = view.data?.run ?? null
  const temporal = run ? temporalWorkflowUrl(settings, run.workflow_id) : null

  async function respond(send: () => Promise<unknown>) {
    setNotice(undefined)
    try {
      await send()
    } catch (error) {
      setNotice(isStaleEntry(error) ? STALE_ENTRY_MESSAGE : error instanceof Error ? error.message : String(error))
    }
    view.refresh()
    queue.refresh()
  }
  const queued = queue.data?.entries ?? []
  const steps = new Map(queued.flatMap((e) => (e.source === 'flow' && e.step ? [[e.call_id, e.step] as const] : [])))
  const children = queued.filter((e): e is ChildEntry => e.source === 'child')

  return (
    <TooltipProvider>
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-5xl px-4 py-6">
          <div className="mb-5 flex items-baseline gap-2">
            <Link to="/workflows" className="text-[12px] text-muted-foreground hover:text-foreground">
              Workflows
            </Link>
            <span className="text-muted-foreground">/</span>
            <h1 className="text-[13px] font-medium">
              {view.data ? (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Link to={flowPath(view.data.definition_id)} className="hover:underline">
                      {flowTitle(view.data)} v{view.data.version}
                    </Link>
                  </TooltipTrigger>
                  <TooltipContent>The flow version it ran</TooltipContent>
                </Tooltip>
              ) : (
                id
              )}
            </h1>
            {temporal && (
              <a href={temporal} target="_blank" rel="noreferrer" className="ml-auto text-[12px] text-primary hover:underline">
                Temporal UI ↗
              </a>
            )}
          </div>

          {view.error ? (
            <p role="alert" className="text-[13px] text-destructive">
              {view.error instanceof ApiError && view.error.status === 404 ? 'No such flow run.' : view.error.message}
            </p>
          ) : !view.data ? (
            <div role="status" aria-label="Loading" className="flex flex-col gap-3">
              <Skeleton className="h-5 w-64" />
              <Skeleton className="h-24 w-full rounded-xl" />
              <Skeleton className="h-40 w-full rounded-xl" />
            </div>
          ) : (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[13px]">
                <p className="flex items-center gap-1.5">
                  <span className="text-muted-foreground">Status</span>
                  <Badge variant="secondary" data-testid="flow-run-status">
                    {FLOW_STATUS_LABEL[view.data.status]}
                  </Badge>
                </p>
                {run && (
                  <p>
                    <span className="text-muted-foreground">Started by </span>
                    {actorText(run.started_by)}
                    {/* #2484: a trigger's run, as whoever made the trigger. */}
                    {view.data.trigger && (
                      <span className="text-muted-foreground">
                        {' '}
                        through {view.data.trigger.kind === 'schedule' ? 'a schedule' : 'an event trigger'}, fired{' '}
                        {timeAgo(view.data.trigger.fired_at)}
                      </span>
                    )}
                  </p>
                )}
                {run?.parent_run_id && (
                  <p>
                    <span className="text-muted-foreground">Started from </span>
                    <Link to={flowRunPath(run.parent_run_id)} className="text-primary hover:underline">
                      another run
                    </Link>
                  </p>
                )}
                {run?.order_id && (
                  <p>
                    <span className="text-muted-foreground">For </span>
                    <Link to={`/orders/${encodeURIComponent(run.order_id)}`} className="text-primary hover:underline">
                      an order
                    </Link>
                  </p>
                )}
                <div className="ml-auto">
                  <ForgetRun runId={id} status={view.data.status} />
                </div>
              </div>
              {!view.data.live && view.data.status !== 'starting' && (
                <p className="mb-3 text-[12.5px] text-muted-foreground">Live status unavailable; showing the last known state</p>
              )}
              {notice && (
                <p role="alert" className="mb-3 text-[13px] text-destructive">
                  {notice}
                </p>
              )}
              {run?.inputs && Object.keys(run.inputs).length > 0 && <Inputs inputs={run.inputs} />}
              {view.data.grant && <GrantPanel runId={id} grant={view.data.grant} onChange={() => view.refresh()} />}
              {view.data.pending.map((entry) => (
                <PendingCard key={entry.call_id} entry={entry} runId={id} step={steps.get(entry.call_id) ?? null} respond={respond} />
              ))}
              {children.map((entry) => (
                <ChildCard key={entry.id} entry={entry} respond={respond} />
              ))}
              {run && run.steps.length > 0 && <Steps steps={run.steps} />}
              {view.data.images && view.data.images.length > 0 && <Images runId={id} images={view.data.images} />}
              {run?.result && (
                <section aria-label="Result" className="mt-4">
                  <h2 className="mb-1 text-[13px] font-medium">Result</h2>
                  <pre className={`overflow-x-auto text-[12px] whitespace-pre-wrap ${INSET}`}>
                    {run.result}
                    {run.result_truncated && '\n…'}
                  </pre>
                </section>
              )}
              {run && <ResetPanel runId={id} steps={run.steps} onReset={() => view.refresh()} />}
              <RunScript definitionId={view.data.definition_id} />
            </>
          )}
        </div>
      </div>
    </TooltipProvider>
  )
}

/** #2483: the inputs the run started with, defaults filled in; a Reset replays the same. */
function Inputs({ inputs }: { inputs: Record<string, unknown> }) {
  return (
    <section aria-label="Inputs" className="mb-3">
      <h2 className="mb-1 text-[13px] font-medium">Inputs</h2>
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-[12.5px]">
        {Object.entries(inputs).map(([name, value]) => (
          <div key={name} className="contents">
            <dt className="font-mono text-muted-foreground">{name}</dt>
            <dd className="break-words font-mono">{value === null ? '—' : JSON.stringify(value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

/**
 * #2477: the images the run's tool calls returned, each a thumbnail of its blob route
 * (`GET /workflow-runs/{id}/images/{name}`), never inline data. A click opens it whole.
 */
function Images({ runId, images }: { runId: string; images: NonNullable<FlowRunView['images']> }) {
  return (
    <section aria-label="Images" className="mt-4">
      <h2 className="mb-1 text-[13px] font-medium">Images</h2>
      <ul className="flex flex-wrap gap-2">
        {images.map((image, i) => {
          const url = api.flowRunImageUrl(runId, image.name)
          const label = `Image ${i + 1} (${formatBytes(image.bytes)})`
          return (
            <li key={image.name}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <a href={url} target="_blank" rel="noreferrer" aria-label={label} className="block rounded-lg">
                    <img
                      src={url}
                      alt={label}
                      loading="lazy"
                      className="size-24 rounded-lg bg-muted object-contain ring-1 ring-foreground/10"
                    />
                  </a>
                </TooltipTrigger>
                <TooltipContent>{image.name}</TooltipContent>
              </Tooltip>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

/** The script of the version the run ran; folding it away stops reading it. */
function RunScript({ definitionId }: { definitionId: string }) {
  const [open, setOpen] = useState(true)
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mt-4">
      <CollapsibleTrigger className="flex items-center gap-1 rounded-md text-[13px] font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
        <ChevronRightIcon aria-hidden="true" className={`size-4 text-muted-foreground transition-transform ${open ? 'rotate-90' : ''}`} />
        Script
      </CollapsibleTrigger>
      <CollapsibleContent>
        <LoadedScript definitionId={definitionId} />
      </CollapsibleContent>
    </Collapsible>
  )
}

function LoadedScript({ definitionId }: { definitionId: string }) {
  const flow = useAsync(() => api.getFlow(definitionId), [definitionId])
  if (flow.error) {
    return (
      <p role="alert" className="mt-2 text-[13px] text-destructive">
        {flow.error.message}
      </p>
    )
  }
  if (!flow.data) return <Skeleton role="status" aria-label="Loading the script" className="mt-2 h-40 w-full rounded-xl" />
  return (
    <div className="mt-2">
      <FlowScript script={flow.data.script} />
    </div>
  )
}

function Steps({ steps }: { steps: FlowStep[] }) {
  return (
    <Card size="sm" className="px-3">
      <table aria-label="Steps" className="w-full text-left text-[12.5px]">
        <thead className="text-muted-foreground">
          <tr>
            <th className="py-1 pr-3 font-normal">#</th>
            <th className="py-1 pr-3 font-normal">Call</th>
            <th className="py-1 pr-3 font-normal">Status</th>
            <th className="py-1 pr-3 font-normal">Started</th>
            <th className="py-1 pr-3 font-normal">Duration</th>
          </tr>
        </thead>
        <tbody>
          {steps.map((step) => (
            <tr key={step.call_id} className="border-t">
              <td className="py-1 pr-3">{step.seq}</td>
              <td className="py-1 pr-3 font-mono">{step.fn}</td>
              <td className="py-1 pr-3">{step.error ? `${step.status} (${step.error})` : step.status}</td>
              <td className="py-1 pr-3">{new Date(step.started_at).toLocaleString()}</td>
              <td className="py-1 pr-3">
                {step.ended_at
                  ? formatDuration((new Date(step.ended_at).getTime() - new Date(step.started_at).getTime()) / 1000)
                  : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}
