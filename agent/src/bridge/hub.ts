import { randomUUID } from 'node:crypto'
import type { Principal } from '../auth/principal.js'
import { type PairingRequest, PairingError, type PairingStore, type PairingView } from './pairings.js'
import { agentFrame, type AgentFrame, type CallOutcome, type PairingEntry, parseTabFrame, type TabFrame } from './protocol.js'
import type { RelayAnswer, RelayRequest, TabRelay } from './relay.js'
import type { SessionTabStore } from './sessionTabs.js'

// The agent's side of the browser bridge (#254, spec §5.2): the tabs connected
// to this process over `GET /api/v1/ai/bridge` (routes/bridge.ts), and the
// forwarding the browser_* tools (tools/browser.ts) do through them. "The SDK
// runs tools in the service process, so a browser tool is an ordinary registry
// tool whose handler forwards the call over the paired tab's WebSocket and
// awaits the result, with a timeout. If no tab is paired it returns an error
// ('no browser attached')." (spec §5.2; the SDK side is
// https://code.claude.com/docs/en/agent-sdk/custom-tools).
//
// Which tab a call goes to (spec §8.5):
//   - a session's turn: the tab the browser user chats with it from. The chat
//     socket names its tab (`tab.bind`, routes/chat.ts) and each message the
//     user sends from it pairs the session with that tab (`pairSession`);
//   - an MCP principal (over /mcp, or a session it owns): the tab the user
//     paired it with by typing its code (bridge/pairings.ts).
//
// The tabs are held in this process. A classic turn runs on the replica whose
// chat socket started it, a durable session's tool call on whichever replica's
// `agent-tools` worker took the activity (so the session's tab is also kept in
// Postgres, bridge/sessionTabs.ts, #2086), and an MCP call on the replica that
// holds its session; so the
// tab a call is for may be connected to another replica: then the call goes to
// that replica through Postgres (bridge/relay.ts, #1916), and it answers "not
// connected" only when no replica holds the tab (docs/ai/browser-bridge.md,
// "Replicas"). Pairing requests reach every tab whatever the replica: each
// replica re-reads them from Postgres every PAIRINGS_POLL_MS.

/** How long a call waits for the tab when the tool names no wait of its own. */
export const CALL_TIMEOUT_MS = 30_000
/** How often connected tabs are sent the pairing requests, when they changed. */
export const PAIRINGS_POLL_MS = 3_000
/** Calls one tab may have waiting at once. */
export const MAX_CALLS_PER_TAB = 8
/** Sessions whose tab is remembered; the least recently paired is forgotten past this. */
export const MAX_SESSION_TABS = 1_000

/** Codes the hub answers with itself, beside the tab's own (frontend src/agent/types.ts `ErrorCode`). */
export type HubErrorCode = 'no_browser' | 'no_answer' | 'disconnected' | 'busy'

/** Who a browser_* call acts for: the caller, and the session when it comes from a turn. */
export type BrowserTarget = { principal: Principal; sessionId?: string | undefined }

export type BrowserStatus =
  | { attached: true; via: 'session' | 'pairing'; route: string; live: string[]; pairing?: PairingView }
  | { attached: false; reason: string }

/** What the browser_* tools need (ToolServices `browser`). */
export interface BrowserTabs {
  call(
    target: BrowserTarget,
    tool: string,
    args: Record<string, unknown>,
    options: { signal: AbortSignal; timeoutMs?: number },
  ): Promise<CallOutcome>
  /** `signal` cancels the lookup (a pairing read that waits on the pool or a lock). */
  status(target: BrowserTarget, options?: { signal?: AbortSignal }): Promise<BrowserStatus>
  pair(principal: Principal): Promise<PairingRequest>
  /** The same tabs, with every call's target naming `sessionId` (tools/harness.ts binds each turn's). */
  forSession(sessionId: string): BrowserTabs
}

type PendingCall = { resolve: (outcome: CallOutcome) => void; timer: NodeJS.Timeout; cleanup: () => void }

function failure(code: HubErrorCode, message: string): CallOutcome {
  return { ok: false, error: { code, message } }
}

const NO_SESSION_TAB =
  'no browser attached: no ScadBuddy tab is paired with this session. A chat session pairs with the tab the user ' +
  'sends its messages from; ask the user to open ScadBuddy and send a message from the assistant panel there.'
const NO_PAIRING =
  'no browser attached: no ScadBuddy tab is paired with this caller. Call browser_pair, give the user the code it ' +
  'returns, and ask them to type it into the prompt that appears in their ScadBuddy tab.'
const NO_STORE =
  'no browser attached: pairing an agent with a tab needs the database (SCADBUDDY_DATABASE_URL, spec §9)'
const NOT_CONNECTED =
  'no browser attached: the paired ScadBuddy tab is not connected (it was closed or reloaded, or lost its ' +
  'connection). Ask the user to open ScadBuddy again; an agent that paired by code must then pair again with ' +
  'browser_pair.'

export type TabHubOptions = {
  /** Undefined without a database: only chat sessions can reach a tab then. */
  pairings?: PairingStore | undefined
  /** Undefined without a database: only tabs connected to this process can be reached then. */
  relay?: TabRelay | undefined
  /**
   * Chat sessions' tabs, for a call that runs on a replica the session's
   * messages never reached (a durable session's tool activity, #2086).
   * Undefined without a database: a session's tab is known only where it paired.
   */
  sessionTabs?: SessionTabStore | undefined
  callTimeoutMs?: number
  pollMs?: number
  log?: (message: string) => void
}

/** One tab's socket, independent of the socket so it can be tested without one. */
export class TabConnection {
  tabId: string | undefined
  route = ''
  live: string[] = []
  readonly #hub: TabHub
  readonly #send: (frame: AgentFrame) => void
  readonly #onReplaced: () => void
  readonly calls = new Map<string, PendingCall>()
  #closed = false
  /** The last `pairings` frame sent, as JSON, so an unchanged one is not sent again. */
  lastPairings = ''

  constructor(hub: TabHub, send: (frame: AgentFrame) => void, onReplaced: () => void) {
    this.#hub = hub
    this.#send = send
    this.#onReplaced = onReplaced
  }

  get closed(): boolean {
    return this.#closed
  }

  send(frame: AgentFrame): void {
    if (!this.#closed) this.#send(frame)
  }

  /** One text frame from the tab (or whatever else arrived, which is refused). */
  async receive(raw: unknown): Promise<void> {
    if (this.#closed) return
    if (typeof raw !== 'string') return this.send(agentFrame({ type: 'error', message: 'frames must be JSON text' }))
    const parsed = parseTabFrame(raw)
    if (!parsed.ok) return this.send(agentFrame({ type: 'error', message: `ignored a malformed frame: ${parsed.error}` }))
    try {
      await this.#handle(parsed.value)
    } catch (err) {
      // A database blip while answering a pairing: ours to log, and the tab is told only that it failed.
      this.#hub.logError(err)
      this.send(agentFrame({ type: 'error', message: 'the agent service could not do that; see its log' }))
    }
  }

  async #handle(frame: TabFrame): Promise<void> {
    if (frame.type === 'hello') {
      if (this.tabId !== undefined) return this.send(agentFrame({ type: 'error', message: 'hello was already sent' }))
      this.tabId = frame.tabId
      this.route = frame.route
      this.live = frame.live
      this.#hub.attach(this)
      return
    }
    if (this.tabId === undefined) return this.send(agentFrame({ type: 'error', message: 'send hello first' }))
    switch (frame.type) {
      case 'state':
        this.route = frame.route
        this.live = frame.live
        return
      case 'result': {
        const call = this.calls.get(frame.id)
        if (!call) return // late: timed out or cancelled already
        call.cleanup()
        call.resolve(frame.outcome)
        return
      }
      case 'pairing.accept':
        return this.#hub.acceptPairing(this, frame.id, frame.code)
      case 'pairing.deny':
        return this.#hub.denyPairing(frame.id)
      case 'pairing.end':
        return this.#hub.endPairing(this, frame.id)
    }
  }

  /** Another connection took this tab id over (a reconnect overtook this one). */
  replaced(): void {
    this.close('the tab connected again')
    this.#onReplaced()
  }

  close(why = 'the tab disconnected before answering'): void {
    if (this.#closed) return
    this.#closed = true
    for (const call of [...this.calls.values()]) {
      call.cleanup()
      call.resolve(failure('disconnected', `the call did not finish: ${why}`))
    }
    this.#hub.detach(this)
  }
}

export class TabHub implements BrowserTabs {
  readonly #tabs = new Map<string, TabConnection>()
  /** sessionId → tabId, least recently paired first (Map keeps insertion order). */
  readonly #sessionTabs = new Map<string, string>()
  readonly #pairings: PairingStore | undefined
  readonly #relay: TabRelay | undefined
  readonly #sessionTabStore: SessionTabStore | undefined
  #sessionTabWrites: Promise<void> = Promise.resolve()
  readonly #callTimeoutMs: number
  readonly #pollMs: number
  readonly #log: (message: string) => void
  #poll: NodeJS.Timeout | undefined
  #refreshing: Promise<void> | undefined
  /**
   * #815 §2: told each time a session gets a connected tab again: its tab
   * reconnected, or the user opened it from another tab (`pairSession`). main.ts
   * resolves the session's `tab_disconnected` attention requests with it.
   */
  onSessionTab: ((sessionId: string) => Promise<unknown>) | undefined

  constructor(options: TabHubOptions = {}) {
    this.#pairings = options.pairings
    this.#relay = options.relay
    this.#sessionTabStore = options.sessionTabs
    this.#relay?.serve({ holds: (tabId) => this.#tabs.has(tabId), run: (tabId, request) => this.#runHere(tabId, request) })
    this.#callTimeoutMs = options.callTimeoutMs ?? CALL_TIMEOUT_MS
    this.#pollMs = options.pollMs ?? PAIRINGS_POLL_MS
    this.#log = options.log ?? ((m) => console.error(m))
  }

  /** A new socket's connection; `onReplaced` closes the socket when a reconnect takes its tab id. */
  open(send: (frame: AgentFrame) => void, onReplaced: () => void = () => {}): TabConnection {
    return new TabConnection(this, send, onReplaced)
  }

  logError(err: unknown): void {
    this.#log(`bridge: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}`)
  }

  /** Tab ids connected to this process now (for tests and status). */
  connected(): string[] {
    return [...this.#tabs.keys()]
  }

  attach(connection: TabConnection): void {
    const tabId = connection.tabId!
    const before = this.#tabs.get(tabId)
    this.#tabs.set(tabId, connection)
    if (before && before !== connection) before.replaced()
    for (const [sessionId, paired] of this.#sessionTabs) if (paired === tabId) this.#sessionTabBack(sessionId)
    if (!this.#poll && this.#pairings) {
      this.#poll = setInterval(() => void this.refresh(), this.#pollMs)
      this.#poll.unref()
    }
    void this.refresh()
  }

  detach(connection: TabConnection): void {
    if (connection.tabId !== undefined && this.#tabs.get(connection.tabId) === connection) {
      this.#tabs.delete(connection.tabId)
    }
    if (this.#tabs.size === 0 && this.#poll) {
      clearInterval(this.#poll)
      this.#poll = undefined
    }
  }

  /** The browser user sent a message to `sessionId` from tab `tabId` (routes/chat.ts). */
  pairSession(sessionId: string, tabId: string): void {
    const before = this.#sessionTabs.get(sessionId)
    // Written when it changes, not on every message: other replicas read it (#2086).
    if (before !== tabId && this.#sessionTabStore) {
      const store = this.#sessionTabStore
      // One write at a time, in pairing order: two in flight could commit out of
      // order and leave the earlier tab stored.
      this.#sessionTabWrites = this.#sessionTabWrites.then(() =>
        store.set(sessionId, tabId).catch((err: unknown) => this.logError(err)),
      )
    }
    this.#sessionTabs.delete(sessionId)
    this.#sessionTabs.set(sessionId, tabId)
    while (this.#sessionTabs.size > MAX_SESSION_TABS) {
      const oldest = this.#sessionTabs.keys().next().value as string
      this.#sessionTabs.delete(oldest)
    }
    if (this.#tabs.has(tabId)) this.#sessionTabBack(sessionId)
  }

  #sessionTabBack(sessionId: string): void {
    const listener = this.onSessionTab
    if (!listener) return
    void listener(sessionId).catch((err: unknown) => this.logError(err))
  }

  /** Whether `sessionId` has a tab that is connected here now. */
  sessionHasTab(sessionId: string): boolean {
    const tabId = this.#sessionTabs.get(sessionId)
    return tabId !== undefined && this.#tabs.has(tabId)
  }

  forSession(sessionId: string): BrowserTabs {
    return {
      call: (target, tool, args, options) => this.call({ ...target, sessionId }, tool, args, options),
      status: (target, options) => this.status({ ...target, sessionId }, options),
      pair: (principal) => this.pair(principal),
      forSession: (other) => this.forSession(other),
    }
  }

  async #resolve(
    target: BrowserTarget,
    signal?: AbortSignal,
  ): Promise<Resolved | { problem: string }> {
    if (target.sessionId !== undefined) {
      const tabId = this.#sessionTabs.get(target.sessionId) ?? (await this.#storedSessionTab(target.sessionId, signal))
      if (tabId !== undefined) return this.#reach(tabId, { via: 'session' })
    }
    if (target.principal.kind === 'browser') return { problem: NO_SESSION_TAB }
    if (!this.#pairings) return { problem: NO_STORE }
    const paired = await this.#pairings.pairedTab(target.principal, signal)
    if (!paired) return { problem: NO_PAIRING }
    return this.#reach(paired.tabId, { via: 'pairing', pairing: paired })
  }

  /** The tab another replica paired `sessionId` with; undefined when none did or it cannot be read. */
  async #storedSessionTab(sessionId: string, signal?: AbortSignal): Promise<string | undefined> {
    if (!this.#sessionTabStore) return undefined
    try {
      return await this.#sessionTabStore.get(sessionId, signal)
    } catch (err) {
      if (signal?.aborted) throw err
      this.logError(err)
      return undefined
    }
  }

  /** The tab here, or its id for the relay to find; not connected when neither. */
  #reach(tabId: string, how: Omit<Resolved, 'tab' | 'tabId'>): Resolved | { problem: string } {
    const tab = this.#tabs.get(tabId)
    if (tab) return { ...how, tab, tabId }
    return this.#relay ? { ...how, tabId } : { problem: NOT_CONNECTED }
  }

  /** A request forwarded from another replica (bridge/relay.ts) for a tab held here. */
  async #runHere(tabId: string, request: RelayRequest): Promise<RelayAnswer> {
    const tab = this.#tabs.get(tabId)
    if (!tab) return { op: 'gone' }
    if (request.op === 'status') return { op: 'status', route: tab.route, live: tab.live }
    // The caller stops waiting on its own replica; the tab is not told, as for a local call.
    const outcome = await this.#callTab(tab, request.tool, request.args, new AbortController().signal, request.timeoutMs)
    return { op: 'call', outcome }
  }

  async call(
    target: BrowserTarget,
    tool: string,
    args: Record<string, unknown>,
    { signal, timeoutMs = this.#callTimeoutMs }: { signal: AbortSignal; timeoutMs?: number },
  ): Promise<CallOutcome> {
    // The pairing lookup stops with the call (#1410): a hung read is not waited out.
    const resolved = await this.#resolve(target, signal).catch((err: unknown) => {
      throw signal.aborted ? abortError() : err
    })
    if ('problem' in resolved) return failure('no_browser', resolved.problem)
    const { tab } = resolved
    if (tab) return this.#callTab(tab, tool, args, signal, timeoutMs)
    const answer = await this.#relay!.forward(resolved.tabId, { op: 'call', tool, args, timeoutMs }, { signal, timeoutMs })
    return answer?.op === 'call' ? answer.outcome : failure('no_browser', NOT_CONNECTED)
  }

  async #callTab(
    tab: TabConnection,
    tool: string,
    args: Record<string, unknown>,
    signal: AbortSignal,
    timeoutMs: number,
  ): Promise<CallOutcome> {
    if (tab.calls.size >= MAX_CALLS_PER_TAB) {
      return failure('busy', `the tab already has ${MAX_CALLS_PER_TAB} calls running; wait for them to finish`)
    }
    if (signal.aborted) throw abortError()
    const id = randomUUID()
    return new Promise<CallOutcome>((resolve, reject) => {
      const onAbort = () => {
        cleanup()
        reject(abortError())
      }
      const cleanup = () => {
        clearTimeout(timer)
        signal.removeEventListener('abort', onAbort)
        tab.calls.delete(id)
      }
      const timer = setTimeout(() => {
        cleanup()
        resolve(
          failure(
            'no_answer',
            `the tab did not answer ${tool} within ${Math.round(timeoutMs / 1000)} s; it may still finish, so ` +
              'take a snapshot before trying again',
          ),
        )
      }, timeoutMs)
      signal.addEventListener('abort', onAbort, { once: true })
      tab.calls.set(id, { resolve, timer, cleanup })
      tab.send(agentFrame({ type: 'call', id, tool, args }))
    })
  }

  async status(target: BrowserTarget, { signal }: { signal?: AbortSignal } = {}): Promise<BrowserStatus> {
    const resolved = await this.#resolve(target, signal)
    if ('problem' in resolved) return { attached: false, reason: resolved.problem }
    let state: { route: string; live: string[] } = resolved.tab ?? { route: '', live: [] }
    if (!resolved.tab) {
      const answer = await this.#relay!.forward(resolved.tabId, { op: 'status' }, {
        signal: signal ?? new AbortController().signal,
        timeoutMs: this.#callTimeoutMs,
      })
      if (answer?.op !== 'status') return { attached: false, reason: NOT_CONNECTED }
      state = answer
    }
    return {
      attached: true,
      via: resolved.via,
      route: state.route,
      live: state.live,
      ...(resolved.pairing ? { pairing: resolved.pairing } : {}),
    }
  }

  async pair(principal: Principal): Promise<PairingRequest> {
    if (!this.#pairings) throw new PairingError(NO_STORE.replace(/^no browser attached: /, ''))
    const request = await this.#pairings.request(principal)
    void this.refresh()
    return request
  }

  async acceptPairing(tab: TabConnection, id: string, code: string): Promise<void> {
    if (!this.#pairings) {
      return tab.send(agentFrame({ type: 'pairing.result', id, ok: false, message: 'pairing needs the database' }))
    }
    const result = await this.#pairings.accept(id, code, tab.tabId!)
    const message = result.ok
      ? `${result.pairing.label} can now use this tab.`
      : result.reason === 'wrong_code'
        ? `That is not the code. ${result.attemptsLeft} ${result.attemptsLeft === 1 ? 'try' : 'tries'} left.`
        : 'This request has expired or was answered already.'
    tab.send(agentFrame({ type: 'pairing.result', id, ok: result.ok, message }))
    await this.refresh()
  }

  async denyPairing(id: string): Promise<void> {
    await this.#pairings?.deny(id)
    await this.refresh()
  }

  async endPairing(tab: TabConnection, id: string): Promise<void> {
    await this.#pairings?.end(id, tab.tabId!)
    await this.refresh()
  }

  /**
   * Sends each connected tab the requests waiting and its own pairings, when
   * they changed since the last frame it got. One read at a time: a refresh
   * asked for while one runs waits for it and runs again.
   */
  refresh(): Promise<void> {
    const run = async () => {
      const tabs = [...this.#tabs.values()]
      if (tabs.length === 0) return
      let pending: PairingView[] = []
      let paired = new Map<string, PairingView[]>()
      if (this.#pairings) {
        try {
          ;[pending, paired] = await Promise.all([
            this.#pairings.pending(),
            this.#pairings.pairedWith(tabs.map((t) => t.tabId!)),
          ])
        } catch (err) {
          this.#log(`bridge: cannot read the pairings: ${(err as Error).message}`)
          return
        }
      }
      for (const tab of tabs) {
        const frame = agentFrame({
          type: 'pairings',
          pending: pending.map(entry),
          paired: (paired.get(tab.tabId!) ?? []).map(entry),
        })
        const key = JSON.stringify(frame)
        if (key === tab.lastPairings) continue
        tab.lastPairings = key
        tab.send(frame)
      }
    }
    const next = (this.#refreshing ?? Promise.resolve()).then(run, run)
    this.#refreshing = next
    return next
  }

  /** Closes every connection and stops the poll (shutdown). */
  close(): void {
    for (const tab of [...this.#tabs.values()]) tab.close('the agent service is shutting down')
    clearInterval(this.#poll)
    this.#poll = undefined
  }
}

/** Where a call goes: the tab here, or (`tab` undefined) one the relay finds on another replica. */
type Resolved = { tabId: string; tab?: TabConnection; via: 'session' | 'pairing'; pairing?: PairingView }

function entry(view: PairingView): PairingEntry {
  return { id: view.id, label: view.label, expiresAt: view.expiresAt.toISOString() }
}

function abortError(): Error {
  const err = new Error('the call was cancelled')
  err.name = 'AbortError'
  return err
}
