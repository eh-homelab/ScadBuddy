import { CrudReadError } from '../api/crudRead.js'
import { type CrudTarget, crudOf } from '../api/crudStores.js'
import type { Owner } from '../sessions/protocol.js'
import { type EcKeys, generateEcKeys, type Subscriber } from './webPush.js'

// Web Push's records (#815 §3; migration 20261103T2000Z_web_push.sql), kept by the API: the
// VAPID key, sealed under the API's KEK; each browser's push subscription, its `p256dh` and
// `auth` keys sealed with the endpoint in the AAD (they are what a message to that endpoint is
// encrypted to); and which request each target was told of, so several replicas hearing one
// change send it once.

export type PushSubscriptionRow = { id: string; owner: Owner; endpoint: string; keys: Subscriber }

/** How many browsers one person may subscribe; a new one past it evicts the oldest. */
export const SUBSCRIPTIONS_PER_OWNER = 5

/** The endpoint is another person's subscription. */
export class SubscriptionTakenError extends Error {
  override name = 'SubscriptionTakenError'
  constructor() {
    super('this push endpoint is subscribed for someone else')
  }
}

/** How long a "told" row is kept: past any entry's 86 400 s ceiling (ai_pending_input). */
export const SENT_KEEP_DAYS = 2

/** The push records as the routes and the dispatcher use them. */
export interface PushRepo {
  /** The VAPID key pair, made and stored the first time it is asked for (one per deployment). */
  vapidKeys(): Promise<EcKeys>
  /**
   * Stores `endpoint` for `owner`, replacing what `owner` stored for it (a browser
   * re-subscribing). One another person holds is refused (SubscriptionTakenError), never
   * replaced. An owner keeps at most SUBSCRIPTIONS_PER_OWNER; past that the oldest go.
   */
  subscribe(owner: Owner, endpoint: string, keys: Subscriber): Promise<string>
  /** Forgets `endpoint`, when `owner` stored it (or it is gone at the push service: `owner` left out). */
  unsubscribe(endpoint: string, owner?: Owner): Promise<boolean>
  /** Every subscription whose keys the API's KEK opens. */
  subscriptions(): Promise<PushSubscriptionRow[]>
  /**
   * Claims telling `target` of `requestId`: true for the one caller that gets to send it.
   * Rows older than SENT_KEEP_DAYS go at the same time.
   */
  claim(requestId: string, target: string): Promise<boolean>
}

/** The route's reason for an endpoint another person subscribed (backend api/crud_agent_notify.py). */
const SUBSCRIPTION_TAKEN = 'subscription-taken'

/**
 * The push records through the API's CRUD routes, area `notify` (backend
 * api/crud_agent_notify.py): the API seals the keys with its KEK, so this process needs none. The VAPID pair is still made here (the first stored wins), and the private
 * key comes back from the API for each pass's signing.
 */
export class CrudPushStore implements PushRepo {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async vapidKeys(): Promise<EcKeys> {
    type Answer = { vapid: { public_key: string; private_key: string } | null }
    const keys = (a: Answer) => (a.vapid ? { publicKey: Buffer.from(a.vapid.public_key, 'hex'), privateKey: Buffer.from(a.vapid.private_key, 'base64url') } : undefined)
    const found = keys(await this.#call<Answer>('the VAPID key', '/push/vapid'))
    if (found) return found
    const made = generateEcKeys()
    const body = { public_key: made.publicKey.toString('hex'), private_key: made.privateKey.toString('base64url') }
    return keys(await this.#call<Answer>('the VAPID key stored', '/push/vapid', { body }))!
  }

  async subscribe(owner: Owner, endpoint: string, keys: Subscriber): Promise<string> {
    const body = { owner, endpoint, p256dh: keys.p256dh.toString('base64url'), auth: keys.auth.toString('base64url') }
    try {
      return (await this.#call<{ id: string }>('a push subscription', '/push/subscriptions', { body })).id
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.reason === SUBSCRIPTION_TAKEN) throw new SubscriptionTakenError()
      throw err
    }
  }

  async unsubscribe(endpoint: string, owner?: Owner): Promise<boolean> {
    const body = { endpoint, ...(owner ? { owner } : {}) }
    return (await this.#call<{ done: boolean }>('a push subscription removed', '/push/subscriptions/remove', { body })).done
  }

  async subscriptions(): Promise<PushSubscriptionRow[]> {
    const { subscriptions } = await this.#call<{ subscriptions: { id: string; owner: Owner; endpoint: string; p256dh: string; auth: string }[] }>(
      'the push subscriptions',
      '/push/subscriptions',
    )
    return subscriptions.map((s) => ({
      id: s.id,
      owner: s.owner,
      endpoint: s.endpoint,
      keys: { p256dh: Buffer.from(s.p256dh, 'base64url'), auth: Buffer.from(s.auth, 'base64url') },
    }))
  }

  async claim(requestId: string, target: string): Promise<boolean> {
    return (await this.#call<{ claimed: boolean }>('a notification claimed', '/push/sent', { body: { request_id: requestId, target } })).claimed
  }
}
