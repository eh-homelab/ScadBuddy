import { mkdtemp, readFile, rm } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { createSdkMcpServer, InMemorySessionStore, query, type SessionKey, type SessionStoreEntry, tool } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { type DurableConversation, resumeTranscript, turnToolName } from '../src/sessions/migrateDurable.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'

// Plan 2026-10-13 Step 7.2 (#4772): what a migrated DurableSession resumes from under the
// TypeScript CLI (sessions/migrateDurable.ts `resumeTranscript`).
//
// - Step 7.1's recorded conversation (fixtures/durable_conversation/), with the two entry kinds the
//   backfill's dry run found in production that it lacks (on 2026-11-01, 61 open DurableSessions,
//   all Claude Code 2.1.292): a `mode` entry and a `system` request-retry entry. Their fields are
//   the ones production's carry; the values are made up here, none is production's.
// - A turn stopped at a paused call (7 of those 61 sessions): the conversation ends at the paused
//   call's deferral marker, with the denials of the message's other calls after it, and the
//   workflow holds the results to deliver. That shape is made here by the TypeScript CLI itself (a
//   PreToolUse hook that defers the first call and denies the others, as the plugin's hook does),
//   since no DurableSession fixture has one.

const KEY = 'sk-ant-api03-unit-test-key-000011112222'
const FIXTURE = new URL('./fixtures/durable_conversation/', import.meta.url)
/** The ScadBuddy session the conversation moves to: not the Claude session it was written under. */
const OWN = '0b7c5a52-0d1e-4c43-9a51-4f3c1b0e7a11'
const NEXT = 'And what name is on it?'
const MISSING = '[Tool result missing due to internal error]'

type Entry = SessionStoreEntry & { uuid?: string; parentUuid?: string | null; sessionId?: string; attachment?: { type?: string; toolUseID?: string }; message?: { content?: unknown } }
type Block = { type?: string; id?: string; name?: string; tool_use_id?: string; content?: unknown; text?: string }

let fake: FakeAnthropic | undefined
const dirs: string[] = []
afterEach(async () => {
  await fake?.close()
  fake = undefined
  await Promise.all(dirs.splice(0).map((d) => rm(d, { recursive: true, force: true })))
})

async function tmp(prefix: string): Promise<string> {
  const dir = await mkdtemp(path.join(os.tmpdir(), prefix))
  dirs.push(dir)
  return dir
}

async function options(f: FakeAnthropic, store: InMemorySessionStore, cwd: string) {
  const dir = await tmp('migrate-durable-')
  return {
    pathToClaudeCodeExecutable: bundledCliPath(),
    tools: [],
    settingSources: [] as [],
    cwd,
    sessionStore: store,
    env: { ANTHROPIC_API_KEY: KEY, ANTHROPIC_BASE_URL: f.url, CLAUDE_CONFIG_DIR: dir, HOME: dir, PATH: process.env.PATH, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1' },
  }
}

/** The store, recording every key Claude Code appends under. */
function recordingStore(): { store: InMemorySessionStore; keys: SessionKey[] } {
  const store = new InMemorySessionStore()
  const keys: SessionKey[] = []
  const append = store.append.bind(store)
  store.append = async (key, entries) => {
    keys.push(key)
    await append(key, entries)
  }
  return { store, keys }
}

/** The project key Claude Code reads under in `cwd` (the in-memory store keys by it; ai_session_entries does not). */
async function projectKeyOf(f: FakeAnthropic, cwd: string): Promise<string> {
  const { store, keys } = recordingStore()
  for await (const _ of query({ prompt: 'probe', options: await options(f, store, cwd) })) {
    /* drain */
  }
  return keys[0]!.projectKey
}

/** Resumes `sessionId` from `entries` with `prompt`; the one request it sent, as `role:type:id-or-text` lines. */
async function resume(f: FakeAnthropic, entries: SessionStoreEntry[], sessionId: string, prompt: string, extra: Record<string, unknown> = {}) {
  const cwd = await tmp('migrate-durable-cwd-')
  const projectKey = await projectKeyOf(f, cwd)
  const store = new InMemorySessionStore()
  await store.append({ projectKey, sessionId }, entries)
  const before = f.messageCalls().length
  let resumedAs = ''
  for await (const m of query({ prompt, options: { ...(await options(f, store, cwd)), ...extra, resume: sessionId } })) {
    if ('session_id' in m && m.session_id) resumedAs = m.session_id
  }
  const calls = f.messageCalls().slice(before)
  return { resumedAs, calls, lines: lines(calls.at(-1)) }
}

function textOf(content: unknown): string {
  if (typeof content === 'string') return content
  return Array.isArray(content) ? (content as Block[]).map((b) => b.text ?? '').join('') : ''
}

function lines(request: RecordedRequest | undefined): string[] {
  return (request?.body?.messages ?? []).flatMap((m) =>
    (typeof m.content === 'string' ? [{ type: 'text', text: m.content }] : (m.content as Block[])).map((b) => {
      if (b.type === 'tool_use') return `${m.role}:tool_use:${b.id}:${b.name}`
      if (b.type === 'tool_result') return `${m.role}:tool_result:${b.tool_use_id}:${textOf(b.content)}`
      return `${m.role}:${b.type}:${b.text ?? ''}`
    }),
  )
}

async function fixture(): Promise<DurableConversation & { total: number }> {
  const entries = (await readFile(new URL('entries.jsonl', FIXTURE), 'utf8'))
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Entry)
  const meta = JSON.parse(await readFile(new URL('meta.json', FIXTURE), 'utf8')) as { session_id: string; checkpoint: string; total: number }
  return { claudeSessionId: meta.session_id, checkpoint: meta.checkpoint, busy: false, entries, pending: {}, total: meta.total }
}

describe('turnToolName', () => {
  it('names a DurableSession turn tool as an AgentSession turn offers it', () => {
    expect(turnToolName('mcp__durable__get_schema')).toBe('mcp__scadbuddy__get_schema')
    expect(turnToolName('mcp__durable__scadbuddy_http__http_request')).toBe('mcp__scadbuddy_http__http_request')
    expect(turnToolName('mcp__durable__my-memory__recall')).toBe('mcp__my-memory__recall')
    for (const builtin of ['Skill', 'Agent', 'Bash', 'mcp__scadbuddy__get_model']) expect(turnToolName(builtin)).toBe(builtin)
  })
})

describe("a migrated DurableSession's conversation under the TypeScript CLI (plan Step 7.2)", () => {
  it("resumes under the session's own id, its tool calls renamed, through production's other entry kinds", async () => {
    const conversation = await fixture()
    const firstAssistant = conversation.entries.findIndex((e) => e.type === 'assistant')
    const before = conversation.entries[firstAssistant]!
    // A request retry Claude Code wrote into the chain before the reply, and a `mode` entry.
    const retry: Entry = {
      cwd: '/app',
      entrypoint: 'sdk-py',
      error: { connection: { code: 'StreamNoResponse', isSSLError: false, message: 'made up' }, formatted: 'made up', isNetworkDown: false, message: 'made up', noResponse: { retryWaitMs: 500, waitedMs: 500 }, rateLimits: null },
      gitBranch: 'HEAD',
      isSidechain: false,
      level: 'error',
      maxRetries: 10,
      parentUuid: before.parentUuid ?? null,
      retryAttempt: 1,
      retryInMs: 500,
      sessionId: conversation.claudeSessionId,
      source: 'request_retry',
      subtype: 'api_error',
      timestamp: '2026-11-01T05:16:10.300Z',
      type: 'system',
      userType: 'external',
      uuid: '7a8d4b6d-0000-4000-8000-000000000001',
      version: '2.1.292',
    }
    const entries = [
      ...conversation.entries.slice(0, firstAssistant),
      retry,
      { ...before, parentUuid: retry.uuid },
      ...conversation.entries.slice(firstAssistant + 1),
      { type: 'mode', mode: 'normal', sessionId: conversation.claudeSessionId },
    ]
    const migrated = resumeTranscript({ ...conversation, entries }, OWN) as Entry[]
    expect(migrated).toHaveLength(entries.length)
    expect(new Set(migrated.map((e) => e.sessionId))).toEqual(new Set([OWN]))
    // The same input gives the same entries: a migration run again appends nothing new.
    expect(resumeTranscript({ ...conversation, entries }, OWN)).toEqual(migrated)

    fake = await startFakeAnthropic(() => ({ text: 'The name is Ada.' }))
    const { resumedAs, calls, lines: said } = await resume(fake, migrated, OWN, NEXT)
    expect(resumedAs).toBe(OWN)
    expect(calls).toHaveLength(1)
    const at = (pattern: RegExp) => said.findIndex((t) => pattern.test(t))
    const order = [
      at(/^user:text:On the name-keychain model, change the name to Ada/),
      at(/^assistant:tool_use:toolu_000001:mcp__scadbuddy__get_schema$/),
      at(/^user:tool_result:toolu_000001:.*"tool": "get_schema"/s),
      at(/^assistant:tool_use:toolu_000003:mcp__scadbuddy__render_model$/),
      at(/^user:tool_result:toolu_000003:.*"tool": "render_model"/s),
      at(/^assistant:text:I used red \(#ff0000\) for the letters\.$/),
      said.indexOf(`user:text:${NEXT}`),
    ]
    expect(order.every((i) => i >= 0), JSON.stringify(said, null, 1)).toBe(true)
    expect([...order].sort((a, b) => a - b)).toEqual(order)
    expect(said.some((t) => t.includes('mcp__durable__'))).toBe(false)
  }, 120_000)

  /**
   * A turn of the TypeScript CLI stopped as a DurableSession's stops at a paused call: three calls
   * in one message, the first deferred (the plugin's hook) and the other two denied after it.
   */
  async function stoppedAtPausedCall(f: FakeAnthropic): Promise<{ entries: Entry[]; sessionId: string; marker: string; ids: string[]; ran: () => number }> {
    let ran = 0
    const server = createSdkMcpServer({
      name: 'durable',
      tools: [
        tool('probe', 'a probe', { x: z.number() }, async () => {
          ran++
          return { content: [{ type: 'text', text: 'RAN' }] }
        }),
      ],
    })
    const { store, keys } = recordingStore()
    const cwd = await tmp('migrate-durable-cwd-')
    let deferred = false
    let sessionId = ''
    const hook = async (input: unknown) => {
      if ((input as { tool_input?: { x?: number } }).tool_input?.x === 1 && !deferred) {
        deferred = true
        return { hookSpecificOutput: { hookEventName: 'PreToolUse' as const, permissionDecision: 'defer' as const } }
      }
      return { hookSpecificOutput: { hookEventName: 'PreToolUse' as const, permissionDecision: 'deny' as const, permissionDecisionReason: 'not run: the turn paused' } }
    }
    for await (const m of query({
      prompt: 'probe three times',
      options: { ...(await options(f, store, cwd)), mcpServers: { durable: server }, allowedTools: ['mcp__durable__probe'], hooks: { PreToolUse: [{ hooks: [hook] }] } },
    })) {
      if ('session_id' in m && m.session_id) sessionId = m.session_id
    }
    const entries = store.getEntries(keys.find((k) => k.sessionId === sessionId && !k.subpath)!) as Entry[]
    const marker = entries.find((e) => e.attachment?.type === 'hook_deferred_tool')!
    const ids = entries.flatMap((e) => (e.type === 'assistant' ? (e.message?.content as Block[]).filter((b) => b.type === 'tool_use').map((b) => b.id!) : []))
    return { entries, sessionId, marker: marker.uuid!, ids, ran: () => ran }
  }

  const paused = (n: number): Reply => (n === 1 ? { toolUses: [1, 2, 3].map((x) => ({ name: 'mcp__durable__probe', input: { x } })) } : { text: `reply ${n}` })

  it('at a paused call, delivers every result the workflow holds, the paused call first answered, and runs nothing again', async () => {
    let n = 0
    fake = await startFakeAnthropic(() => paused(++n))
    const stopped = await stoppedAtPausedCall(fake)
    expect(stopped.ids).toHaveLength(3)
    // The marker comes before the other calls' denials, as in the 7 production sessions.
    const at = stopped.entries.findIndex((e) => e.uuid === stopped.marker)
    expect(stopped.entries.slice(at + 1).some((e) => e.type === 'user')).toBe(true)
    const pending = Object.fromEntries(stopped.ids.map((id, i) => [id, { content: `held result ${i}`, is_error: i === 0, blocks: null }]))
    const migrated = resumeTranscript({ claudeSessionId: stopped.sessionId, checkpoint: stopped.marker, busy: false, entries: stopped.entries, pending }, OWN)
    const { lines: said } = await resume(fake, migrated, OWN, NEXT)
    const [first, second, third] = stopped.ids
    const results = said.filter((t) => t.startsWith('user:tool_result:'))
    expect(results.sort()).toEqual([`user:tool_result:${first}:held result 0`, `user:tool_result:${second}:held result 1`, `user:tool_result:${third}:held result 2`].sort())
    expect(said.some((t) => t.includes(MISSING) || t.includes('not run'))).toBe(false)
    expect(said.indexOf(`user:tool_result:${first}:held result 0`)).toBeLessThan(said.indexOf(`user:text:${NEXT}`))
    expect(stopped.ran()).toBe(0)
  }, 120_000)

  it('at a paused call whose result the workflow does not hold, Claude Code answers it itself and runs nothing', async () => {
    let n = 0
    fake = await startFakeAnthropic(() => paused(++n))
    const stopped = await stoppedAtPausedCall(fake)
    const migrated = resumeTranscript({ claudeSessionId: stopped.sessionId, checkpoint: stopped.marker, busy: false, entries: stopped.entries, pending: {} }, OWN)
    const { lines: said } = await resume(fake, migrated, OWN, NEXT)
    expect(said).toContain(`user:tool_result:${stopped.ids[0]}:${MISSING}`)
    expect(said).toContain(`user:text:${NEXT}`)
    expect(stopped.ran()).toBe(0)
  }, 120_000)
})
