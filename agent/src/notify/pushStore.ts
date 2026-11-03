import { randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { type Envelope, type Kek, openSecret, rewrap, SealError, sealSecret } from '../secrets.js'
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

/** How long a "told" row is kept: past any entry's 86 400 s ceiling (ai_pending_input). */
export const SENT_KEEP_DAYS = 2

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

  /** Stores `endpoint` for `owner`, replacing what was stored for it (a browser re-subscribing, or another person on it). */
  async subscribe(owner: Owner, endpoint: string, keys: Subscriber): Promise<string> {
    const id = randomUUID()
    const secret = JSON.stringify({ p256dh: keys.p256dh.toString('base64url'), auth: keys.auth.toString('base64url') })
    const sealed = sealSecret(this.#kek, secret, subscriptionAad(id, endpoint))
    await this.#sql.begin(async (tx) => {
      await tx`DELETE FROM ai_push_subscriptions WHERE endpoint = ${endpoint}`
      await tx`
        INSERT INTO ai_push_subscriptions (id, owner_kind, owner_id, owner_label, endpoint, secret_sealed, dek_sealed, kek_id)
        VALUES (${id}, ${owner.kind}, ${owner.id}, ${owner.label}, ${endpoint}, ${sealed.secretSealed}, ${sealed.dekSealed}, ${sealed.kekId})`
    })
    return id
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

  /** Key rotation (spec §9): the VAPID key's and every subscription's data key, re-sealed under `current`. */
  async rewrapFrom(previous: Kek, current: Kek): Promise<{ rewrapped: number; failed: number }> {
    const result = { rewrapped: 0, failed: 0 }
    if (previous.id === current.id) return result
    const vapid = await this.#sql<Sealed[]>`SELECT secret_sealed, dek_sealed, kek_id FROM ai_push_vapid WHERE kek_id = ${previous.id}`
    const subs = await this.#sql<(Sealed & { id: string; endpoint: string })[]>`
      SELECT id, endpoint, secret_sealed, dek_sealed, kek_id FROM ai_push_subscriptions WHERE kek_id = ${previous.id}`
    const next = (row: Sealed, aad: string): Envelope | undefined => {
      try {
        return rewrap(previous, current, envelope(row), aad)
      } catch (err) {
        if (!(err instanceof SealError)) throw err
        result.failed++
        return undefined
      }
    }
    for (const row of vapid) {
      const e = next(row, VAPID_AAD)
      if (!e) continue
      const updated = await this.#sql`
        UPDATE ai_push_vapid SET dek_sealed = ${e.dekSealed}, kek_id = ${e.kekId} WHERE id = 1 AND kek_id = ${previous.id}`
      result.rewrapped += updated.count
    }
    for (const row of subs) {
      const e = next(row, subscriptionAad(row.id, row.endpoint))
      if (!e) continue
      const updated = await this.#sql`
        UPDATE ai_push_subscriptions SET dek_sealed = ${e.dekSealed}, kek_id = ${e.kekId} WHERE id = ${row.id} AND kek_id = ${previous.id}`
      result.rewrapped += updated.count
    }
    return result
  }
}
