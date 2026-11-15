import { useContext } from 'react'
import { Link } from 'react-router'
import { ATTENTION_POLL_MS, RunsWaitingContext } from '../../agent/attention'
import { api } from '../../api/client'
import { FLOWS_TOPIC, flowRunPath } from '../../lib/flows'
import { useAsync } from '../../lib/useAsync'
import { usePollWhileUnavailable } from '../../lib/usePollWhileUnavailable'

const TOPICS = [FLOWS_TOPIC]
const NONE: string[] = []

/**
 * #2482 — the flow runs started from the browser whose own calls wait for you, each
 * linking to its page, where they are decided. The header's badge counts them, and they
 * are in no session's list, so the panel it opens shows them here. Read from the badge's
 * own poll (`RunsWaitingContext`), never a second one. Nothing when none waits.
 * #3186 — each by its flow's display name, from the flows list (the badge's poll is the
 * agent's, which has no display names), its name until then. #265: while a run waits,
 * the list is read again on each change on the `flows` topic, so a rename shows without
 * a remount. #6158: while the realtime socket is unavailable, it is read every
 * `ATTENTION_POLL_MS` instead.
 */
export function RunsWaiting() {
  const waiting = useContext(RunsWaitingContext) ?? []
  const any = waiting.length > 0
  const flows = useAsync(() => (any ? api.listFlows({ archived: true }) : Promise.resolve([])), [any], any ? TOPICS : NONE)
  usePollWhileUnavailable(() => {
    if (any) flows.refresh()
  }, ATTENTION_POLL_MS)
  const titles = new Map((flows.data ?? []).flatMap((f) => (f.display_name ? [[f.name, f.display_name] as const] : [])))
  if (!any) return null
  return (
    <div className="shrink-0 border-b border-line px-3 py-1.5 text-[12.5px]">
      <ul aria-label="Flow runs waiting for you" className="flex flex-col gap-1">
        {waiting.map((run) => (
          <li key={run.id}>
            <Link to={flowRunPath(run.id)} className="text-primary hover:underline">
              {titles.get(run.name) ?? run.name}
            </Link>{' '}
            waits for you on {run.waiting === 1 ? 'a call' : `${run.waiting} calls`}
          </li>
        ))}
      </ul>
    </div>
  )
}
