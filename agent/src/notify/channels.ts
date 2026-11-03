import { randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { assertHostAllowed, EgressError, isPrivateAddress, type Resolver, systemResolver } from '../http/egress.js'
import { isLoopbackPeer } from '../http/origins.js'
import { pinnedFetch } from '../http/pinned.js'
import { type Envelope, type Kek, last4, openSecret, rewrap, SealError, sealSecret } from '../secrets.js'

// #815 §3, out of the browser: an ntfy topic or a Home Assistant notify service, set in
// Settings (routes/notifyChannels.ts; the agent has no tool for them, so it cannot add a
// place to send to). The dispatcher (dispatcher.ts) tells each enabled channel of every
// attention request once, with the request's reason as the title, a fixed line and a link
// to the session: never the agent's message, since an ntfy topic or a phone's lock screen
// is no place for what a conversation said.
//
// A channel's URL is the person's own choice, like a plugin's MCP endpoint, so it follows
// the same rule (plugins/registry.ts `assertEndpointAllowed`): never link-local or a cloud
// metadata host; https anywhere; plain http only when every address is loopback or private
// (a Home Assistant or ntfy server on the LAN). It is checked when saved and at every send,
// and the send connects to the address checked, following no redirect.
//
// ntfy: POST <server>/<topic>, the line as the body, Title/Click/Priority headers and an
// optional `Authorization: Bearer` access token (https://docs.ntfy.sh/publish/).
// Home Assistant: POST <base>/api/services/notify/<service> with {title, message, data}
// and a long-lived access token (https://developers.home-assistant.io/docs/api/rest/,
// https://www.home-assistant.io/integrations/notify/). Neither was run against a real server
// here; the tests use fakes on loopback.

export const CHANNEL_KINDS = ['ntfy', 'home_assistant'] as const
export type ChannelKind = (typeof CHANNEL_KINDS)[number]

export type ChannelInput = {
  kind: ChannelKind
  name: string
  url: string
  /** Home Assistant's notify service, `mobile_app_<device>`; none for ntfy. */
  target?: string | null
  /** ntfy's access token (optional) or Home Assistant's long-lived token (required). */
  token?: string | null
  enabled?: boolean
}

export type ChannelPatch = Partial<ChannelInput>

/** A channel as the routes show it: never the token, only whether there is one and its last four. */
export type ChannelView = {
  id: string
  kind: ChannelKind
  name: string
  url: string
  target: string | null
  enabled: boolean
  has_token: boolean
  token_last4: string
  last_error: string | null
  last_sent_at: string | null
}

/** What a channel is sent: a title, a line, and where a click goes. */
export type ChannelMessage = { title: string; body: string; click?: string }

export class ChannelError extends Error {
  override name = 'ChannelError'
  readonly status: 400 | 404
  constructor(message: string, status: 400 | 404 = 400) {
    super(message)
    this.status = status
  }
}

type Row = {
  id: string
  kind: ChannelKind
  name: string
  url: string
  target: string | null
  enabled: boolean
  secret_sealed: Buffer | null
  dek_sealed: Buffer | null
  kek_id: string | null
  token_last4: string
  last_error: string | null
  last_sent_at: Date | null
}

const aad = (id: string, kind: string, url: string) => `notify-channel:${id}:${kind}:${url}`
const SERVICE = /^[a-z0-9_]{1,100}$/
const TOKEN_MAX = 4096

/** A channel with its token opened, for a send. */
export type OpenChannel = { id: string; kind: ChannelKind; url: string; target: string | null; token: string | undefined }

export class ChannelStore {
  readonly #sql: Sql
  readonly #kek: Kek
  readonly #resolve: Resolver

  constructor(sql: Sql, kek: Kek, resolve: Resolver = systemResolver) {
    this.#sql = sql
    this.#kek = kek
    this.#resolve = resolve
  }

  async list(): Promise<ChannelView[]> {
    const rows = await this.#sql<Row[]>`SELECT * FROM ai_notify_channels ORDER BY created_at, id`
    return rows.map(view)
  }

  async create(input: ChannelInput): Promise<string> {
    const id = randomUUID()
    const checked = await this.#check(input)
    const sealed = checked.token ? sealSecret(this.#kek, checked.token, aad(id, checked.kind, checked.url)) : undefined
    await this.#sql`
      INSERT INTO ai_notify_channels (id, kind, name, url, target, enabled, secret_sealed, dek_sealed, kek_id, token_last4)
      VALUES (${id}, ${checked.kind}, ${checked.name}, ${checked.url}, ${checked.target}, ${input.enabled ?? true},
              ${sealed?.secretSealed ?? null}, ${sealed?.dekSealed ?? null}, ${sealed?.kekId ?? null}, ${checked.token ? last4(checked.token) : ''})`
    return id
  }

  /**
   * Changes a channel. A token is bound to the kind and URL it was entered for, so changing
   * either drops it unless the patch gives one; `token: null` drops it too.
   */
  async update(id: string, patch: ChannelPatch): Promise<void> {
    const [row] = await this.#sql<Row[]>`SELECT * FROM ai_notify_channels WHERE id = ${id}`
    if (!row) throw new ChannelError(`no channel ${id}`, 404)
    const kind = patch.kind ?? row.kind
    const url = patch.url ?? row.url
    const moved = kind !== row.kind || url !== row.url
    let token: string | undefined
    if (typeof patch.token === 'string') token = patch.token
    else if (patch.token === undefined && !moved && row.secret_sealed) token = this.#open(row)
    const checked = await this.#check({
      kind,
      name: patch.name ?? row.name,
      url,
      target: patch.target === undefined ? row.target : patch.target,
      token: token ?? null,
    })
    const sealed = checked.token ? sealSecret(this.#kek, checked.token, aad(id, checked.kind, checked.url)) : undefined
    await this.#sql`
      UPDATE ai_notify_channels SET kind = ${checked.kind}, name = ${checked.name}, url = ${checked.url}, target = ${checked.target},
             enabled = ${patch.enabled ?? row.enabled},
             secret_sealed = ${sealed?.secretSealed ?? null}, dek_sealed = ${sealed?.dekSealed ?? null}, kek_id = ${sealed?.kekId ?? null},
             token_last4 = ${checked.token ? last4(checked.token) : ''}, last_error = NULL
       WHERE id = ${id}`
  }

  async delete(id: string): Promise<boolean> {
    return (await this.#sql`DELETE FROM ai_notify_channels WHERE id = ${id}`).count > 0
  }

  /** One channel with its token, or undefined. A token this KEK cannot open is a ChannelError. */
  async open(id: string): Promise<OpenChannel | undefined> {
    const [row] = await this.#sql<Row[]>`SELECT * FROM ai_notify_channels WHERE id = ${id}`
    return row ? this.#openRow(row) : undefined
  }

  /** The enabled channels whose tokens this KEK opens. */
  async enabled(): Promise<OpenChannel[]> {
    const rows = await this.#sql<Row[]>`SELECT * FROM ai_notify_channels WHERE enabled ORDER BY created_at, id`
    return rows.flatMap((row) => {
      try {
        return [this.#openRow(row)]
      } catch {
        return []
      }
    })
  }

  /** What the last send said: null when it went through. */
  async record(id: string, error: string | null): Promise<void> {
    if (error === null) await this.#sql`UPDATE ai_notify_channels SET last_error = NULL, last_sent_at = now() WHERE id = ${id}`
    else await this.#sql`UPDATE ai_notify_channels SET last_error = ${error.slice(0, 500)} WHERE id = ${id}`
  }

  /** Sends `message` to `channel`, checking its URL again first. Throws EgressError or ChannelError on a failure. */
  async send(channel: OpenChannel, message: ChannelMessage, signal?: AbortSignal): Promise<void> {
    const target = channel.kind === 'ntfy' ? channel.url : `${channel.url.replace(/\/+$/, '')}/api/services/notify/${channel.target}`
    const address = (await allowedUrl(target, this.#resolve))[0]!
    const auth = channel.token ? { authorization: `Bearer ${channel.token}` } : {}
    const request =
      channel.kind === 'ntfy'
        ? {
            headers: { ...auth, title: message.title, priority: 'high', 'content-type': 'text/plain; charset=utf-8', ...(message.click ? { click: message.click } : {}) },
            body: message.body,
          }
        : {
            headers: { ...auth, 'content-type': 'application/json' },
            body: JSON.stringify({
              title: message.title,
              message: message.body,
              ...(message.click ? { data: { url: message.click, clickAction: message.click } } : {}),
            }),
          }
    const response = await pinnedFetch(target, address, {
      method: 'POST',
      ...request,
      signal: signal ?? AbortSignal.timeout(10_000),
      maxBytes: 64 * 1024,
    })
    if (response.status < 200 || response.status >= 300) throw new ChannelError(`HTTP ${response.status}`)
  }

  /** Key rotation (spec §9): each channel token's data key, re-sealed under `current`. */
  async rewrapFrom(previous: Kek, current: Kek): Promise<{ rewrapped: number; failed: number }> {
    const result = { rewrapped: 0, failed: 0 }
    if (previous.id === current.id) return result
    const rows = await this.#sql<Row[]>`SELECT * FROM ai_notify_channels WHERE kek_id = ${previous.id}`
    for (const row of rows) {
      let next: Envelope
      try {
        next = rewrap(previous, current, envelope(row), aad(row.id, row.kind, row.url))
      } catch (err) {
        if (!(err instanceof SealError)) throw err
        result.failed++
        continue
      }
      const updated = await this.#sql`
        UPDATE ai_notify_channels SET dek_sealed = ${next.dekSealed}, kek_id = ${next.kekId} WHERE id = ${row.id} AND kek_id = ${previous.id}`
      result.rewrapped += updated.count
    }
    return result
  }

  #open(row: Row): string {
    return openSecret(this.#kek, envelope(row), aad(row.id, row.kind, row.url))
  }

  #openRow(row: Row): OpenChannel {
    return { id: row.id, kind: row.kind, url: row.url, target: row.target, token: row.secret_sealed ? this.#open(row) : undefined }
  }

  async #check(input: ChannelInput): Promise<{ kind: ChannelKind; name: string; url: string; target: string | null; token: string | null }> {
    if (!CHANNEL_KINDS.includes(input.kind)) throw new ChannelError(`kind must be one of ${CHANNEL_KINDS.join(', ')}`)
    const name = input.name.trim()
    if (name.length < 1 || name.length > 100) throw new ChannelError('name must be 1 to 100 characters')
    let url: URL
    try {
      url = new URL(input.url)
    } catch {
      throw new ChannelError('url is not a URL')
    }
    if (url.username || url.password) throw new ChannelError('url must not carry credentials')
    if (url.search || url.hash) throw new ChannelError('url must have no query or fragment')
    const text = url.href
    if (text.length > 2048) throw new ChannelError('url is longer than 2048 characters')
    if (input.kind === 'ntfy' && url.pathname.replace(/\/+$/, '') === '') throw new ChannelError('an ntfy url names its topic: https://ntfy.sh/<topic>')
    const token = input.token ?? null
    if (token !== null && (token.length === 0 || token.length > TOKEN_MAX || /[\r\n]/.test(token))) {
      throw new ChannelError(`token must be 1 to ${TOKEN_MAX} characters on one line`)
    }
    let target: string | null = null
    if (input.kind === 'home_assistant') {
      target = input.target ?? ''
      if (!SERVICE.test(target)) throw new ChannelError('target must be a Home Assistant notify service name, such as mobile_app_pixel')
      if (token === null) throw new ChannelError('Home Assistant needs a long-lived access token')
    }
    try {
      await allowedUrl(text, this.#resolve)
    } catch (err) {
      if (err instanceof EgressError) throw new ChannelError(err.message)
      throw err
    }
    return { kind: input.kind, name, url: input.kind === 'ntfy' ? text : text.replace(/\/+$/, ''), target, token }
  }
}

function envelope(row: Row): Envelope {
  return { secretSealed: row.secret_sealed!, dekSealed: row.dek_sealed!, kekId: row.kek_id! }
}

function view(row: Row): ChannelView {
  return {
    id: row.id,
    kind: row.kind,
    name: row.name,
    url: row.url,
    target: row.target,
    enabled: row.enabled,
    has_token: row.secret_sealed !== null,
    token_last4: row.token_last4,
    last_error: row.last_error,
    last_sent_at: row.last_sent_at?.toISOString() ?? null,
  }
}

/** https anywhere; http only when every address is loopback or private; never link-local or metadata. */
async function allowedUrl(url: string, resolve: Resolver): Promise<string[]> {
  const parsed = new URL(url)
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') throw new EgressError('url must be http or https')
  const addresses = await assertHostAllowed(url, resolve, 'url', 'a notification service')
  if (parsed.protocol === 'http:' && !addresses.every((a) => isLoopbackPeer(a) || isPrivateAddress(a))) {
    throw new EgressError(`url must be https: plain http is allowed only to loopback or private addresses, and ${parsed.hostname} is not`)
  }
  return addresses
}
