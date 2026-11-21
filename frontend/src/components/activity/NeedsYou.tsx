import { Suspense, lazy, useContext, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router'
import { api, ApiError } from '../../api/client'
import { USER_ONLY } from '../../agent/dom'
import { notTakenText } from '../../agent/pendingApprovals'
import { decisionBody, respond, RespondError } from '../../agent/respond'
import { noticeTitle } from '../../agent/attentionNotifications'
import { actorText, flowRunPath } from '../../lib/flows'
import { timeAgo } from '../../lib/format'
import { NEEDS_YOU_ID, NeedsYouContext, REVIEW_PARAM, reviewPath, runName, type NeedsYouItem } from '../../lib/needsYou'
import { traceAction } from '../../lib/traceAction'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { Spinner } from '../ui/spinner'

// The bell is in every page's shell: a grant's full review (its rules, the editor) loads
// only when a person opens one.
const GrantPanel = lazy(async () => ({ default: (await import('../flows/GrantPanel')).GrantPanel }))
const TriggerList = lazy(async () => ({ default: (await import('../flows/Triggers')).TriggerList }))

/**
 * #6399 — what waits on the person, of every kind (`lib/needsYou.ts`), each with its kind, where
 * it comes from, how long it has waited, and its answer: Approve and Deny through the one respond
 * route, a grant's review and decline through the flow routes, else a link to where it is
 * answered (the session in the panel, the run's page, the tab's pairing prompt). The buttons
 * that decide are user-only (#254): the assistant can neither press nor read them.
 *
 * `compact` is the bell's list: a grant's full review is a link, to the run's page or here.
 */

/** The kind, as the row's label says it. */
function kindLabel(item: NeedsYouItem): string {
  switch (item.kind) {
    case 'run-grant':
      return 'Grant request'
    case 'trigger-grant':
      return 'Standing grant request'
    case 'pairing':
      return 'Pairing request'
    case 'agent': {
      const { entry } = item
      if (entry.run) return entry.kind === 'approval' ? 'Flow run approval' : 'Flow run question'
      if (entry.kind === 'approval') return entry.sessionId ? 'Approval' : 'Prepared action'
      if (entry.kind === 'question') return 'Question'
      return noticeTitle(entry.reason ?? '').replace(/^The assistant /, 'Assistant: ')
    }
  }
}

/** Where it comes from: the session, flow run, trigger or client that asks. */
function sourceText(item: NeedsYouItem): string {
  switch (item.kind) {
    case 'run-grant':
      return `Flow run ${runName(item.request)}, asked by ${actorText(item.request.grant.requested_by)}`
    case 'trigger-grant':
      return `A trigger of ${item.trigger.name}${item.trigger.grant ? `, asked by ${actorText(item.trigger.grant.requested_by)}` : ''}`
    case 'pairing':
      return `${item.pairing.label}, from outside ScadBuddy`
    case 'agent': {
      const { entry } = item
      if (entry.run) return `Flow run ${entry.run.name}${entry.tool ? `, ${entry.tool}` : ''}`
      if (!entry.sessionId) return entry.by ? `${entry.tool}, asked by ${entry.by}` : entry.tool || 'An MCP client'
      return entry.kind === 'approval' && entry.tool ? `${entry.tool} in an assistant session` : 'An assistant session'
    }
  }
}

/** What it asks. */
function askText(item: NeedsYouItem): string {
  switch (item.kind) {
    case 'agent':
      return item.entry.text
    case 'run-grant':
      return 'Outward calls inside the grant would run without asking you.'
    case 'trigger-grant':
      return 'Each run it starts would make the calls inside the grant without asking you, until it expires.'
    case 'pairing':
      return 'It could see this tab and change it as you can. Type the code it gave you in the pairing prompt on this page.'
  }
}

function waitedText(item: NeedsYouItem, now: number): string {
  if (item.since) return `asked ${timeAgo(item.since, now)}`
  if (item.kind === 'pairing') {
    const at = new Date(item.pairing.expiresAt)
    if (!Number.isNaN(at.getTime())) return `ends at ${at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
  }
  return ''
}

type RowState = { kind: 'idle'; error?: string } | { kind: 'busy' } | { kind: 'done'; text: string }

export function NeedsYouRow({
  item,
  compact = false,
  now,
  onLeave,
  open = false,
}: {
  item: NeedsYouItem
  compact?: boolean
  now: number
  /** A link in the row was followed (the bell closes). */
  onLeave?: () => void
  /** Its review starts open (`REVIEW_PARAM` names it). */
  open?: boolean
}) {
  const ctx = useContext(NeedsYouContext)
  const [state, setState] = useState<RowState>({ kind: 'idle' })
  const [reviewing, setReviewing] = useState(open)
  // The bell's "Review" on this page changes `REVIEW_PARAM` under a row already shown.
  useEffect(() => {
    if (open) setReviewing(true)
  }, [open])
  const refresh = () => ctx?.refresh()
  const headingId = `needs-you-${compact ? 'bell-' : ''}${item.key}`

  const run = (send: () => Promise<unknown>, done: string) => {
    setState({ kind: 'busy' })
    send().then(
      () => {
        setState({ kind: 'done', text: done })
        refresh()
      },
      (err: unknown) => {
        if (err instanceof RespondError && err.settled) {
          setState({ kind: 'done', text: notTakenText(err) })
          refresh()
          return
        }
        setState({ kind: 'idle', error: err instanceof ApiError ? err.detail : notTakenText(err) })
        // A grant decided elsewhere meanwhile: read again, so the list says what is true now.
        if (err instanceof ApiError && err.status === 409) refresh()
      },
    )
  }
  const busy = state.kind === 'busy'

  const actions = (() => {
    if (state.kind === 'done') return null
    if (item.kind === 'agent') {
      const { entry } = item
      if (entry.run) {
        return (
          <Button asChild size="sm" variant="outline">
            <Link to={flowRunPath(entry.run.id)} onClick={onLeave}>
              Open run
            </Link>
          </Button>
        )
      }
      if (entry.kind === 'approval' && entry.id) {
        const decide = (approve: boolean) =>
          run(
            () =>
              traceAction('assistant.respond', { 'scadbuddy.respond_kind': 'approval' }, () =>
                respond(entry.id, decisionBody(approve, entry.inputHash)),
              ),
            approve ? 'Approved.' : 'Denied.',
          )
        return (
          <>
            <Button size="sm" disabled={busy} onClick={() => decide(true)} {...USER_ONLY}>
              Approve
            </Button>
            <Button size="sm" variant="destructive" disabled={busy} onClick={() => decide(false)} {...USER_ONLY}>
              Deny
            </Button>
            {entry.sessionId && ctx?.openSession && (
              <Button size="sm" variant="ghost" onClick={() => (onLeave?.(), ctx.openSession?.(entry.sessionId!))}>
                Open session
              </Button>
            )}
          </>
        )
      }
      return ctx?.openSession ? (
        <Button size="sm" variant="outline" onClick={() => (onLeave?.(), ctx.openSession?.(entry.sessionId))}>
          Answer in the assistant
        </Button>
      ) : null
    }
    if (item.kind === 'run-grant') {
      const runId = item.request.run_id
      return (
        <>
          {compact ? (
            <Button asChild size="sm">
              <Link to={flowRunPath(runId)} onClick={onLeave}>
                Review
              </Link>
            </Button>
          ) : (
            <Button size="sm" aria-expanded={reviewing} onClick={() => setReviewing((r) => !r)}>
              {reviewing ? 'Hide grant' : 'Review grant…'}
            </Button>
          )}
          <Button
            size="sm"
            variant="destructive"
            disabled={busy}
            onClick={() => run(() => api.declineFlowGrant(runId), 'Declined.')}
            {...USER_ONLY}
          >
            Decline
          </Button>
        </>
      )
    }
    if (item.kind === 'trigger-grant') {
      const id = item.trigger.id
      return (
        <>
          {compact ? (
            <Button asChild size="sm">
              <Link to={reviewPath(item.key)} onClick={onLeave}>
                Review
              </Link>
            </Button>
          ) : (
            <Button size="sm" aria-expanded={reviewing} onClick={() => setReviewing((r) => !r)}>
              {reviewing ? 'Hide trigger' : 'Review grant…'}
            </Button>
          )}
          <Button
            size="sm"
            variant="destructive"
            disabled={busy}
            onClick={() => run(() => api.declineFlowTriggerGrant(id), 'Declined.')}
            {...USER_ONLY}
          >
            Decline
          </Button>
        </>
      )
    }
    return null
  })()

  const waited = waitedText(item, now)
  return (
    <article
      aria-labelledby={headingId}
      data-testid="needs-you-item"
      data-kind={item.kind === 'agent' ? item.entry.kind : item.kind}
      className={cn(
        'flex flex-col gap-1 border-l-[3px] border-needs bg-card text-[13px]',
        compact ? 'rounded-r-md px-2 py-1.5' : 'rounded-r-lg px-3 py-2 ring-1 ring-foreground/10',
      )}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <h3 id={headingId} className="font-semibold text-needs">
          {kindLabel(item)}
        </h3>
        <span className="min-w-0 break-words text-muted-foreground">{sourceText(item)}</span>
        {waited && <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">{waited}</span>}
      </div>
      {askText(item) && (
        <p className={cn('whitespace-pre-wrap break-words', compact && item.kind !== 'agent' && 'text-muted-foreground')}>
          {askText(item)}
        </p>
      )}
      {state.kind === 'done' ? (
        <p role="status" className="text-xs text-muted-foreground">
          {state.text}
        </p>
      ) : (
        actions && <div className="mt-0.5 flex flex-wrap items-center gap-1.5">{actions}</div>
      )}
      {state.kind === 'idle' && state.error && (
        <p role="alert" className="text-xs text-warn">
          {state.error}
        </p>
      )}
      {reviewing && (
        <Suspense fallback={<Spinner />}>
          {item.kind === 'run-grant' && (
            <GrantPanel runId={item.request.run_id} grant={item.request.grant} onChange={refresh} />
          )}
          {item.kind === 'trigger-grant' && <TriggerList triggers={[item.trigger]} onChanged={refresh} />}
        </Suspense>
      )}
    </article>
  )
}

/** The rows, with no heading of their own; nothing while nothing waits or the list is unknown. */
export function NeedsYouList({ compact = false, onLeave }: { compact?: boolean; onLeave?: () => void }) {
  const ctx = useContext(NeedsYouContext)
  const now = useNow()
  const [params] = useSearchParams()
  const opened = compact ? null : params.get(REVIEW_PARAM)
  const items = ctx?.items ?? []
  const more = ctx?.more ?? 0
  if (items.length === 0) return null
  return (
    <>
      <ul className="flex flex-col gap-1.5">
        {items.map((item) => (
          <li key={item.key}>
            <NeedsYouRow item={item} compact={compact} now={now} onLeave={onLeave} open={item.key === opened} />
          </li>
        ))}
      </ul>
      {more > 0 && <p className="px-1 text-xs text-muted-foreground">and {more} more grant {more === 1 ? 'request' : 'requests'}</p>}
    </>
  )
}

/** "asked 3 minutes ago" moves on while the list is open. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(timer)
  }, [])
  return now
}

/**
 * The Activity page's full list, at its top (`#needs-you`, which the bell's "See all" and a
 * notification open: it is scrolled to and focused then). Nothing while nothing waits.
 */
export function NeedsYouSection() {
  const ctx = useContext(NeedsYouContext)
  const { hash, key } = useLocation()
  const heading = useRef<HTMLHeadingElement>(null)
  const count = ctx?.items?.length ?? 0
  useEffect(() => {
    if (hash !== `#${NEEDS_YOU_ID}` || count === 0) return
    const target = heading.current
    target?.scrollIntoView?.({ block: 'start' })
    target?.focus({ preventScroll: true })
    // Once per arrival (`key`): a list that changes while it is read does not take the focus back.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hash, key, count > 0])
  if (count === 0) return null
  return (
    <section id={NEEDS_YOU_ID} aria-labelledby="needs-you-heading" className="flex scroll-mt-4 flex-col gap-2">
      <h2 id="needs-you-heading" ref={heading} tabIndex={-1} aria-label={`Needs you, ${count}`} className="flex items-center gap-2 text-base font-semibold focus:outline-none">
        Needs you
        <span className="rounded-full bg-needs px-2 text-xs leading-5 font-semibold text-needs-ink tabular-nums">{count}</span>
      </h2>
      <NeedsYouList />
    </section>
  )
}
