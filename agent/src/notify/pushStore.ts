import { randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { CrudReadError } from '../api/crudRead.js'
import { type CrudTarget, crudOf } from '../api/crudStores.js'
import { type Envelope, type Kek, openSecret, SealError, sealSecret } from '../secrets.js'
import type { Owner } from '../sessions/protocol.js'
import { type EcKeys, generateEcKeys, type Subscriber } from './webPush.js'

// Web Push's records (#815 §3; migration 20261103T2000Z_web_push.sql): the VAPID key, made
// on first use and kept sealed under the KEK (secrets.ts), never in an env var; each
// browser's push subscription, its `p256dh` and `auth` keys sealed with the endpoint in the
// AAD (they are what a message to that endpoint is encrypted to); and which request each
// target was told of, so several replicas hearing one change send it once.

type Sealed = { secret_sealed: Buffer; dek_sealed: Buffer; kek_id: string }

const envelope = (row: Sealed): Envelope => ({ secretSealed: row.secret_sealed, dekSealed: row.dek_sealed, kekId: row.kek_id })
const VAPID_AAD = 'push-vapid:1'
const subscriptionAad = (id: string, endpoint: string) => `push-subscription:${id}:${endpoint}`

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

/**
 * The push records as the routes and the dispatcher use them: `PushStore` over SQL with this
 * process's KEK, or `CrudPushStore` through the API's CRUD routes (area `notify`, #5299 step 5),
 * where the API seals and opens. `available` says whether the store can be used now (with no
 * KEK here, only once the API serves the area); left out, always.
 */
export type PushRepo = Pick<PushStore, 'vapidKeys' | 'subscribe' | 'unsubscribe' | 'subscriptions' | 'claim'> & { available?(): boolean }

export class PushStore {
  readonly #sql: Sql
  readonly #kek: Kek

  constructor(sql: Sql, kek: Kek) {
    this.#sql = sql
    this.#kek = kek
  }

  /** The VAPID key pair, made and stored the first time it is asked for (one per deployment). */
  async vapidKeys(): Promise<EcKeys> {
    const read = async () => {
      const [row] = await this.#sql<(Sealed & { public_key: Buffer })[]>`
        SELECT public_key, secret_sealed, dek_sealed, kek_id FROM ai_push_vapid WHERE id = 1`
      return row
        ? { publicKey: row.public_key, privateKey: Buffer.from(openSecret(this.#kek, envelope(row), VAPID_AAD), 'base64url') }
        : undefined
    }
    const found = await read()
    if (found) return found
    const made = generateEcKeys()
    const sealed = sealSecret(this.#kek, made.privateKey.toString('base64url'), VAPID_AAD)
    // Two replicas making one at once: the first insert wins and both read it back.
    await this.#sql`
      INSERT INTO ai_push_vapid (id, public_key, secret_sealed, dek_sealed, kek_id)
      VALUES (1, ${made.publicKey}, ${sealed.secretSealed}, ${sealed.dekSealed}, ${sealed.kekId})
      ON CONFLICT (id) DO NOTHING`
    return (await read())!
  }

  /**
   * Stores `endpoint` for `owner`, replacing what `owner` stored for it (a browser
   * re-subscribing). One another person holds is refused (SubscriptionTakenError), never
   * replaced. An owner keeps at most SUBSCRIPTIONS_PER_OWNER; past that the oldest go.
   */
  async subscribe(owner: Owner, endpoint: string, keys: Subscriber): Promise<string> {
    const secret = JSON.stringify({ p256dh: keys.p256dh.toString('base64url'), auth: keys.auth.toString('base64url') })
    return this.#sql.begin(async (tx) => {
      const [held] = await tx<{ id: string; owner_kind: string; owner_id: string }[]>`
        SELECT id, owner_kind, owner_id FROM ai_push_subscriptions WHERE endpoint = ${endpoint} FOR UPDATE`
      if (held && (held.owner_kind !== owner.kind || held.owner_id !== owner.id)) throw new SubscriptionTakenError()
      if (held) {
        // The same browser again (every shell mount PUTs it): the same id, so what was already
        // pushed to it (ai_notifications_sent, `push:<id>`) is not pushed again. Its keys are
        // re-sealed, in case the browser rotated them, and it counts as the newest.
        const sealed = sealSecret(this.#kek, secret, subscriptionAad(held.id, endpoint))
        await tx`
          UPDATE ai_push_subscriptions SET owner_label = ${owner.label}, secret_sealed = ${sealed.secretSealed},
                 dek_sealed = ${sealed.dekSealed}, kek_id = ${sealed.kekId}, created_at = now()
           WHERE id = ${held.id}`
        return held.id
      }
      const id = randomUUID()
      const sealed = sealSecret(this.#kek, secret, subscriptionAad(id, endpoint))
      await tx`
        INSERT INTO ai_push_subscriptions (id, owner_kind, owner_id, owner_label, endpoint, secret_sealed, dek_sealed, kek_id)
        VALUES (${id}, ${owner.kind}, ${owner.id}, ${owner.label}, ${endpoint}, ${sealed.secretSealed}, ${sealed.dekSealed}, ${sealed.kekId})`
      await tx`
        DELETE FROM ai_push_subscriptions WHERE id IN (
          SELECT id FROM ai_push_subscriptions WHERE owner_kind = ${owner.kind} AND owner_id = ${owner.id}
           ORDER BY created_at DESC, id OFFSET ${SUBSCRIPTIONS_PER_OWNER})`
      return id
    })
  }

  /** Forgets `endpoint`, when `owner` stored it (or it is gone at the push service: `owner` left out). */
  async unsubscribe(endpoint: string, owner?: Owner): Promise<boolean> {
    const rows = owner
      ? await this.#sql`DELETE FROM ai_push_subscriptions WHERE endpoint = ${endpoint} AND owner_kind = ${owner.kind} AND owner_id = ${owner.id}`
      : await this.#sql`DELETE FROM ai_push_subscriptions WHERE endpoint = ${endpoint}`
    return rows.count > 0
  }

  /** Every subscription whose keys this KEK opens; one it cannot is skipped (left for rotation). */
  async subscriptions(): Promise<PushSubscriptionRow[]> {
    const rows = await this.#sql<(Sealed & { id: string; owner_kind: string; owner_id: string; owner_label: string; endpoint: string })[]>`
      SELECT id, owner_kind, owner_id, owner_label, endpoint, secret_sealed, dek_sealed, kek_id
        FROM ai_push_subscriptions ORDER BY created_at`
    return rows.flatMap((row) => {
      try {
        const keys = JSON.parse(openSecret(this.#kek, envelope(row), subscriptionAad(row.id, row.endpoint))) as { p256dh: string; auth: string }
        return [
          {
            id: row.id,
            owner: { kind: row.owner_kind, id: row.owner_id, label: row.owner_label } as Owner,
            endpoint: row.endpoint,
            keys: { p256dh: Buffer.from(keys.p256dh, 'base64url'), auth: Buffer.from(keys.auth, 'base64url') },
          },
        ]
      } catch (err) {
        if (err instanceof SealError) return []
        throw err
      }
    })
  }

  /**
   * Claims telling `target` of `requestId`: true for the one caller that gets to send it.
   * Rows older than SENT_KEEP_DAYS go at the same time.
   */
  async claim(requestId: string, target: string): Promise<boolean> {
    await this.#sql`DELETE FROM ai_notifications_sent WHERE sent_at < now() - make_interval(days => ${SENT_KEEP_DAYS})`
    const rows = await this.#sql`
      INSERT INTO ai_notifications_sent (request_id, target) VALUES (${requestId}, ${target})
      ON CONFLICT DO NOTHING RETURNING 1`
    return rows.length > 0
  }

}

/** The route's reason for an endpoint another person subscribed (backend api/crud_agent_notify.py). */
const SUBSCRIPTION_TAKEN = 'subscription-taken'

/**
 * The push records through the API's CRUD routes, area `notify` (#5299 step 5; backend
 * api/crud_agent_notify.py): the API seals the keys with its KEK under this file's AAD, so this
 * process needs none. The VAPID pair is still made here (the first stored wins), and the private
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
