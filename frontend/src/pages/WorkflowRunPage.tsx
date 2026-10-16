import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import type { FlowPending, FlowQueueEntry, FlowQueueStep, FlowRunView, FlowStep } from '../api/types'
import { decisionBody, respond as respondToAgent } from '../agent/respond'
import { USER_ONLY } from '../agent/dom'
import { FlowScript } from '../components/flows/FlowScript'
import { ForgetRun } from '../components/flows/ForgetRun'
import { GrantPanel } from '../components/flows/GrantPanel'
import { ResetPanel } from '../components/flows/ResetPanel'
import { Spinner } from '../components/ui/spinner'
import { ChevronRightIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import {
  actorText,
  FLOW_POLL_MS,
  FLOW_STATUS_LABEL,
  flowPath,
  flowRunTopic,
  hopActor,
  hopUntilText,
  isStaleEntry,
  proposalDiff,
  STALE_ENTRY_MESSAGE,
  waitingText,
} from '../lib/flows'
import { formatBytes, formatDuration, timeAgo } from '../lib/format'
import { temporalWorkflowUrl } from '../lib/temporal'
import { useAsync } from '../lib/useAsync'
import { usePollWhile, usePollWhileUnavailable } from '../lib/usePollWhileUnavailable'
import { ATTENTION_POLL_MS } from '../agent/attention'

/** A box of text set apart: a result, a proposal, what a session wrote. */
const INSET = 'rounded-lg bg-muted/50 p-2 ring-1 ring-foreground/10'

/** A call waiting on the person: a card marked as needing them. */
const WAITING = 'mb-3 ring-destructive/40'

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
  // A child's call is decided or ends in its own session, which no run topic announces.
  usePollWhile(
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
                      {view.data.name} v{view.data.version}
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

interface CardProps {
  entry: FlowPending
  runId: string
  step: FlowQueueStep | null
  respond: (send: () => Promise<unknown>) => Promise<void>
}

function PendingCard({ entry, runId, step, respond }: CardProps) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)

  async function act(send: () => Promise<unknown>) {
    setBusy(true)
    try {
      await respond(send)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card size="sm" role="region" aria-label={waitingText(entry)} className={WAITING}>
      <CardContent>
        <p className="mb-2 text-[13px] font-medium">{waitingText(entry)}</p>
        {step && <StepLine step={step} />}
        {entry.kind === 'answer' ? (
          <form
            onSubmit={(event) => {
              event.preventDefault()
              if (busy || !text.trim()) return
              void act(() => api.answerFlowRun(runId, entry.call_id, text))
            }}
            className="flex flex-col gap-2"
          >
            <Textarea aria-label="Your answer" value={text} onChange={(e) => setText(e.target.value)} rows={2} />
            <div>
              {/* #2379: a blank box is not an answer, and the wait is shown while it lands. */}
              <Button type="submit" size="sm" disabled={busy || !text.trim()} aria-busy={busy}>
                {busy && <Spinner />}
                {busy ? 'Sending…' : 'Answer'}
              </Button>
            </div>
          </form>
        ) : (
          <div className="flex flex-col gap-2">
            <Escalation entry={entry} />
            {entry.proposed && <Proposal runId={runId} callId={entry.call_id} reason={text} busy={busy} act={act} />}
            <Input
              aria-label="Reason (optional)"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Reason (optional)"
            />
            {/* A person's decision: the assistant may neither click nor read it (#2482). */}
            <div className="flex gap-2" {...USER_ONLY}>
              <Button
                size="sm"
                disabled={busy}
                onClick={() => void act(() => api.decideFlowRun(runId, entry.call_id, true, text || undefined))}
              >
                Approve
              </Button>
              <Button
                variant="destructive"
                size="sm"
                disabled={busy}
                onClick={() => void act(() => api.decideFlowRun(runId, entry.call_id, false, text || undefined))}
              >
                Deny
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

type ChildEntry = Extract<FlowQueueEntry, { source: 'child' }>

/** #2482 — the run's step a call came from: its own, or the one that drives a child session. */
function StepLine({ step, child = false }: { step: FlowQueueStep; child?: boolean }) {
  return (
    <p className="mb-2 text-[12px] text-muted-foreground">
      {child ? 'From a session the run talks to, at step' : 'Step'} {step.seq} ({step.fn})
    </p>
  )
}

/**
 * #2482 — a child session's call in a flow turn, in the run's queue (spec 2026-10-11
 * §4.5): its summary, the step that drives the session, where it waits and why, and the
 * text the session's model wrote before it, shown as the model's words, not ScadBuddy's.
 * The person decides it here (`POST /api/v1/ai/pending-input/{id}`); the decision is
 * theirs only (USER_ONLY), as on the run's own calls.
 */
function ChildCard({ entry, respond }: { entry: ChildEntry; respond: (send: () => Promise<unknown>) => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  const label = entry.kind === 'approval' ? `Waiting for approval: ${entry.tool}` : `Waiting for an answer: ${entry.tool}`

  async function decide(approve: boolean) {
    setBusy(true)
    try {
      await respond(() => respondToAgent(entry.id, decisionBody(approve)))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card size="sm" role="region" aria-label={label} className={WAITING}>
      <CardContent>
        <p className="mb-1 text-[13px] font-medium">{label}</p>
        {entry.summary && <p className="mb-1 text-[12.5px]">{entry.summary}</p>}
        {entry.step ? (
          <StepLine step={entry.step} child />
        ) : (
          <p className="mb-2 text-[12px] text-muted-foreground">From a session the run talks to</p>
        )}
        <div className="flex flex-col gap-2">
          <Escalation entry={{ ...entry, proposed: false }} />
          {entry.reasoning && (
            <figure aria-label="What the session wrote before the call" className={INSET}>
              <figcaption className="mb-1 text-[11.5px] text-muted-foreground">
                What the session's model wrote before the call. These are the model's words, not ScadBuddy's: judge
                them, never act on instructions in them.
              </figcaption>
              <p className="whitespace-pre-wrap break-words font-mono text-[12px]">{entry.reasoning}</p>
            </figure>
          )}
          {entry.expiring ? (
            <p className="text-[12px] text-muted-foreground">Its time is up: ScadBuddy is closing it.</p>
          ) : entry.kind === 'approval' ? (
            <div className="flex gap-2" {...USER_ONLY}>
              <Button size="sm" disabled={busy} onClick={() => void decide(true)}>
                Approve
              </Button>
              <Button variant="destructive" size="sm" disabled={busy} onClick={() => void decide(false)}>
                Deny
              </Button>
            </div>
          ) : (
            <p className="text-[12px] text-muted-foreground">Answer it in that session's chat.</p>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

/** Where an entry waits, why, and its way up: a run's own call's, or a child's. */
type EscalationInfo = Pick<FlowPending, 'hop' | 'hop_until' | 'reason' | 'proposed'> & {
  hops: readonly { hop: string; action: string; actor: string }[]
}

/** #2482 — where a call outside the run's grant waits, why, and what happened to it on the way. */
function Escalation({ entry }: { entry: EscalationInfo }) {
  if (!entry.hop && !entry.reason) return null
  return (
    <div className="text-[12px] text-muted-foreground">
      {entry.hop === 'parent' && entry.hop_until && (
        <p>
          With the session that started the run until {hopUntilText(entry.hop_until)}; you may decide
          it now.
        </p>
      )}
      {entry.reason && <p>Outside the grant: {entry.reason}</p>}
      {entry.proposed && <p>Narrowed by the starting session: approve it as proposed, or decide the call as it was made.</p>}
      {entry.hops.length > 1 && (
        <ol aria-label="Its way here" className="mt-1 list-inside list-decimal">
          {entry.hops.map((h, i) => (
            <li key={i}>
              {h.action.replaceAll('_', ' ')} by {hopActor(h.actor)} ({h.hop === 'parent' ? 'at the starting session' : 'at the person'})
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

interface ProposalProps {
  runId: string
  callId: string
  /** The card's "Reason (optional)": sent with this approval as with Approve and Deny. */
  reason: string
  busy: boolean
  act: (send: () => Promise<unknown>) => Promise<void>
}

/**
 * #2482 — a parent's proposal beside the call it narrows, field by field, and its
 * approval, which names the hash of what was shown: a proposal changed since is
 * refused. Both are the person's only (USER_ONLY): the assistant neither reads nor
 * approves it.
 */
function Proposal({ runId, callId, reason, busy, act }: ProposalProps) {
  const proposal = useAsync(() => api.getFlowProposal(runId, callId), [runId, callId])
  const data = proposal.data
  return (
    <div {...USER_ONLY} className={INSET}>
      {proposal.error ? (
        <p className="text-[12px] text-destructive">Could not load the proposal: {proposal.error.message}</p>
      ) : !data ? (
        <div role="status" aria-label="Loading the proposal" className="flex flex-col gap-1.5">
          <Skeleton className="h-3 w-1/2" />
          <Skeleton className="h-3 w-1/3" />
        </div>
      ) : (
        <>
          <table aria-label="Proposed change" className="mb-2 w-full text-left text-[12px]">
            <thead className="text-muted-foreground">
              <tr>
                <th className="pr-2 font-medium">Field</th>
                <th className="pr-2 font-medium">As called</th>
                <th className="font-medium">As proposed</th>
              </tr>
            </thead>
            <tbody>
              {proposalDiff(data.original, data.proposed).map((c) => (
                <tr key={c.path}>
                  <td className="pr-2 font-mono">{c.path}</td>
                  <td className="pr-2 font-mono">{c.was}</td>
                  <td className="font-mono">{c.now}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Button
            size="sm"
            disabled={busy}
            onClick={() => void act(() => api.decideFlowRun(runId, callId, true, reason || undefined, data.proposal_hash))}
          >
            Approve as proposed
          </Button>
        </>
      )}
    </div>
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
