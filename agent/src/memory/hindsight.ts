import { readFile } from 'node:fs/promises'
import type {
  HookCallback,
  HookCallbackMatcher,
  HookEvent,
  HookJSONOutput,
  SessionStore,
  SessionStoreEntry,
} from '@anthropic-ai/claude-agent-sdk'
import { pinnedFetch } from '../http/pinned.js'
import type { CheckedPlugin } from '../plugins/registry.js'
import { wrapUntrustedText } from '../safety/untrusted.js'
import { redact } from '../secrets.js'

// Automatic Hindsight memory for assistant sessions: recall at each user
// prompt, retain at each turn's end, without the model having to call the
// `hindsight` plugin's `recall`/`retain` tools itself (in production it
// called recall once and retain never across four sessions, and the bank held
// no documents).
//
// A PORT of Hindsight's Claude Agent SDK integration, `create_memory_hooks` in
// https://github.com/vectorize-io/hindsight/blob/eb021da3b2501911e4b57c82b3de1123572a200e/hindsight-integrations/claude-agent-sdk/hindsight_claude_agent_sdk/hooks.py
// (defaults from config.py beside it), kept close enough to diff against it:
//
//   UserPromptSubmit  `_recall_hook`: recall against the prompt (or a fixed
//                     `recallQuery`), number the first `recallMaxResults`
//                     results, prefix them with `recallPrefix`.
//   Stop              `_retain_hook`: read the transcript, retain it.
//   PostToolUse       `_tool_retain_hook`, only when `retainOnTools` names
//                     tools (matcher: the names, regex-escaped, joined by |).
//                     Empty by default, so OFF.
//
// Where this departs from upstream, and why:
//
//   - In-process SDK callback hooks, not command hooks: harness/plugins.ts
//     refuses command hooks because the process would inherit the credential
//     env, and the Python package is not usable here.
//   - Recall is injected as `hookSpecificOutput.additionalContext`, not
//     `systemMessage`. sdk.d.ts (0.3.283) types `additionalContext` on
//     `UserPromptSubmitHookSpecificOutput`, which Claude Code adds to the
//     model's context; `systemMessage` is, per Claude Code's hooks reference,
//     a message shown to the user. The memories are untrusted data (#258,
//     safety/untrusted.ts): they go inside the untrusted-data envelope, in a
//     `<hindsight_memories>` block that retain strips again.
//   - Recall has a short timeout (RECALL_TIMEOUT_MS), since it delays the
//     turn; on timeout or error nothing is injected and the reason is logged.
//   - Retain is fire-and-forget: the Stop hook returns at once and the
//     request runs on, so a slow or failing Hindsight never delays or fails a
//     turn. A failure is logged (upstream logs a warning too). Retains of one
//     document run in turn order across turns (`enqueueRetain`), so an older
//     snapshot never lands last, and a graceful shutdown waits for them
//     (`drainRetains`, main.ts).
//   - `retainMode: 'transcript'` (the default) retains the WHOLE conversation
//     as one document, `document_id: conversation:<session id>`, so each
//     turn's retain upserts the same document instead of adding a new one.
//     The format is that of Hindsight's coding-agents plugin
//     (@vectorize-io/hindsight-coding-agents 0.3.4, dist/claude-stop-hook.js
//     `readClaudeTranscript`, `renderLine`, `actionLine`, `renderSessionJsonl`):
//     JSONL, a `REF-ID` system line, then one `{role, content}` per user or
//     assistant text and one `action` line per tool use naming its target;
//     tool results are left out. Upstream's `_extract_result_from_transcript`
//     keeps only the last result, which upserted under one id would erase
//     every earlier turn; it is `retainMode: 'result'`, upstream's behaviour
//     exactly (no document id, `retainPrefix`, the 20- and 4000-character
//     limits).
//   - Secrets (the Claude credential, every plugin header) are redacted from
//     everything retained and logged, and from the recall query. The retained text is the transcript's
//     user and assistant text only, never the environment.
//   - Every request goes to the address the plugin's egress check passed,
//     follows no redirect, and carries the plugin's own header
//     (http/pinned.ts, as the plugin forwarder does).
//   - The injected memories escape `<` and `>` (as `\u003c`/`\u003e`, still
//     the same JSON), so recalled text cannot close `<hindsight_memories>`.
//   - The PostToolUse matcher is anchored (`^(?:a|b)$`); upstream's bare
//     alternation also matches tool names that merely contain one.
//
// The REST calls (Hindsight's API, the same host as the MCP URL):
//   recall  POST /v1/default/banks/<bank>/memories/recall
//           { query, budget, max_tokens, tags?, tags_match? } → { results: [{ text }] }
//   retain  POST /v1/default/banks/<bank>/memories
//           { items: [{ content, context?, document_id?, tags?, metadata? }], async: true }
// as upstream's own TypeScript client sends them
// (hindsight-integrations/paperclip/src/client.ts at the same commit) and the
// coding-agents plugin's `retain` (dist/claude-stop-hook.js).

/** How long recall may hold up a user's turn before it is given up on. */
export const RECALL_TIMEOUT_MS = 3_000
/** A retain runs after the turn; the coding-agents plugin gives a request 15 s. */
export const RETAIN_TIMEOUT_MS = 15_000

export type Budget = 'low' | 'mid' | 'high'
export type TagsMatch = 'any' | 'all' | 'any_strict' | 'all_strict'

/** config.py defaults. */
export const DEFAULT_BUDGET: Budget = 'mid'
export const DEFAULT_MAX_TOKENS = 4096
export const DEFAULT_RECALL_TAGS_MATCH: TagsMatch = 'any'

/** hooks.py `MemoryHookConfig`, same fields in camelCase, plus `retainMode`. */
export type MemoryHookConfig = {
  /** Inject relevant memories on each `UserPromptSubmit`. */
  autoRecall: boolean
  /** Retain on `Stop`. */
  autoRetain: boolean
  /** Tool names whose results are retained on `PostToolUse`; empty means no PostToolUse hook. */
  retainOnTools: string[]
  /** The recall query; `$prompt` means the prompt itself. */
  recallQuery: string
  recallMaxResults: number
  /** Text before the memory list. */
  recallPrefix: string
  retainTags: string[]
  /** Text before a retained result (`retainMode: 'result'` only, as upstream; a transcript is JSONL). */
  retainPrefix: string
  /** ScadBuddy's: the whole conversation as one upserted document, or upstream's last result only. */
  retainMode: 'transcript' | 'result'
}

export const DEFAULT_MEMORY_HOOK_CONFIG: MemoryHookConfig = {
  autoRecall: true,
  autoRetain: true,
  retainOnTools: [],
  recallQuery: '$prompt',
  recallMaxResults: 5,
  recallPrefix: '\n\nRelevant memories from previous sessions:\n',
  retainTags: ['source:claude-agent-sdk'],
  retainPrefix: 'Agent session result: ',
  retainMode: 'transcript',
}

/** Where the plugin's Hindsight is: its API base, the bank, and how to reach it. */
export type HindsightTarget = {
  apiBase: string
  bankId: string
  address: string
  header?: { name: string; value: string }
}

export class HindsightConfigError extends Error {
  override name = 'HindsightConfigError'
}

/**
 * The target behind a `hindsight` plugin: its URL is `<base>/mcp/<bank_id>/`
 * (Hindsight's per-bank MCP endpoint, README "Hindsight"), and the REST API is
 * `<base>/v1/default/banks/<bank_id>/…` on the same host.
 */
export function hindsightTarget({ plugin, address }: CheckedPlugin): HindsightTarget {
  const url = new URL(plugin.url)
  const match = /^(.*)\/mcp\/([^/]+)\/?$/.exec(url.pathname)
  if (!match?.[2]) {
    throw new HindsightConfigError(`plugin ${plugin.name}: url ${plugin.url} is not a Hindsight bank endpoint (…/mcp/<bank_id>/)`)
  }
  return {
    apiBase: `${url.origin}${match[1] ?? ''}`,
    bankId: decodeURIComponent(match[2]),
    address,
    ...(plugin.header ? { header: plugin.header } : {}),
  }
}

export type RecallBody = { query: string; budget: Budget; max_tokens: number; tags?: string[]; tags_match?: TagsMatch }
export type RetainItem = {
  content: string
  context?: string
  document_id?: string
  tags?: string[]
  metadata?: Record<string, string>
}

export class HindsightError extends Error {
  override name = 'HindsightError'
}

/** The two REST calls, over the pinned address. */
export class HindsightClient {
  private readonly target: HindsightTarget
  constructor(target: HindsightTarget) {
    this.target = target
  }

  get bankId(): string {
    return this.target.bankId
  }

  private async post(path: string, body: unknown, signal: AbortSignal): Promise<unknown> {
    const url = `${this.target.apiBase}/v1/default/banks/${encodeURIComponent(this.target.bankId)}${path}`
    const headers: Record<string, string> = { 'content-type': 'application/json', accept: 'application/json' }
    if (this.target.header) headers[this.target.header.name] = this.target.header.value
    const res = await pinnedFetch(url, this.target.address, { method: 'POST', headers, body: JSON.stringify(body), signal })
    if (res.status < 200 || res.status >= 300) {
      throw new HindsightError(`POST ${path} answered HTTP ${res.status}: ${res.body.slice(0, 200)}`)
    }
    try {
      return JSON.parse(res.body) as unknown
    } catch {
      return undefined
    }
  }

  /** `arecall`: the results' texts, in the order Hindsight ranked them. */
  async recall(body: RecallBody, signal: AbortSignal): Promise<string[]> {
    const response = (await this.post('/memories/recall', body, signal)) as { results?: unknown } | undefined
    const results = Array.isArray(response?.results) ? response.results : []
    return results
      .map((r: unknown) => (typeof r === 'object' && r !== null ? (r as { text?: unknown }).text : undefined))
      .filter((t): t is string => typeof t === 'string')
  }

  /** `aretain`, async on the server (extraction is queued, as the coding-agents plugin asks). */
  async retain(item: RetainItem, signal: AbortSignal): Promise<void> {
    await this.post('/memories', { items: [item], async: true }, signal)
  }
}

// ------------------------------------------------------------- transcripts

type TranscriptLine = {
  type?: unknown
  isMeta?: unknown
  isSidechain?: unknown
  isCompactSummary?: unknown
  timestamp?: unknown
  result?: unknown
  message?: { content?: unknown }
}

export type Turn = { role: string; content: string; timestamp?: string }

/** A retained user or assistant text is cut to this many characters. */
export const TURN_MAX_CHARS = 8_000

/**
 * Blocks the harness adds to a user turn that are not the user's words: the
 * memories injected here, and the panel's page context (clientProtocol.ts
 * `renderPageContext`, with the sentence after it). The coding-agents plugin
 * strips its own injected blocks the same way (`stripInjectedMemory`).
 */
const INJECTED_RE =
  /<hindsight_memories>[\s\S]*?<\/hindsight_memories>|<page_context>[\s\S]*?<\/page_context>(?:\nThe block above describes the ScadBuddy page[^\n]*)?/g

function clean(text: string): string {
  const out = text.replace(INJECTED_RE, '').trim()
  return out.length > TURN_MAX_CHARS ? `${out.slice(0, TURN_MAX_CHARS)}… (truncated)` : out
}

const TARGET_KEYS = ['file_path', 'path', 'notebook_path', 'command', 'pattern', 'query', 'url', 'name', 'id']
const ACTION_TARGET_CAP = 100

/** coding-agents `actionLine`: the tool and the first of its inputs that says what it acted on. */
export function actionLine(tool: string, input: unknown): string {
  let target = ''
  if (input && typeof input === 'object') {
    const record = input as Record<string, unknown>
    for (const key of TARGET_KEYS) {
      const value = record[key]
      if (typeof value === 'string' && value.trim()) {
        target = value.trim().split('\n')[0] ?? ''
        break
      }
    }
  } else if (typeof input === 'string') {
    target = input.trim().split('\n')[0] ?? ''
  }
  if (target.length > ACTION_TARGET_CAP) target = `${target.slice(0, ACTION_TARGET_CAP)}…`
  return target ? `${tool} ${target}` : tool
}

/** coding-agents `renderLine`: text blocks joined, then one action per tool use; tool results dropped. */
function renderLine(content: unknown, role: string): Turn[] {
  if (typeof content === 'string') {
    const text = clean(content)
    return text ? [{ role, content: text }] : []
  }
  if (!Array.isArray(content)) return []
  const texts: string[] = []
  const actions: Turn[] = []
  for (const block of content as unknown[]) {
    if (!block || typeof block !== 'object') continue
    const b = block as { type?: unknown; text?: unknown; name?: unknown; input?: unknown }
    if (b.type === 'text' && typeof b.text === 'string') {
      const text = clean(b.text)
      if (text) texts.push(text)
    } else if (b.type === 'tool_use' && typeof b.name === 'string') {
      actions.push({ role: 'action', content: actionLine(b.name, b.input) })
    }
  }
  const out: Turn[] = []
  const joined = texts.join('\n').trim()
  if (joined) out.push({ role, content: joined })
  out.push(...actions)
  return out
}

function entriesOf(jsonl: string): TranscriptLine[] {
  const out: TranscriptLine[] = []
  for (const raw of jsonl.split('\n')) {
    if (!raw.trim()) continue
    try {
      const parsed: unknown = JSON.parse(raw)
      if (typeof parsed === 'object' && parsed !== null) out.push(parsed as TranscriptLine)
    } catch {
      // A torn last line, as upstream skips it.
    }
  }
  return out
}

/** coding-agents `readClaudeTranscript`, over parsed entries. */
export function transcriptTurns(entries: readonly TranscriptLine[]): Turn[] {
  const turns: Turn[] = []
  for (const line of entries) {
    if (line.type !== 'user' && line.type !== 'assistant') continue
    if (line.isMeta === true || line.isSidechain === true || line.isCompactSummary === true) continue
    if (typeof line.message !== 'object' || line.message === null) continue
    for (const turn of renderLine(line.message.content, line.type)) {
      turns.push(typeof line.timestamp === 'string' ? { ...turn, timestamp: line.timestamp } : turn)
    }
  }
  return turns
}

/** coding-agents `renderSessionJsonl`. */
export function renderSessionJsonl(refId: string, turns: readonly Turn[]): string {
  return [{ role: 'system', content: `REF-ID: ${refId}` }, ...turns].map((t) => JSON.stringify(t)).join('\n')
}

/** hooks.py `_extract_result_from_transcript`: the last `result`, else the last assistant text. */
export function extractResultFromTranscript(entries: readonly TranscriptLine[]): string {
  let fallback = ''
  for (const entry of [...entries].reverse()) {
    if (entry.type === 'result' && typeof entry.result === 'string' && entry.result.trim()) return entry.result.trim()
    if (!fallback && entry.type === 'assistant') {
      const content = entry.message?.content
      if (Array.isArray(content)) {
        fallback = (content as { type?: unknown; text?: unknown }[])
          .filter((b) => typeof b === 'object' && b !== null && b.type === 'text')
          .map((b) => (typeof b.text === 'string' ? b.text.trim() : ''))
          .filter(Boolean)
          .join('\n')
      }
    }
  }
  return fallback
}

// ------------------------------------------------------------------- hooks

export type MemoryHooksOptions = {
  client: HindsightClient
  budget?: Budget
  maxTokens?: number
  recallTags?: string[]
  recallTagsMatch?: TagsMatch
  hookConfig?: Partial<MemoryHookConfig>
  /** Redacted from everything retained and every log line. */
  secrets: readonly string[]
  /** A failure's one line (the session manager's `stderr`). */
  log: (line: string) => void
  /**
   * Where the transcript is kept when the file the Stop hook names cannot be
   * read (sessions/store.ts: the SDK mirrors every line into Postgres).
   */
  sessionStore?: Pick<SessionStore, 'load'>
  recallTimeoutMs?: number
  retainTimeoutMs?: number
}

export type MemoryHooks = {
  hooks: Partial<Record<HookEvent, HookCallbackMatcher[]>>
  /** Settles when every retain started so far has finished (tests; nothing waits on it in a turn). */
  settled(): Promise<void>
}

function describe(err: unknown): string {
  if (err instanceof Error && err.name === 'AbortError') {
    // Node's request rejects with an AbortError whose cause is the signal's reason.
    return (err.cause as Error | undefined)?.name === 'TimeoutError' ? 'timed out' : 'aborted'
  }
  return err instanceof Error ? err.message : String(err)
}

/** hooks.py `create_memory_hooks`. */
/**
 * Retains in flight in this process, chained per document. Each turn builds its
 * own hooks, so a queue per turn would let turn N's slow upsert of
 * `conversation:<id>` land after turn N+1's and put back the older snapshot
 * (review of #807). Chaining per document keeps them in the order the turns
 * ended; `drainRetains` lets a shutdown wait for them.
 */
const retainQueues = new Map<string, Promise<void>>()
const pendingRetains = new Set<Promise<void>>()

function enqueueRetain(key: string | undefined, job: () => Promise<void>): Promise<void> {
  const previous = key ? retainQueues.get(key) : undefined
  const run = (previous ?? Promise.resolve()).then(job, job)
  pendingRetains.add(run)
  if (key) retainQueues.set(key, run)
  // The caller handles `run`'s failure; this branch only cleans up.
  run
    .finally(() => {
      pendingRetains.delete(run)
      if (key && retainQueues.get(key) === run) retainQueues.delete(key)
    })
    .catch(() => {})
  return run
}

/** Waits for every retain this process has started (a graceful shutdown). Never throws. */
export async function drainRetains(): Promise<void> {
  while (pendingRetains.size) await Promise.allSettled([...pendingRetains])
}

export function createMemoryHooks(options: MemoryHooksOptions): MemoryHooks {
  const cfg: MemoryHookConfig = { ...DEFAULT_MEMORY_HOOK_CONFIG, ...options.hookConfig }
  const { client } = options
  const budget = options.budget ?? DEFAULT_BUDGET
  const maxTokens = options.maxTokens ?? DEFAULT_MAX_TOKENS
  const recallTimeoutMs = options.recallTimeoutMs ?? RECALL_TIMEOUT_MS
  const retainTimeoutMs = options.retainTimeoutMs ?? RETAIN_TIMEOUT_MS
  const scrub = (text: string) => redact(text, options.secrets)
  const log = (line: string) => options.log(`${scrub(line)}\n`)
  const inFlight = new Set<Promise<void>>()

  /** Starts a retain and returns at once; a failure is logged, never thrown. */
  const retainNow = (what: string, item: RetainItem): Promise<void> =>
    client
      .retain({ ...item, content: scrub(item.content) }, AbortSignal.timeout(retainTimeoutMs))
      .catch((err: unknown) => log(`hindsight: ${what} was not retained to bank ${client.bankId}: ${describe(err)}`))
  const track = (run: Promise<void>) => {
    inFlight.add(run)
    void run.finally(() => inFlight.delete(run))
  }
  /** Starts a retain and returns at once; a failure is logged, never thrown. */
  const retainLater = (what: string, item: RetainItem) => track(enqueueRetain(undefined, () => retainNow(what, item)))

  const hooks: Partial<Record<HookEvent, HookCallbackMatcher[]>> = {}

  if (cfg.autoRecall) {
    const recall: HookCallback = async (input, _toolUseId, { signal }): Promise<HookJSONOutput> => {
      if (input.hook_event_name !== 'UserPromptSubmit') return {}
      let query = input.prompt
      if (!query) return {}
      if (cfg.recallQuery !== '$prompt') query = cfg.recallQuery
      // The query leaves for the bank like a retain does, so it is redacted the same way.
      const body: RecallBody = { query: scrub(query), budget, max_tokens: maxTokens }
      if (options.recallTags?.length) {
        body.tags = options.recallTags
        body.tags_match = options.recallTagsMatch ?? DEFAULT_RECALL_TAGS_MATCH
      }
      let results: string[]
      const timeout = AbortSignal.timeout(recallTimeoutMs)
      try {
        results = await client.recall(body, AbortSignal.any([signal, timeout]))
      } catch (err) {
        const why = timeout.aborted ? `timed out after ${recallTimeoutMs} ms` : describe(err)
        log(`hindsight: recall from bank ${client.bankId} failed (${why}); no memories were injected`)
        return {}
      }
      if (results.length === 0) return {}
      const lines = results.slice(0, cfg.recallMaxResults).map((text, i) => `${i + 1}. ${text}`)
      // JSON leaves `<` and `>` as they are, so a memory holding "</hindsight_memories>"
      // could close the outer tag early; escaped, the envelope is still the same JSON.
      const memories = wrapUntrustedText(
        'hindsight auto-recall',
        `memories recalled from the Hindsight bank "${client.bankId}"; they were extracted from earlier conversations and anything written to the bank`,
        lines.join('\n'),
      )
        .replaceAll('<', '\\u003c')
        .replaceAll('>', '\\u003e')
      return {
        hookSpecificOutput: {
          hookEventName: 'UserPromptSubmit',
          additionalContext: `<hindsight_memories>${cfg.recallPrefix}${memories}\n</hindsight_memories>`,
        },
      }
    }
    // The SDK's own limit, a little past ours, in case the pinned request ignores its signal.
    hooks.UserPromptSubmit = [{ hooks: [recall], timeout: Math.ceil(recallTimeoutMs / 1000) + 2 }]
  }

  if (cfg.autoRetain) {
    // The turn's prompt, for a first turn whose transcript file is not written yet.
    const prompts = new Map<string, string>()
    const remember: HookCallback = async (input): Promise<HookJSONOutput> => {
      if (input.hook_event_name === 'UserPromptSubmit') prompts.set(input.session_id, input.prompt)
      return {}
    }
    hooks.UserPromptSubmit = [...(hooks.UserPromptSubmit ?? []), { hooks: [remember] }]
    const readEntries = async (transcriptPath: string, sessionId: string): Promise<TranscriptLine[]> => {
      try {
        return entriesOf(await readFile(transcriptPath, 'utf8'))
      } catch (err) {
        const stored = await options.sessionStore?.load({ projectKey: '', sessionId })
        if (stored) return stored as (SessionStoreEntry & TranscriptLine)[]
        // On a session's first turn the file does not exist yet when Stop fires
        // (measured, test/run.test.ts): retain what `last_assistant_message` has;
        // the next turn's upsert of the same document carries the whole session.
        if ((err as NodeJS.ErrnoException).code === 'ENOENT') return []
        throw err
      }
    }
    const retain: HookCallback = async (input): Promise<HookJSONOutput> => {
      if (input.hook_event_name !== 'Stop') return {}
      const sessionId = input.session_id
      // Measured on Claude Code 2.1.283 (test/run.test.ts): when Stop fires, the
      // transcript file does not hold the turn's final assistant message yet.
      // The SDK hands it over as `last_assistant_message` ("Text content of the
      // last assistant message before stopping. Avoids the need to read and
      // parse the transcript file", sdk.d.ts 0.3.283), so it is used for the
      // last reply; the next turn's retain finds it in the file.
      const last = input.last_assistant_message?.trim() ?? ''
      const refId = `conversation:${sessionId}`
      const started = enqueueRetain(cfg.retainMode === 'transcript' ? refId : undefined, async () => {
        const entries = await readEntries(input.transcript_path, sessionId)
        if (cfg.retainMode === 'result') {
          const result = last || extractResultFromTranscript(entries)
          if (!result || result.length < 20) return
          const content = cfg.retainPrefix + (result.length > 4000 ? result.slice(0, 4000) : result)
          await retainNow(`the result of session ${sessionId}`, { content, ...(cfg.retainTags.length ? { tags: cfg.retainTags } : {}) })
          return
        }
        const turns = transcriptTurns(entries)
        const prompt = clean(prompts.get(sessionId) ?? '')
        prompts.delete(sessionId)
        if (turns.length === 0 && prompt) turns.push({ role: 'user', content: prompt })
        const reply = clean(last)
        const tail = turns.at(-1)
        if (reply && !(tail?.role === 'assistant' && tail.content === reply)) turns.push({ role: 'assistant', content: reply })
        if (turns.length === 0) return
        await retainNow(`session ${sessionId}`, {
          content: renderSessionJsonl(refId, turns),
          context: 'ScadBuddy assistant session',
          document_id: refId,
          ...(cfg.retainTags.length ? { tags: cfg.retainTags } : {}),
          metadata: { source: 'scadbuddy-assistant', session_id: sessionId },
        })
      }).catch((err: unknown) => log(`hindsight: session ${sessionId} was not retained: cannot read its transcript (${describe(err)})`))
      track(started)
      return {}
    }
    hooks.Stop = [{ hooks: [retain] }]
  }

  if (cfg.retainOnTools.length) {
    // Anchored: the SDK tests the matcher as a regex, and a bare alternation would
    // also match any tool whose name merely contains one of these.
    const matcher = `^(?:${cfg.retainOnTools.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})$`
    const toolRetain: HookCallback = (input): Promise<HookJSONOutput> => {
      if (input.hook_event_name !== 'PostToolUse') return Promise.resolve({})
      const response = typeof input.tool_response === 'string' ? input.tool_response : JSON.stringify(input.tool_response ?? '')
      if (!response || response.length < 20) return Promise.resolve({})
      const args = typeof input.tool_input === 'string' ? input.tool_input : JSON.stringify(input.tool_input ?? {})
      const content = `Tool ${input.tool_name} called with: ${args.slice(0, 500)}\nResult: ${response.slice(0, 2000)}`
      retainLater(`the result of ${input.tool_name}`, { content, tags: [...cfg.retainTags, `tool:${input.tool_name}`] })
      return Promise.resolve({})
    }
    hooks.PostToolUse = [{ matcher, hooks: [toolRetain] }]
  }

  return {
    hooks,
    settled: async () => {
      while (inFlight.size) await Promise.allSettled([...inFlight])
    },
  }
}
