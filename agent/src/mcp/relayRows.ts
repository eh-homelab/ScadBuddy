import type { Sql } from 'postgres'
import { type CrudTarget, crudOf } from '../api/crudStores.js'

// The /mcp session relay's storage (#2086, sessionRelay.ts): the directory of which replica holds
// each session (`ai_mcp_sessions`), the relay's message rows (`ai_mcp_relay_messages`) and the
// NOTIFY that announces each message on `scadbuddy_mcp`. `PgRelayRows` is the agent's own SQL;
// `CrudRelayRows` is the same, one route per method, on the backend's CRUD routes of area
// `mcp-relay` (backend api/crud_agent_mcp.py, #5299 step 11), and main.ts picks it per call once
// the API lists that area (`gated`, api/crudStores.ts). A message's row and its NOTIFY commit in
// one transaction either way, so the row is readable before its header is heard.

/** The channel the relay's headers are NOTIFYed on. */
export const MCP_CHANNEL = 'scadbuddy_mcp'
/** Relay rows older than this are nobody's any more. */
const STALE_MESSAGES = '10 minutes'

export interface RelayRows {
  /** Records that `replica` holds the session (or moves it there). */
  register(hash: string, replica: string, callerKey: string): Promise<void>
  /** Moves the session's `last_seen` to now. */
  touch(hash: string): Promise<void>
  /** Deletes the session's row; with `replica`, only while that replica holds it. Whether one went. */
  remove(hash: string, replica?: string): Promise<boolean>
  /** The replica holding the session, or undefined. */
  owner(hash: string): Promise<string | undefined>
  /** Open sessions not idle for `idleSeconds`, and the caller's. */
  counts(callerKey: string, idleSeconds: number): Promise<{ total: number; caller: number }>
  /** Inserts `body` as row `body.row` (when given) and NOTIFYs `header`, in one transaction. */
  send(header: object, body?: { row: string; value: unknown }): Promise<void>
  /** Deletes row `row` and answers its body, or undefined when it was already taken. */
  take(row: string): Promise<unknown>
  /** Deletes row `row`; whether it was still there. */
  withdraw(row: string): Promise<boolean>
  /** Deletes the rows nobody took and the directory rows idle for `idleSeconds`. */
  sweep(idleSeconds: number): Promise<void>
}

export class PgRelayRows implements RelayRows {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async register(hash: string, replica: string, callerKey: string): Promise<void> {
    await this.#sql`
      INSERT INTO ai_mcp_sessions (id_hash, replica, caller_key)
      VALUES (${hash}, ${replica}, ${callerKey})
      ON CONFLICT (id_hash) DO UPDATE SET replica = EXCLUDED.replica, caller_key = EXCLUDED.caller_key, last_seen = now()`
  }

  async touch(hash: string): Promise<void> {
    await this.#sql`UPDATE ai_mcp_sessions SET last_seen = now() WHERE id_hash = ${hash}`
  }

  async remove(hash: string, replica?: string): Promise<boolean> {
    const removed =
      replica === undefined
        ? await this.#sql`DELETE FROM ai_mcp_sessions WHERE id_hash = ${hash} RETURNING id_hash`
        : await this.#sql`DELETE FROM ai_mcp_sessions WHERE id_hash = ${hash} AND replica = ${replica} RETURNING id_hash`
    return removed.length > 0
  }

  async owner(hash: string): Promise<string | undefined> {
    const [found] = await this.#sql<{ replica: string }[]>`SELECT replica FROM ai_mcp_sessions WHERE id_hash = ${hash}`
    return found?.replica
  }

  async counts(callerKey: string, idleSeconds: number): Promise<{ total: number; caller: number }> {
    const [row] = await this.#sql<{ total: number; caller: number }[]>`
      SELECT count(*)::int AS total, (count(*) FILTER (WHERE caller_key = ${callerKey}))::int AS caller
      FROM ai_mcp_sessions WHERE last_seen > now() - make_interval(secs => ${idleSeconds})`
    return row ?? { total: 0, caller: 0 }
  }

  async send(header: object, body?: { row: string; value: unknown }): Promise<void> {
    await this.#sql.begin(async (tx) => {
      if (body) await tx`INSERT INTO ai_mcp_relay_messages (id, body) VALUES (${body.row}, ${tx.json(body.value as never)})`
      await tx`SELECT pg_notify(${MCP_CHANNEL}, ${JSON.stringify(header)})`
    })
  }

  async take(row: string): Promise<unknown> {
    const [taken] = await this.#sql<{ body: unknown }[]>`DELETE FROM ai_mcp_relay_messages WHERE id = ${row} RETURNING body`
    return taken?.body
  }

  async withdraw(row: string): Promise<boolean> {
    return (await this.#sql`DELETE FROM ai_mcp_relay_messages WHERE id = ${row} RETURNING id`).length > 0
  }

  async sweep(idleSeconds: number): Promise<void> {
    await Promise.all([
      this.#sql`DELETE FROM ai_mcp_relay_messages WHERE created_at < now() - ${STALE_MESSAGES}::interval`,
      this.#sql`DELETE FROM ai_mcp_sessions WHERE last_seen < now() - make_interval(secs => ${idleSeconds})`,
    ])
  }
}

/** The CRUD routes' bound on an idle window (backend api/crud_agent_mcp.py `IDLE_SECONDS_MAX`). */
export const RELAY_IDLE_SECONDS_MAX = 7 * 24 * 3600

/**
 * The API's JSON gate (backend api/limits.py `MAX_TEXT_BODY_BYTES`). A relayed request is at
 * most the MCP SDK's 4 MiB, but the row escapes it once more, so quotes, backslashes and
 * control characters can take it past this.
 */
export const RELAY_SEND_MAX_BYTES = 8 * 1024 * 1024

/** A relay message too large for the API's JSON gate: refused here, before it is sent. */
export class RelayBodyTooLargeError extends Error {
  readonly bytes: number

  constructor(bytes: number) {
    super(`an MCP relay message of ${bytes} bytes is past the API's ${RELAY_SEND_MAX_BYTES}`)
    this.bytes = bytes
    this.name = 'RelayBodyTooLargeError'
  }
}

/** `RelayRows` over the backend's CRUD routes (area `mcp-relay`). Every id travels in the body. */
export class CrudRelayRows implements RelayRows {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async register(hash: string, replica: string, callerKey: string): Promise<void> {
    await this.#call("an MCP session's directory row", '/mcp-relay/sessions', {
      body: { hash, replica, caller_key: callerKey },
    })
  }

  async touch(hash: string): Promise<void> {
    await this.#call("an MCP session's use", '/mcp-relay/sessions/touch', { body: { hash } })
  }

  async remove(hash: string, replica?: string): Promise<boolean> {
    const answer = await this.#call<{ done: boolean }>("an MCP session's removal", '/mcp-relay/sessions/remove', {
      body: replica === undefined ? { hash } : { hash, replica },
    })
    return answer.done
  }

  async owner(hash: string): Promise<string | undefined> {
    const answer = await this.#call<{ replica: string | null }>("an MCP session's owner", '/mcp-relay/sessions/owner', { body: { hash } })
    return answer.replica ?? undefined
  }

  async counts(callerKey: string, idleSeconds: number): Promise<{ total: number; caller: number }> {
    return await this.#call('the open MCP sessions', '/mcp-relay/counts', {
      body: { caller_key: callerKey, idle_seconds: idleSeconds },
    })
  }

  async send(header: object, body?: { row: string; value: unknown }): Promise<void> {
    const sent = body ? { header, body: body.value } : { header }
    const bytes = Buffer.byteLength(JSON.stringify(sent), 'utf8')
    if (bytes > RELAY_SEND_MAX_BYTES) throw new RelayBodyTooLargeError(bytes)
    await this.#call('an MCP relay message', '/mcp-relay/send', { body: sent })
  }

  async take(row: string): Promise<unknown> {
    const answer = await this.#call<{ body: unknown }>('an MCP relay message', '/mcp-relay/rows/take', { body: { row } })
    return answer.body ?? undefined
  }

  async withdraw(row: string): Promise<boolean> {
    return (await this.#call<{ done: boolean }>("an MCP relay message's withdrawal", '/mcp-relay/rows/withdraw', { body: { row } })).done
  }

  async sweep(idleSeconds: number): Promise<void> {
    await this.#call("the MCP relay's sweep", '/mcp-relay/sweep', { body: { idle_seconds: idleSeconds } })
  }
}
