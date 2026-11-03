import { createHash } from 'node:crypto'
import { assertHostAllowed, assertSecureUrl, EgressError, type Resolver, systemResolver } from '../http/egress.js'
import { pinnedFetch } from '../http/pinned.js'
import type { PendingInputPage } from '../routes/pendingInput.js'
import type { Owner } from '../sessions/protocol.js'
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
// read of the conversation even if it could open the payload. The push goes through the
// egress check (http/egress.ts): https only, the host resolved and every address checked,
// the connection pinned to them, no redirect followed.

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

export type PushDispatcherDeps = {
  store: PushStore
  /** The attention requests `owner` may see now (`attentionNotices` of `pendingInput`). */
  notices: (owner: Owner) => Promise<AttentionNotice[]>
  /** VAPID's `sub`: who the push service may contact (SCADBUDDY_PUBLIC_URL). */
  subject: string
  resolve?: Resolver
  log?: (message: string) => void
}

export class PushDispatcher {
  readonly #deps: PushDispatcherDeps
  #running: Promise<void> | undefined
  #again = false

  constructor(deps: PushDispatcherDeps) {
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

  /** Sends what has not been sent yet to every subscription. */
  async pass(): Promise<void> {
    const subscriptions = await this.#deps.store.subscriptions()
    if (subscriptions.length === 0) return
    const byOwner = new Map<string, { owner: Owner; subs: PushSubscriptionRow[] }>()
    for (const sub of subscriptions) {
      const key = `${sub.owner.kind}:${sub.owner.id}`
      const group = byOwner.get(key) ?? { owner: sub.owner, subs: [] }
      group.subs.push(sub)
      byOwner.set(key, group)
    }
    let vapid: Awaited<ReturnType<PushStore['vapidKeys']>> | undefined
    for (const { owner, subs } of byOwner.values()) {
      const notices = await this.#deps.notices(owner)
      for (const notice of notices) {
        for (const sub of subs) {
          if (!(await this.#deps.store.claim(notice.id, `push:${sub.id}`))) continue
          vapid ??= await this.#deps.store.vapidKeys()
          await this.#send(sub, notice, vapid)
        }
      }
    }
  }

  async #send(sub: PushSubscriptionRow, notice: AttentionNotice, vapid: Awaited<ReturnType<PushStore['vapidKeys']>>): Promise<void> {
    const payload = Buffer.from(JSON.stringify({ request_id: notice.id, session_id: notice.sessionId, reason: notice.reason }))
    try {
      const target = assertSecureUrl(sub.endpoint, 'push endpoint')
      const [address] = await assertHostAllowed(target.href, this.#deps.resolve ?? systemResolver, 'push endpoint', 'a push service')
      const response = await pinnedFetch(target.href, address!, {
        method: 'POST',
        headers: {
          authorization: vapidAuthorization(target.href, vapid, this.#deps.subject),
          'content-encoding': 'aes128gcm',
          'content-type': 'application/octet-stream',
          ttl: String(PUSH_TTL_S),
          urgency: 'high',
          topic: topicOf(notice.id),
        },
        body: encryptPayload(payload, sub.keys),
        signal: AbortSignal.timeout(10_000),
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

/** RFC 8030 §5.4: a message with the same topic replaces one not yet delivered; at most 32 base64url characters. */
function topicOf(requestId: string): string {
  return createHash('sha256').update(requestId).digest('base64url').slice(0, 32)
}
