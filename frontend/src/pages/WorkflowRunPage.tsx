import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import type { FlowPending, FlowRunView, FlowStep } from '../api/types'
import { USER_ONLY } from '../agent/dom'
import { FlowScript } from '../components/flows/FlowScript'
import { ForgetRun } from '../components/flows/ForgetRun'
import { GrantPanel } from '../components/flows/GrantPanel'
import { ResetPanel } from '../components/flows/ResetPanel'
import { Button } from '../components/ui/Button'
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
import { formatBytes, formatDuration } from '../lib/format'
import { temporalWorkflowUrl } from '../lib/temporal'
import { useAsync } from '../lib/useAsync'
import { usePollWhileUnavailable } from '../lib/usePollWhileUnavailable'

const FIELD = 'w-full rounded-[6px] border border-line bg-bg px-2 py-1.5 text-[13px]'

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
  const settings = useAsync(() => api.getSettings(), []).data
  usePollWhileUnavailable(() => view.refresh(), FLOW_POLL_MS)
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
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5 flex items-baseline gap-2">
          <Link to="/workflows" className="text-[12px] text-muted hover:text-ink">
            Workflows
          </Link>
          <span className="text-faint">/</span>
          <h1 className="text-[13px] font-medium">
            {view.data ? (
              <Link to={flowPath(view.data.definition_id)} className="hover:underline" title="The flow version it ran">
                {view.data.name} v{view.data.version}
              </Link>
            ) : (
              id
            )}
          </h1>
          {temporal && (
            <a href={temporal} target="_blank" rel="noreferrer" className="ml-auto text-[12px] text-accent hover:underline">
              Temporal UI ↗
            </a>
          )}
        </div>

        {view.error ? (
          <p role="alert" className="text-[13px] text-warn">
            {view.error instanceof ApiError && view.error.status === 404 ? 'No such flow run.' : view.error.message}
          </p>
        ) : !view.data ? (
          <p className="text-[13px] text-muted">Loading…</p>
        ) : (
          <>
            <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[13px]">
              <p>
                <span className="text-muted">Status: </span>
                <span data-testid="flow-run-status">{FLOW_STATUS_LABEL[view.data.status]}</span>
              </p>
              {run && (
                <p>
                  <span className="text-muted">Started by </span>
                  {actorText(run.started_by)}
                </p>
              )}
              <div className="ml-auto">
                <ForgetRun runId={id} status={view.data.status} />
              </div>
            </div>
            {!view.data.live && view.data.status !== 'starting' && (
              <p className="mb-3 text-[12.5px] text-muted">Live status unavailable; showing the last known state</p>
            )}
            {notice && (
              <p role="alert" className="mb-3 text-[13px] text-warn">
                {notice}
              </p>
            )}
            {run?.inputs && Object.keys(run.inputs).length > 0 && <Inputs inputs={run.inputs} />}
            {view.data.grant && <GrantPanel runId={id} grant={view.data.grant} onChange={() => view.refresh()} />}
            {view.data.pending.map((entry) => (
              <PendingCard key={entry.call_id} entry={entry} runId={id} respond={respond} />
            ))}
            {run && run.steps.length > 0 && <Steps steps={run.steps} />}
            {view.data.images && view.data.images.length > 0 && <Images runId={id} images={view.data.images} />}
            {run?.result && (
              <section aria-label="Result" className="mt-4">
                <h2 className="mb-1 text-[13px] font-medium">Result</h2>
                <pre className="overflow-x-auto rounded-[6px] border border-line bg-surface-2 p-2 text-[12px] whitespace-pre-wrap">
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
            <dt className="font-mono text-muted">{name}</dt>
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
              <a href={url} target="_blank" rel="noreferrer" aria-label={label} title={image.name}>
                <img
                  src={url}
                  alt={label}
                  loading="lazy"
                  className="h-24 w-24 rounded-[6px] border border-line bg-surface-2 object-contain"
                />
              </a>
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
  respond: (send: () => Promise<unknown>) => Promise<void>
}

function PendingCard({ entry, runId, respond }: CardProps) {
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
    <section
      aria-label={waitingText(entry)}
      className="mb-3 rounded-[8px] border border-warn/40 bg-surface-2 p-3"
    >
      <p className="mb-2 text-[13px]">{waitingText(entry)}</p>
      {entry.kind === 'answer' ? (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            void act(() => api.answerFlowRun(runId, entry.call_id, text))
          }}
          className="flex flex-col gap-2"
        >
          <textarea aria-label="Your answer" value={text} onChange={(e) => setText(e.target.value)} className={FIELD} rows={2} />
          <div>
            <Button type="submit" variant="primary" size="sm" disabled={busy}>
              Answer
            </Button>
          </div>
        </form>
      ) : (
        <div className="flex flex-col gap-2">
          <Escalation entry={entry} />
          {entry.proposed && <Proposal runId={runId} callId={entry.call_id} reason={text} busy={busy} act={act} />}
          <input
            aria-label="Reason (optional)"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Reason (optional)"
            className={FIELD}
          />
          {/* A person's decision: the assistant may neither click nor read it (#2482). */}
          <div className="flex gap-2" {...USER_ONLY}>
            <Button
              variant="primary"
              size="sm"
              disabled={busy}
              onClick={() => void act(() => api.decideFlowRun(runId, entry.call_id, true, text || undefined))}
            >
              Approve
            </Button>
            <Button
              variant="danger"
              size="sm"
              disabled={busy}
              onClick={() => void act(() => api.decideFlowRun(runId, entry.call_id, false, text || undefined))}
            >
              Deny
            </Button>
          </div>
        </div>
      )}
    </section>
  )
}

/** #2482 — where a call outside the run's grant waits, why, and what happened to it on the way. */
function Escalation({ entry }: { entry: FlowPending }) {
  if (!entry.hop && !entry.reason) return null
  return (
    <div className="text-[12px] text-muted">
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
    <div {...USER_ONLY} className="rounded-[6px] border border-line bg-bg p-2">
      {proposal.error ? (
        <p className="text-[12px] text-danger">Could not load the proposal: {proposal.error.message}</p>
      ) : !data ? (
        <p className="text-[12px] text-muted">Loading the proposal…</p>
      ) : (
        <>
          <table aria-label="Proposed change" className="mb-2 w-full text-left text-[12px]">
            <thead>
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
            variant="primary"
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
    <details open={open} className="mt-4" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary className="cursor-pointer text-[13px] font-medium">Script</summary>
      {open && <LoadedScript definitionId={definitionId} />}
    </details>
  )
}

function LoadedScript({ definitionId }: { definitionId: string }) {
  const flow = useAsync(() => api.getFlow(definitionId), [definitionId])
  if (flow.error) {
    return (
      <p role="alert" className="mt-2 text-[13px] text-warn">
        {flow.error.message}
      </p>
    )
  }
  if (!flow.data) return <p className="mt-2 text-[13px] text-muted">Loading…</p>
  return (
    <div className="mt-2">
      <FlowScript script={flow.data.script} />
    </div>
  )
}

function Steps({ steps }: { steps: FlowStep[] }) {
  return (
    <table aria-label="Steps" className="w-full text-left text-[12.5px]">
      <thead className="text-muted">
        <tr>
          <th className="py-1 font-normal">#</th>
          <th className="py-1 font-normal">Call</th>
          <th className="py-1 font-normal">Status</th>
          <th className="py-1 font-normal">Started</th>
          <th className="py-1 font-normal">Duration</th>
        </tr>
      </thead>
      <tbody>
        {steps.map((step) => (
          <tr key={step.call_id} className="border-t border-line">
            <td className="py-1">{step.seq}</td>
            <td className="py-1 font-mono">{step.fn}</td>
            <td className="py-1">{step.error ? `${step.status} (${step.error})` : step.status}</td>
            <td className="py-1">{new Date(step.started_at).toLocaleString()}</td>
            <td className="py-1">
              {step.ended_at
                ? formatDuration((new Date(step.ended_at).getTime() - new Date(step.started_at).getTime()) / 1000)
                : '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
