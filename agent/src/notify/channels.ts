import { assertHostAllowed, EgressError, isPrivateAddress, type Resolver, systemResolver } from '../http/egress.js'
import { isLoopbackPeer } from '../http/origins.js'
import { pinnedFetch } from '../http/pinned.js'
import { CrudReadError } from '../api/crudRead.js'
import { type CrudTarget, crudOf, segment, UUID_SEGMENT } from '../api/crudStores.js'

// #815 §3, out of the browser: an ntfy topic or a Home Assistant notify service, set in
// Settings (routes/notifyChannels.ts; the agent has no tool for them, so it cannot add a
// place to send to). The dispatcher (dispatcher.ts) tells each enabled channel of every
// request waiting on the person (an approval, a question or an attention request) once,
// with the request's kind as the title, a fixed line and a link
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

const SERVICE = /^[a-z0-9_]{1,100}$/
const TOKEN_MAX = 4096

/** A channel with its token opened, for a send. */
export type OpenChannel = { id: string; kind: ChannelKind; url: string; target: string | null; token: string | undefined }

/** A channel's fields as `ChannelStore` checked them. */
export type CheckedChannel = { kind: ChannelKind; name: string; url: string; target: string | null; token: string | null }

/** The channels, kept by the API (area `notify`): it seals and opens the tokens with its KEK. */
export class ChannelStore {
  readonly #rows: CrudChannelRows
  readonly #resolve: Resolver

  constructor(rows: CrudChannelRows, resolve: Resolver = systemResolver) {
    this.#rows = rows
    this.#resolve = resolve
  }

  async list(): Promise<ChannelView[]> {
    return this.#rows.list()
  }

  async create(input: ChannelInput): Promise<string> {
    return this.#rows.create(await this.#check(input), input.enabled ?? true)
  }

  /**
   * Changes a channel. A token is bound to the kind and URL it was entered for, so changing
   * either drops it unless the patch gives one; `token: null` drops it too. The API keeps the
   * sealed token while the kind and URL stand.
   */
  async update(id: string, patch: ChannelPatch): Promise<void> {
    const row = await this.#rows.view(id)
    if (!row) throw new ChannelError(`no channel ${id}`, 404)
    const kind = patch.kind ?? row.kind
    const url = patch.url ?? row.url
    const moved = kind !== row.kind || url !== row.url
    const keep = patch.token === undefined && !moved && row.has_token
    const fields = { kind, name: patch.name ?? row.name, url, target: patch.target === undefined ? row.target : patch.target }
    const checked = await this.#check({ ...fields, token: typeof patch.token === 'string' ? patch.token : null }, keep)
    if (!(await this.#rows.update(id, checked, patch.enabled ?? row.enabled, keep))) throw new ChannelError(`no channel ${id}`, 404)
  }

  async delete(id: string): Promise<boolean> {
    return this.#rows.delete(id)
  }

  /** One channel with its token, or undefined. A token the API's KEK cannot open is a ChannelError. */
  async open(id: string): Promise<OpenChannel | undefined> {
    return this.#rows.open(id)
  }

  /** The enabled channels whose tokens the API's KEK opens. */
  async enabled(): Promise<OpenChannel[]> {
    return this.#rows.enabled()
  }

  /** What the last send said: null when it went through. */
  async record(id: string, error: string | null): Promise<void> {
    return this.#rows.record(id, error)
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

  /** `keepToken`: the channel keeps the token it holds (checked when it was entered), so `input.token` is none. */
  async #check(input: ChannelInput, keepToken = false): Promise<CheckedChannel> {
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
      if (token === null && !keepToken) throw new ChannelError('Home Assistant needs a long-lived access token')
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

/** The route's reason for a channel moved under an update that kept its token (backend api/crud_agent_notify.py). */
const CHANNEL_MOVED = 'channel-moved'

/**
 * The channels' rows through the API's CRUD routes, area `notify` (backend
 * api/crud_agent_notify.py): the API seals a token with its KEK under this file's AAD, and
 * opens it only for a send (`open`, `enabled`). `ChannelStore` still checks each channel, its
 * URL's egress rules included, and sends.
 */
export class CrudChannelRows {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async list(): Promise<ChannelView[]> {
    return (await this.#call<{ channels: ChannelView[] }>('the notification channels', '/notify-channels')).channels.map(viewOfWire)
  }

  async view(id: string): Promise<ChannelView | undefined> {
    if (!UUID.test(id)) return undefined
    const { channel } = await this.#call<{ channel: ChannelView | null }>('a notification channel', `/notify-channels/${segment(id, UUID_SEGMENT, 'a channel id')}`)
    return channel ? viewOfWire(channel) : undefined
  }

  async create(checked: CheckedChannel, enabled: boolean): Promise<string> {
    const body = { ...fieldsOf(checked), enabled, ...(checked.token ? { token: checked.token } : {}) }
    return (await this.#call<{ id: string }>('a notification channel created', '/notify-channels', { body })).id
  }

  /** Whether the channel was there. `keepToken`: it keeps its sealed token. */
  async update(id: string, checked: CheckedChannel, enabled: boolean, keepToken: boolean): Promise<boolean> {
    if (!UUID.test(id)) return false
    const body = { ...fieldsOf(checked), enabled, ...(keepToken ? { keep_token: true } : checked.token ? { token: checked.token } : {}) }
    try {
      return (await this.#call<{ done: boolean }>('a notification channel changed', `/notify-channels/${segment(id, UUID_SEGMENT, 'a channel id')}/update`, { body })).done
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.reason === CHANNEL_MOVED) throw new ChannelError(err.detail)
      throw err
    }
  }

  async delete(id: string): Promise<boolean> {
    if (!UUID.test(id)) return false
    return (await this.#call<{ done: boolean }>('a notification channel deleted', `/notify-channels/${segment(id, UUID_SEGMENT, 'a channel id')}/delete`, { method: 'POST' })).done
  }

  async open(id: string): Promise<OpenChannel | undefined> {
    if (!UUID.test(id)) return undefined
    try {
      const { channel } = await this.#call<{ channel: OpenChannel | null }>('a notification channel opened', `/notify-channels/${segment(id, UUID_SEGMENT, 'a channel id')}/open`, { method: 'POST' })
      return channel ? { ...channel, token: channel.token ?? undefined } : undefined
    } catch (err) {
      // A token the API's key does not open.
      if (err instanceof CrudReadError && err.fromRoute && err.status === 409) throw new ChannelError(err.detail)
      throw err
    }
  }

  async enabled(): Promise<OpenChannel[]> {
    const { channels } = await this.#call<{ channels: (Omit<OpenChannel, 'token'> & { token: string | null })[] }>(
      'the enabled notification channels',
      '/notify-channels/open-enabled',
      { method: 'POST' },
    )
    return channels.map((c) => ({ ...c, token: c.token ?? undefined }))
  }

  async record(id: string, error: string | null): Promise<void> {
    await this.#call('a notification sent', `/notify-channels/${segment(id, UUID_SEGMENT, 'a channel id')}/record`, { body: { error } })
  }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function fieldsOf(checked: CheckedChannel): { kind: ChannelKind; name: string; url: string; target: string | null } {
  return { kind: checked.kind, name: checked.name, url: checked.url, target: checked.target }
}

/** A view with the send time to the millisecond. */
function viewOfWire(v: ChannelView): ChannelView {
  return { ...v, last_sent_at: v.last_sent_at === null ? null : new Date(v.last_sent_at).toISOString() }
}
