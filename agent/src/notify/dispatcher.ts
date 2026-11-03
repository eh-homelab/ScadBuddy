import { createHash } from 'node:crypto'
import { EgressError, type Resolver, systemResolver } from '../http/egress.js'
import { pinnedFetch } from '../http/pinned.js'
import type { PendingInputPage } from '../routes/pendingInput.js'
import type { Owner } from '../sessions/protocol.js'
import { BROWSER_USER } from '../routes/approvals.js'
import { ChannelError, type ChannelMessage, type ChannelStore, type OpenChannel } from './channels.js'
import { checkPushEndpoint, type PinnedEndpoint } from './pushEgress.js'
import type { PushStore, PushSubscriptionRow } from './pushStore.js'
import { encryptPayload, vapidAuthorization } from './webPush.js'

// #815 §3, the closed-tab channel: every attention request waiting on a person (a
// `tab_disconnected` wait among them) is pushed once to each browser subscribed for someone
// who may see it, so the service worker (frontend `public/push-sw.js`) can show it with no
// ScadBuddy tab open. A pass runs on each pending-input change (gate/changes.ts) and at
// start; `ai_notifications_sent` makes each (request, subscription) one send across replicas
// and restarts.
//
// What is pushed is the request's id, its session's id and its reason, never its message:
// enough to name the notification and open the request, and nothing a push service could
// read of the conversation even if it could open the payload. Each push is checked by
// pushEgress.ts (https, an allowlisted push service, public addresses only) and connects to
// the address it checked, following no redirect. A pass has at most PUSH_CONCURRENCY pushes
// in flight and starts none past PASS_DEADLINE_MS; a send is claimed only as it starts, so
// what a pass did not reach is the next pass's.

/** An attention request waiting on the user, as a push announces it. */
export type AttentionNotice = { id: string; sessionId: string | null; reason: string }

/** The attention requests in a pending-input page that wait on the person: never a `done` summary (frontend `attention.ts` `notices`). */
export function attentionNotices(page: PendingInputPage): AttentionNotice[] {
  return page.entries.flatMap((entry) => {
    const a = entry.attention
    if (entry.kind !== 'answer' || !a || !entry.id || entry.expiring || entry.run?.hop === 'parent') return []
    if (a.reason === 'done' && a.on_timeout === null) return []
    return [{ id: entry.id, sessionId: typeof entry.session_id === 'string' ? entry.session_id : null, reason: a.reason }]
  })
}

/** How long a push service keeps an undelivered message: an attention request's default timeout (300 s) and then some. */
export const PUSH_TTL_S = 900
/** How many pushes a pass has in flight at once. */
export const PUSH_CONCURRENCY = 4
/** How long one pass may run; a push still in flight then is aborted. */
export const PASS_DEADLINE_MS = 30_000
/** How long one push may take. */
export const PUSH_TIMEOUT_MS = 10_000

export type NotificationDispatcherDeps = {
  store: PushStore
  /** The attention requests `owner` may see now (`attentionNotices` of `pendingInput`). */
  notices: (owner: Owner) => Promise<AttentionNotice[]>
  /** VAPID's `sub`: who the push service may contact (SCADBUDDY_PUBLIC_URL). */
  subject: string
  /** The push-service allowlist (pushEgress.ts `pushHostsOf` of the stored setting). */
  hosts: () => Promise<readonly string[]>
  resolve?: Resolver
  /** Tests only: checks and pins an endpoint in place of `checkPushEndpoint`, for a fake push service on loopback. */
  pinEndpoint?: (endpoint: string) => Promise<PinnedEndpoint>
  /** A pass's deadline, PASS_DEADLINE_MS unless a test sets it. */
  passDeadlineMs?: number
  /** The out-of-browser channels (channels.ts), told of what the browser user may see; none without the KEK. */
  channels?: ChannelStore | undefined
  /** SCADBUDDY_PUBLIC_URL: where a channel's link opens the session; no link without it. */
  publicUrl?: string | undefined
  log?: (message: string) => void
}

type Vapid = Awaited<ReturnType<PushStore['vapidKeys']>>

export class NotificationDispatcher {
  readonly #deps: NotificationDispatcherDeps
  #running: Promise<void> | undefined
  #again = false

  constructor(deps: NotificationDispatcherDeps) {
    this.#deps = deps
  }

  /** A pass now, or one more after the pass under way; never two at once in this process. */
  wake(): void {
    if (this.#running) {
      this.#again = true
      return
    }
    this.#running = this.pass()
      .catch((err: unknown) => this.#log(`web push: a pass failed (${(err as Error).message})`))
      .finally(() => {
        this.#running = undefined
        if (this.#again) {
          this.#again = false
          this.wake()
        }
      })
  }

  /** Sends what has not been sent yet to every subscription and every enabled channel. */
  async pass(): Promise<void> {
    const subscriptions = await this.#deps.store.subscriptions()
    const channels = (await this.#deps.channels?.enabled()) ?? []
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
      for (const notice of await this.#deps.notices(owner)) {
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
      for (const notice of await this.#deps.notices(BROWSER_USER)) {
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

/** What a channel is sent for an attention request: its reason and a link, never its message. */
export function attentionMessage(notice: AttentionNotice, publicUrl: string | undefined): ChannelMessage {
  const click = sessionLink(publicUrl, notice.sessionId)
  return { title: noticeTitle(notice.reason), body: 'Open ScadBuddy to answer it.', ...(click ? { click } : {}) }
}

/** RFC 8030 §5.4: a message with the same topic replaces one not yet delivered; at most 32 base64url characters. */
function topicOf(requestId: string): string {
  return createHash('sha256').update(requestId).digest('base64url').slice(0, 32)
}
