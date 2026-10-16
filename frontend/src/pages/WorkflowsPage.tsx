import { useId, useState } from 'react'
import { Link, NavLink, useSearchParams } from 'react-router'
import { api } from '../api/client'
import type { FlowRun, FlowSummary, FlowTrigger } from '../api/types'
import { FlowActions } from '../components/flows/FlowActions'
import { StartRun } from '../components/flows/StartRun'
import { TriggerList } from '../components/flows/Triggers'
import { timeAgo } from '../lib/format'
import {
  actorText,
  FLOW_POLL_MS,
  FLOW_RUNS_TOPIC,
  FLOW_STATUS_LABEL,
  flowPath,
  flowRunPath,
  FLOWS_PATH,
  flowStateText,
  waitingText,
} from '../lib/flows'
import { useAsync } from '../lib/useAsync'
import { usePollWhileUnavailable } from '../lib/usePollWhileUnavailable'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { tabsListVariants } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'

const TOPICS = [FLOW_RUNS_TOPIC]

/** shadcn's tabs, as links: each tab is a page of its own (`/workflows`, `/workflows/flows`). */
const tabClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    'inline-flex h-full items-center justify-center rounded-md border border-transparent px-3 text-sm font-medium whitespace-nowrap transition-all focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none',
    isActive
      ? 'bg-background text-foreground shadow-sm dark:border-input dark:bg-input/30'
      : 'text-foreground/60 hover:text-foreground dark:text-muted-foreground dark:hover:text-foreground',
  )

/** The lists' rows as one card, divided. */
const LIST = 'divide-y overflow-hidden rounded-xl bg-card text-card-foreground ring-1 ring-foreground/10'

function Loading({ label }: { label: string }) {
  return (
    <div role="status" aria-label={label} className={LIST}>
      {[0, 1, 2].map((row) => (
        <div key={row} className="flex flex-col gap-2 px-3 py-3">
          <Skeleton className="h-3.5 w-1/3" />
          <Skeleton className="h-3 w-1/2" />
        </div>
      ))}
    </div>
  )
}

/**
 * #1057 — every flow run (`/workflows`, plan 2026-10-09 Task F1), newest first, with what
 * each waits on. `?session=` keeps the runs one assistant session started (F2). Answers
 * and approvals are given on a run's own page. #2350 — the Flows tab
 * (`/workflows/flows`) lists every registered flow. #2485 — each can be archived or
 * deleted there; Show archived lists those too, deleted ones with Restore. #2484 — each
 * flow's triggers under it: when one fires next, its last run, and pause or resume.
 */
export function WorkflowsPage({ tab = 'runs' }: { tab?: 'runs' | 'flows' }) {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-4">
          <h1 className="text-lg font-semibold tracking-tight">Workflows</h1>
          <p className="mt-0.5 text-[13px] text-muted-foreground">
            {tab === 'runs'
              ? 'Runs of the flows an assistant registered: each step, and what it waits on you for.'
              : 'The flows registered here: each one’s versions and script, and a run started from one.'}
          </p>
        </div>
        <nav aria-label="Workflows" className={cn(tabsListVariants(), 'mb-4 h-9')}>
          <NavLink to="/workflows" end className={tabClass}>
            Runs
          </NavLink>
          <NavLink to={FLOWS_PATH} className={tabClass}>
            Flows
          </NavLink>
        </nav>
        {tab === 'runs' ? <Runs /> : <Flows />}
      </div>
    </div>
  )
}

function Runs() {
  const [params] = useSearchParams()
  const session = params.get('session') ?? undefined
  const runs = useAsync(() => api.listFlowRuns({ session }), [session], TOPICS)
  usePollWhileUnavailable(() => runs.refresh(), FLOW_POLL_MS)

  return (
    <>
      {session && (
        <p className="mb-4 text-[13px]">
          Started by assistant session <code className="font-mono text-[12px]">{session}</code>.{' '}
          <Link to="/workflows" className="text-primary hover:underline">
            Show all runs
          </Link>
        </p>
      )}
      {runs.error ? (
        <p role="alert" className="text-[13px] text-destructive">
          {runs.error.message}
        </p>
      ) : runs.loading && !runs.data ? (
        <Loading label="Loading the runs" />
      ) : runs.data && runs.data.length > 0 ? (
        <ul aria-label="Flow runs" className={LIST}>
          {runs.data.map((run) => (
            <RunRow key={run.id} run={run} />
          ))}
        </ul>
      ) : (
        <Card className="items-center px-4 text-[13px] text-muted-foreground">No flow runs yet.</Card>
      )}
    </>
  )
}

function Flows() {
  const [archived, setArchived] = useState(false)
  const flows = useAsync(() => api.listFlows({ archived }), [archived])
  // One read of every trigger, shown under its flow.
  const triggers = useAsync(() => api.listFlowTriggers(), [])
  const switchId = useId()
  const toggle = (
    <div className="mb-3 flex w-fit items-center gap-2">
      <Switch id={switchId} checked={archived} onCheckedChange={setArchived} />
      <Label htmlFor={switchId} className="font-normal text-muted-foreground">
        Show archived
      </Label>
    </div>
  )
  if (flows.error) {
    return (
      <>
        {toggle}
        <p role="alert" className="text-[13px] text-destructive">
          {flows.error.message}
        </p>
      </>
    )
  }
  if (!flows.data) {
    return (
      <>
        {toggle}
        <Loading label="Loading the flows" />
      </>
    )
  }
  // `GET /workflows` is each name's newest version, by name.
  return (
    <>
      {toggle}
      {flows.data.length === 0 ? (
        <Card className="items-center px-4 text-center text-[13px] text-muted-foreground">
          {archived
            ? 'No flows registered yet. An assistant registers one with register_flow.'
            : 'No flows registered yet, or every one is archived. An assistant registers one with register_flow.'}
        </Card>
      ) : (
        <ul aria-label="Flows" className={LIST}>
          {flows.data.map((latest) => (
            <FlowRow
              key={latest.name}
              latest={latest}
              triggers={(triggers.data ?? []).filter((t) => t.name === latest.name)}
              onChanged={() => {
                flows.refresh()
                triggers.refresh()
              }}
            />
          ))}
        </ul>
      )}
    </>
  )
}

function FlowRow({
  latest,
  triggers,
  onChanged,
}: {
  latest: FlowSummary
  triggers: FlowTrigger[]
  onChanged: () => void
}) {
  const state = flowStateText(latest)
  return (
    <li data-flow={latest.name} className="flex flex-col gap-1.5 px-3 py-2.5 sm:flex-row sm:items-center sm:gap-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <Link to={flowPath(latest.id)} className="text-[13px] font-medium hover:underline">
            {latest.name}
          </Link>
          <Badge variant="secondary">latest v{latest.version}</Badge>
          {state && <Badge variant="outline">{latest.deleted_at ? 'Deleted' : 'Archived'}</Badge>}
          <span className="ml-auto text-[12px] text-muted-foreground">{timeAgo(latest.created_at)}</span>
        </div>
        <p className="mt-0.5 text-[12.5px] text-muted-foreground">
          Registered by {actorText(latest.created_by)}
          {state && `. ${state}, ${timeAgo((latest.deleted_at ?? latest.archived_at)!)}`}
        </p>
        {triggers.length > 0 && (
          <div className="mt-1.5">
            <TriggerList triggers={triggers} onChanged={onChanged} compact />
          </div>
        )}
      </div>
      <div className="flex flex-wrap items-start gap-2">
        {/* #2483: a flow's inputs are asked for before it starts. */}
        {!state && <StartRun flowId={latest.id} size="sm" variant="outline" />}
        <FlowActions
          flow={latest}
          onChanged={onChanged}
          onRestored={onChanged}
          restore={latest.commit ? { commit: latest.commit, version: latest.version } : null}
        />
      </div>
    </li>
  )
}

function RunRow({ run }: { run: FlowRun }) {
  return (
    <li data-run={run.id} className="px-3 py-2.5">
      <div className="flex items-center gap-2">
        <Link to={flowRunPath(run.id)} className="text-[13px] font-medium hover:underline">
          {run.name} v{run.version}
        </Link>
        <Badge variant="outline">{FLOW_STATUS_LABEL[run.status]}</Badge>
        <span className="ml-auto text-[12px] text-muted-foreground">{timeAgo(run.created_at)}</span>
      </div>
      {run.waiting_on.map((entry) => (
        <p key={entry.call_id} className="mt-1 text-[12.5px] text-destructive">
          {waitingText(entry)}
        </p>
      ))}
    </li>
  )
}
