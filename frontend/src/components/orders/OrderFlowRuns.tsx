import { useState } from 'react'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { FlowRun } from '../../api/types'
import { PendingCard } from '../flows/PendingCard'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import {
  FLOW_RUNS_TOPIC,
  FLOW_STATUS_LABEL,
  flowRunPath,
  flowRunTopic,
  flowTitle,
  isStaleEntry,
  STALE_ENTRY_MESSAGE,
} from '../../lib/flows'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'

/**
 * #4252 (orders spec §7.4) — the flow runs an order is for (`GET /workflow-runs?order_id=`),
 * and each run's parked calls with the decide controls its own page has.
 */
/** The most `GET /workflow-runs` lists at once (`api/flows.py`). */
export const ORDER_RUNS_LIMIT = 200

export function OrderFlowRuns({ orderId }: { orderId: string }) {
  const runs = useAsync(
    () => api.listFlowRuns({ order_id: orderId, limit: ORDER_RUNS_LIMIT }),
    [orderId],
    [FLOW_RUNS_TOPIC],
  )
  if (runs.error) {
    return (
      <p role="alert" className="text-[13px] text-destructive">
        The order's flow runs could not be read: {runs.error.message}
      </p>
    )
  }
  if (!runs.data) return <Skeleton className="h-5 w-48" />
  if (runs.data.length === 0) return null
  return (
    <section aria-label="Flow runs" className="flex flex-col gap-2">
      <h2 className="text-sm font-medium">Flow runs</h2>
      <ul className="flex flex-col gap-2">
        {runs.data.map((run) => (
          <OrderRun key={run.id} run={run} onChanged={() => runs.refresh()} />
        ))}
      </ul>
      {runs.data.length >= ORDER_RUNS_LIMIT && (
        // Never hide older runs without a sign (review of #5668).
        <p className="text-[12.5px] text-muted-foreground">
          The newest {ORDER_RUNS_LIMIT} runs are shown; older ones are on the{' '}
          <Link to="/workflows" className="text-primary hover:underline">
            Flows
          </Link>{' '}
          page.
        </p>
      )}
    </section>
  )
}

function OrderRun({ run, onChanged }: { run: FlowRun; onChanged: () => void }) {
  const waiting = run.waiting_on.length > 0
  const view = useAsync(() => (waiting ? api.getFlowRun(run.id) : Promise.resolve(null)), [run.id, waiting], [flowRunTopic(run.id)])
  const [notice, setNotice] = useState<string>()

  async function respond(send: () => Promise<unknown>) {
    setNotice(undefined)
    try {
      await send()
    } catch (error) {
      setNotice(isStaleEntry(error) ? STALE_ENTRY_MESSAGE : error instanceof Error ? error.message : String(error))
    }
    view.refresh()
    onChanged()
  }

  return (
    <li data-run={run.id} className="flex flex-col gap-1">
      <div className="flex items-center gap-2 text-[13px]">
        <Link to={flowRunPath(run.id)} className="font-medium hover:underline">
          {flowTitle(run)} v{run.version}
        </Link>
        <Badge variant="outline">{FLOW_STATUS_LABEL[run.status]}</Badge>
        <span className="ml-auto text-[12px] text-muted-foreground">{timeAgo(run.created_at)}</span>
      </div>
      {notice && (
        <p role="alert" className="text-[13px] text-destructive">
          {notice}
        </p>
      )}
      {view.data?.pending.map((entry) => (
        <PendingCard key={entry.call_id} entry={entry} runId={run.id} step={null} respond={respond} />
      ))}
    </li>
  )
}
