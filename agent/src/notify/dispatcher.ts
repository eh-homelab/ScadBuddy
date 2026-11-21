import { createHash } from 'node:crypto'
import { EgressError, type Resolver, systemResolver } from '../http/egress.js'
import { pinnedFetch } from '../http/pinned.js'
import type { PendingInputPage } from '../routes/pendingInput.js'
import type { Owner } from '../sessions/protocol.js'
import { BROWSER_USER } from '../routes/approvals.js'
import { ChannelError, type ChannelMessage, type ChannelStore, type OpenChannel } from './channels.js'
import { checkPushEndpoint, type PinnedEndpoint } from './pushEgress.js'
import type { PushRepo, PushSubscriptionRow } from './pushStore.js'
import { encryptPayload, vapidAuthorization } from './webPush.js'

// #815 §3, the closed-tab channel: every request waiting on a person (an attention request,
// a `tab_disconnected` wait, a plain question or an approval) is pushed once to each
// browser subscribed for someone who may see it, so the service worker (frontend
// `public/push-sw.js`) can show it with no ScadBuddy tab open. A pass runs on each
// pending-input change (gate/changes.ts) and at start; `ai_notifications_sent` makes each
// (request, subscription) one send across replicas and restarts.
//
// What is pushed is the request's id, its session's id and its reason, never its message:
// enough to name the notification and open the request, and nothing a push service could
// read of the conversation even if it could open the payload. Each push is checked by
// pushEgress.ts (https, an allowlisted push service, public addresses only) and connects to
// the address it checked, following no redirect. A pass has at most PUSH_CONCURRENCY pushes
// in flight and starts none past PASS_DEADLINE_MS; a send is claimed only as it starts, so
// what a pass did not reach is the next pass's.
//
// #6399: the requests outside the agent's pending input that wait on a person (a flow run's
// grant, a trigger's standing grant, an agent's pairing request) are pushed the same way, to
// a person's subscriptions (`PERSON_KINDS`) and to the channels. Nothing in this process
// hears them change, so a pass also runs every WAITING_POLL_MS, reading only them, on the one
// replica that holds the poll's lease (`pollLease`). Their notice
// ids are the page's own (frontend `lib/needsYou.ts`), so the page's notification and the push
// share a tag. Such a request has no ceiling (a standing grant waits until a person decides it),
// but a claim is kept SENT_KEEP_DAYS only: so one is pushed only while it is younger than
// WAITING_PUSH_WINDOW_MS, which is shorter. A claim made in that window outlives it, and once
// the claim is swept the request is too old to be claimed again: one push per target, ever.

/**
 * A request waiting on the user, as a push announces it. `reason` is an attention request's
 * own, `question` for a plain question, or `approval` for a parked call's decision.
 */
export type AttentionNotice = {
  id: string
  sessionId: string | null
  reason: string
  /** #6399: when a grant or pairing request started to wait (ms since the epoch); past WAITING_PUSH_WINDOW_MS it is not pushed. */
  since?: number
}

/**
 * Every request in a pending-input page that waits on the person: approvals, plain questions
 * and attention requests, never a `done` summary, one past its timer, or a flow run's call still
 * with the session that started it (frontend `attention.ts` `notices`, the badge's count).
 */
export function pendingNotices(page: PendingInputPage): AttentionNotice[] {
  return page.entries.flatMap((entry) => {
    if (!entry.id || entry.expiring || entry.run?.hop === 'parent') return []
    const a = entry.attention
    if (a && a.reason === 'done' && a.on_timeout === null) return []
    const reason = entry.kind === 'approval' ? 'approval' : (a?.reason ?? 'question')
    return [{ id: entry.id, sessionId: typeof entry.session_id === 'string' ? entry.session_id : null, reason }]
  })
}

/** The owners a flow grant or a pairing request is pushed to: only a person decides one. */
export const PERSON_KINDS: ReadonlySet<Owner['kind']> = new Set(['browser', 'oidc'])

/** How often a pass runs with no pending-input change, so a grant or pairing request is pushed. */
export const WAITING_POLL_MS = 30_000
/** The lease that makes one replica a timer window's poller: one row, taken again once it is this old. */
export const WAITING_POLL_LEASE = 'waiting-poll'
export const WAITING_POLL_LEASE_S = WAITING_POLL_MS / 1000 - 5

/**
 * How long after it started a grant or pairing request may be pushed: shorter than the claims'
 * SENT_KEEP_DAYS (pushStore.ts, backend `crud_agent_notify.py`), so a swept claim is never
 * claimed again.
 */
export const WAITING_PUSH_WINDOW_MS = 24 * 60 * 60_000

/** The reasons whose request is answered on the Activity page's "Needs you", not in a session. */
const NEEDS_YOU_REASONS: ReadonlySet<string> = new Set(['grant', 'pairing'])

/** The page a grant or pairing request opens (frontend `lib/needsYou.ts` `NEEDS_YOU_PATH`). */
export const NEEDS_YOU_PATH = '/activity#needs-you'

/** How long a push service keeps an undelivered message: an attention request's default timeout (300 s) and then some. */
export const PUSH_TTL_S = 900
/** How many pushes a pass has in flight at once. */
export const PUSH_CONCURRENCY = 4
/** How long one pass may run; a push still in flight then is aborted. */
export const PASS_DEADLINE_MS = 30_000
/** How long one push may take. */
export const PUSH_TIMEOUT_MS = 10_000

export type NotificationDispatcherDeps = {
  store: PushRepo
  /** The requests waiting on `owner` now (`pendingNotices` of `pendingInput`). */
  notices: (owner: Owner) => Promise<AttentionNotice[]>
  /** #6399: the flow grants and pairing requests waiting on a person now, told to `PERSON_KINDS` and the channels. */
  waiting?: () => Promise<AttentionNotice[]>
  /**
   * #6399: takes the named poll's lease for `seconds` (backend `push_poll_lease`), true when
   * this replica holds it. With none, every replica polls; the sends are still claimed once.
   */
  pollLease?: (name: string, seconds: number) => Promise<boolean>
  /** VAPID's `sub`: who the push service may contact (SCADBUDDY_PUBLIC_URL). */
  subject: string
  /** The push-service allowlist (pushEgress.ts `pushHostsOf` of the stored setting). */
  hosts: () => Promise<readonly string[]>
  resolve?: Resolver
  /** Tests only: checks and pins an endpoint in place of `checkPushEndpoint`, for a fake push service on loopback. */
  pinEndpoint?: (endpoint: string) => Promise<PinnedEndpoint>
  /** A pass's deadline, PASS_DEADLINE_MS unless a test sets it. */
  passDeadlineMs?: number
  /** The out-of-browser channels (channels.ts), told of what the browser user may see; none without the database; with no KEK here, only once the API serves area `notify`. */
  channels?: ChannelStore | undefined
  /** SCADBUDDY_PUBLIC_URL: where a channel's link opens the session; no link without it. */
  publicUrl?: string | undefined
  log?: (message: string) => void
}

type Vapid = Awaited<ReturnType<PushRepo['vapidKeys']>>

/** What a pass reads: everything, or (a timer's wake) only the grant and pairing requests. */
export type PassScope = 'all' | 'waiting'

export class NotificationDispatcher {
  readonly #deps: NotificationDispatcherDeps
  #running: Promise<void> | undefined
  #again: PassScope | undefined

  constructor(deps: NotificationDispatcherDeps) {
    this.#deps = deps
  }

  /**
   * A pass now, or one more after the pass under way; never two at once in this process.
   * `waiting` reads only the grant and pairing requests (the timer's wake): nothing else changed.
   */
  wake(scope: PassScope = 'all'): void {
    if (this.#running) {
      this.#again = this.#again === 'all' || scope === 'all' ? 'all' : 'waiting'
      return
    }
    this.#running = this.pass(scope)
      .catch((err: unknown) => this.#log(`web push: a pass failed (${(err as Error).message})`))
      .finally(() => {
        this.#running = undefined
        const again = this.#again
        this.#again = undefined
        if (again) this.wake(again)
      })
  }

  /** Sends what has not been sent yet to every subscription and every enabled channel. */
  async pass(scope: PassScope = 'all'): Promise<void> {
    // A timer's pass runs on the one replica that holds the poll's lease (one row, upserted
    // by the API), so N replicas make one read a window. A lease that cannot be read skips it.
    if (scope === 'waiting' && this.#deps.pollLease) {
      const held = await this.#deps.pollLease(WAITING_POLL_LEASE, WAITING_POLL_LEASE_S).catch(() => false)
      if (!held) return
    }
    // A timer's pass with nothing young enough to push opens no subscription's keys.
    const waiting = await this.#waiting()
    if (scope === 'waiting' && waiting.length === 0) return
    // With no KEK in this process, the records are reachable only once the API serves them (#5299 step 5).
    const subscriptions = this.#deps.store.available?.() === false ? [] : await this.#deps.store.subscriptions()
    const channelStore = this.#deps.channels?.available() === false ? undefined : this.#deps.channels
    const channels = (await channelStore?.enabled()) ?? []
    if (subscriptions.length === 0 && channels.length === 0) return
    const deadline = AbortSignal.timeout(this.#deps.passDeadlineMs ?? PASS_DEADLINE_MS)
    const byOwner = new Map<string, { owner: Owner; subs: PushSubscriptionRow[] }>()
    for (const sub of subscriptions) {
      const key = `${sub.owner.kind}:${sub.owner.id}`
      const group = byOwner.get(key) ?? { owner: sub.owner, subs: [] }
      group.subs.push(sub)
      byOwner.set(key, group)
    }
    type Job = { notice: AttentionNotice; target: string; run: () => Promise<void> }
    const jobs: Job[] = []
    let hosts: readonly string[] | undefined
    let vapid: Promise<Vapid> | undefined
    for (const { owner, subs } of byOwner.values()) {
      const own = scope === 'all' ? await this.#deps.notices(owner) : []
      const notices = [...own, ...(PERSON_KINDS.has(owner.kind) ? waiting : [])]
      for (const notice of notices) {
        for (const sub of subs) {
          jobs.push({
            notice,
            target: `push:${sub.id}`,
            run: async () => {
              hosts ??= await this.#deps.hosts()
              vapid ??= this.#deps.store.vapidKeys()
              await this.#send(sub, notice, await vapid, hosts, deadline)
            },
          })
        }
      }
    }
    // Channels are the deployment's, set in Settings by the browser user: told what that user sees.
    if (channels.length > 0) {
      const own = scope === 'all' ? await this.#deps.notices(BROWSER_USER) : []
      for (const notice of [...own, ...waiting]) {
        for (const channel of channels) {
          jobs.push({ notice, target: `channel:${channel.id}`, run: () => this.#tell(channel, notice, deadline) })
        }
      }
    }
    let next = 0
    const worker = async () => {
      while (next < jobs.length && !deadline.aborted) {
        const job = jobs[next++]!
        // Claimed only as it starts, so one the deadline cut off is the next pass's.
        if (!(await this.#deps.store.claim(job.notice.id, job.target))) continue
        await job.run()
      }
    }
    await Promise.all(Array.from({ length: Math.min(PUSH_CONCURRENCY, jobs.length) }, worker))
    if (next < jobs.length) this.#log('notifications: a pass reached its deadline; the rest go on the next pass')
  }

  /**
   * The grants and pairing requests now that are young enough to push (WAITING_PUSH_WINDOW_MS);
   * none when the read fails, so the agent's own still go.
   */
  async #waiting(): Promise<AttentionNotice[]> {
    if (!this.#deps.waiting) return []
    try {
      const oldest = Date.now() - WAITING_PUSH_WINDOW_MS
      return (await this.#deps.waiting()).filter((n) => n.since !== undefined && n.since >= oldest)
    } catch (err) {
      this.#log(`notifications: the flow grants and pairing requests could not be read (${(err as Error).message})`)
      return []
    }
  }

  async #tell(channel: OpenChannel, notice: AttentionNotice, deadline: AbortSignal): Promise<void> {
    const store = this.#deps.channels!
    try {
      await store.send(channel, attentionMessage(notice, this.#deps.publicUrl), AbortSignal.any([deadline, AbortSignal.timeout(PUSH_TIMEOUT_MS)]))
      await store.record(channel.id, null)
    } catch (err) {
      const why = err instanceof EgressError || err instanceof ChannelError ? err.message : `the send failed (${(err as Error).message})`
      await store.record(channel.id, why).catch(() => {})
      this.#log(`notification channel ${channel.id}: ${why}`)
    }
  }

  async #send(sub: PushSubscriptionRow, notice: AttentionNotice, vapid: Vapid, hosts: readonly string[], deadline: AbortSignal): Promise<void> {
    const payload = Buffer.from(JSON.stringify({ request_id: notice.id, session_id: notice.sessionId, reason: notice.reason }))
    try {
      const pinned = this.#deps.pinEndpoint
        ? await this.#deps.pinEndpoint(sub.endpoint)
        : await checkPushEndpoint(sub.endpoint, hosts, this.#deps.resolve ?? systemResolver)
      const response = await pinnedFetch(pinned.url, pinned.address, {
        method: 'POST',
        headers: {
          authorization: vapidAuthorization(pinned.url, vapid, this.#deps.subject),
          'content-encoding': 'aes128gcm',
          'content-type': 'application/octet-stream',
          ttl: String(PUSH_TTL_S),
          urgency: 'high',
          topic: topicOf(notice.id),
        },
        body: encryptPayload(payload, sub.keys),
        signal: AbortSignal.any([deadline, AbortSignal.timeout(PUSH_TIMEOUT_MS)]),
        maxBytes: 64 * 1024,
      })
      // RFC 8030 §7.3: a subscription the push service no longer has is gone for good.
      if (response.status === 404 || response.status === 410) {
        await this.#deps.store.unsubscribe(sub.endpoint)
        return
      }
      if (response.status < 200 || response.status >= 300) this.#log(`web push: the push service answered HTTP ${response.status}`)
    } catch (err) {
      const why = err instanceof EgressError ? err.message : `the push failed (${(err as Error).message})`
      this.#log(`web push: ${why}`)
    }
  }

  #log(message: string): void {
    ;(this.#deps.log ?? console.error)(message)
  }
}

/** A notification's title by reason (frontend `attentionNotifications.ts` `noticeTitle`, `public/push-sw.js`). */
export function noticeTitle(reason: string): string {
  switch (reason) {
    case 'tab_disconnected':
      return 'The assistant lost its ScadBuddy tab'
    case 'question':
      return 'The assistant has a question'
    case 'blocked':
      return 'The assistant is blocked'
    case 'approval':
      return 'The assistant needs your approval'
    case 'grant':
      return 'A flow asks for a grant'
    case 'pairing':
      return 'An agent asks to use ScadBuddy'
    default:
      return 'The assistant needs you'
  }
}

/** The page a click opens: ScadBuddy at the request's session (frontend `webPush.ts` `SESSION_PARAM`). */
export function sessionLink(publicUrl: string | undefined, sessionId: string | null): string | undefined {
  if (!publicUrl) return undefined
  const url = new URL('/', publicUrl)
  if (sessionId) url.searchParams.set('assistant-session', sessionId)
  return url.href
}

/** The page a notice's click opens: "Needs you" for a grant or pairing request, else its session. */
export function noticeLink(publicUrl: string | undefined, notice: AttentionNotice): string | undefined {
  if (!NEEDS_YOU_REASONS.has(notice.reason)) return sessionLink(publicUrl, notice.sessionId)
  return publicUrl ? new URL(NEEDS_YOU_PATH, publicUrl).href : undefined
}

/** What a notification says to do (frontend `public/push-sw.js` `BODIES`). */
export function noticeBody(reason: string): string {
  return reason === 'approval' || NEEDS_YOU_REASONS.has(reason) ? 'Open ScadBuddy to decide it.' : 'Open ScadBuddy to answer it.'
}

/** What a channel is sent for a request: its reason and a link, never its message or the call it would approve. */
export function attentionMessage(notice: AttentionNotice, publicUrl: string | undefined): ChannelMessage {
  const click = noticeLink(publicUrl, notice)
  return { title: noticeTitle(notice.reason), body: noticeBody(notice.reason), ...(click ? { click } : {}) }
}

/** RFC 8030 §5.4: a message with the same topic replaces one not yet delivered; at most 32 base64url characters. */
function topicOf(requestId: string): string {
  return createHash('sha256').update(requestId).digest('base64url').slice(0, 32)
}
