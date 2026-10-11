import { Link } from 'react-router'
import { api } from '../../api/client'
import { FLOW_RUNS_TOPIC } from '../../lib/flows'
import { useAsync } from '../../lib/useAsync'

const TOPICS = [FLOW_RUNS_TOPIC]

/**
 * #1057 — the flow runs this session started (spec 2026-10-01 §7.3, plan Task F2), as a
 * link to the Workflows page filtered to them. Nothing when it started none, or when
 * they cannot be read.
 */
export function SessionFlowRuns({ sessionId }: { sessionId: string }) {
  const runs = useAsync(() => api.listFlowRuns({ session: sessionId }), [sessionId], TOPICS)
  const count = runs.data?.length ?? 0
  if (count === 0) return null
  return (
    <div className="shrink-0 border-b border-line px-3 py-1.5 text-[12.5px]">
      <Link to={`/workflows?session=${encodeURIComponent(sessionId)}`} className="text-accent hover:underline">
        Flow runs started here ({count})
      </Link>
    </div>
  )
}
