import { mkdtemp, readFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { InMemorySessionStore, query, type SessionKey, type SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, describe, expect, it } from 'vitest'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { type FakeAnthropic, type RecordedRequest, startFakeAnthropic } from './support/fakeAnthropic.js'

// Plan 2026-10-13 Step 7.1, the gate of Phase 7 (spec 2026-10-13 §10.2, #4772): a
// DurableSession's conversation, as the plugin's `__claude_agent_conversation` Query
// serves it, is a transcript the agent's own Claude Code resumes. So Step 7.2 copies the
// entries into ai_session_entries as they are, with no converter.
//
// The fixture is one real DurableSession, recorded locally on 2026-11-01: agent-durable's
// worker (Python claude-agent-sdk 0.2.164, its bundled Claude Code 2.1.292, the plugin
// pin of pyproject.toml) through evals/durable.ts's stack against the fake endpoint, the
// customise scenario (get_schema, then render_model, both durable tools) and a second
// turn. Its 53 entries are the plugin's Query pages in order (identical to the
// workflow's own `fork_point` texts); meta.json is `fork_point`'s session id, checkpoint
// and total. Only local paths were rewritten (the checkout to /app, the home directory to
// /home/agent). No production data.

const KEY = 'sk-ant-api03-unit-test-key-000011112222'
const FIXTURE = new URL('./fixtures/durable_conversation/', import.meta.url)
const NEXT = 'And what name is on it?'
let fake: FakeAnthropic | undefined
afterEach(async () => fake?.close())

type Meta = { session_id: string; checkpoint: string; total: number }
type Block = { type?: string; text?: string; id?: string; tool_use_id?: string; content?: unknown }
type Entry = SessionStoreEntry & { uuid?: string; sessionId?: string; message?: { role?: string; content?: unknown } }

async function fixture(): Promise<{ entries: Entry[]; meta: Meta }> {
  const text = await readFile(new URL('entries.jsonl', FIXTURE), 'utf8')
  const entries = text
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Entry)
  const meta = JSON.parse(await readFile(new URL('meta.json', FIXTURE), 'utf8')) as Meta
  return { entries, meta }
}

async function options(f: FakeAnthropic, store: InMemorySessionStore, cwd: string) {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'durable-import-'))
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

function blocks(request: RecordedRequest | undefined): { role: string; blocks: Block[] }[] {
  return (request?.body?.messages ?? []).map((m) => ({
    role: m.role,
    blocks: typeof m.content === 'string' ? [{ type: 'text', text: m.content }] : (m.content as Block[]),
  }))
}

function textOf(content: unknown): string {
  if (typeof content === 'string') return content
  return Array.isArray(content) ? (content as Block[]).map((b) => b.text ?? '').join('') : ''
}

const keysOf = (entries: Entry[], type: string) => [...new Set(entries.filter((e) => e.type === type).flatMap((e) => Object.keys(e)))].sort()

describe('a DurableSession conversation under the TypeScript CLI (plan Step 7.1)', () => {
  it('is the transcript the TypeScript CLI writes: the same entry types, with the same fields', async () => {
    const { entries, meta } = await fixture()
    expect(entries).toHaveLength(meta.total)
    fake = await startFakeAnthropic(() => ({ text: 'a TypeScript-written reply' }))
    const { store, keys } = recordingStore()
    const cwd = await mkdtemp(path.join(os.tmpdir(), 'durable-import-cwd-'))
    for await (const _ of query({ prompt: 'hello', options: await options(fake, store, cwd) })) {
      /* drain */
    }
    const written = store.getEntries(keys[0]!) as Entry[]
    // Every kind of entry the TypeScript CLI wrote for one turn is in the durable
    // conversation, but `cost-state`, which the plugin keeps out of what it commits
    // (temporalio.claude_agent_sdk `_runner.py` `_without_cost_state`).
    const durableTypes = new Set(entries.map((e) => e.type))
    expect([...new Set(written.map((e) => e.type))].filter((t) => !durableTypes.has(t))).toEqual(['cost-state'])
    // The transcript entries carry the same fields, but those a newer Claude Code adds
    // to an assistant entry (2.1.310 over 2.1.292), which a resume does not need.
    for (const type of ['user', 'attachment', 'last-prompt']) expect(keysOf(entries, type), type).toEqual(keysOf(written, type))
    expect(keysOf(written, 'assistant').filter((k) => !keysOf(entries, 'assistant').includes(k))).toEqual(['requestRef', 'requestedModel'])
    expect(keysOf(entries, 'assistant').filter((k) => !keysOf(written, 'assistant').includes(k))).toEqual([])
    // The durable conversation is one session, ending at the workflow's checkpoint.
    expect(new Set(entries.map((e) => e.sessionId))).toEqual(new Set([meta.session_id]))
    expect(entries.filter((e) => e.uuid && e.message).at(-1)?.uuid).toBe(meta.checkpoint)
  }, 120_000)

  it('resumes: the next request carries both old turns, their tool calls and results, then the new prompt', async () => {
    const { entries, meta } = await fixture()
    fake = await startFakeAnthropic(() => ({ text: 'The name is Ada.' }))
    const { store, keys } = recordingStore()
    const cwd = await mkdtemp(path.join(os.tmpdir(), 'durable-import-cwd-'))
    // The key Claude Code reads under in this directory: ai_session_entries ignores the
    // project key (src/sessions/store.ts), the in-memory store does not.
    for await (const _ of query({ prompt: 'probe', options: await options(fake, store, cwd) })) {
      /* drain */
    }
    const projectKey = keys[0]!.projectKey
    await store.append({ projectKey, sessionId: meta.session_id }, entries)
    const before = fake.messageCalls().length

    let resumedAs = ''
    let result = ''
    for await (const m of query({ prompt: NEXT, options: { ...(await options(fake, store, cwd)), resume: meta.session_id } })) {
      if ('session_id' in m && m.session_id) resumedAs = m.session_id
      if (m.type === 'result' && m.subtype === 'success') result = m.result
    }
    expect(resumedAs).toBe(meta.session_id)
    expect(result).toBe('The name is Ada.')
    const calls = fake.messageCalls().slice(before)
    expect(calls).toHaveLength(1)
    const messages = blocks(calls[0])
    const all = messages.flatMap((m) => m.blocks.map((b) => ({ role: m.role, ...b })))
    const texts = all.map((b) => `${b.role}:${b.type}:${b.type === 'tool_result' ? textOf(b.content) : (b.text ?? b.id ?? '')}`)
    const at = (pattern: RegExp) => texts.findIndex((t) => pattern.test(t))
    const order = [
      at(/^user:text:On the name-keychain model, change the name to Ada/),
      at(/^assistant:tool_use:toolu_000001$/),
      all.findIndex((b) => b.type === 'tool_result' && b.tool_use_id === 'toolu_000001'),
      at(/^assistant:tool_use:toolu_000003$/),
      all.findIndex((b) => b.type === 'tool_result' && b.tool_use_id === 'toolu_000003'),
      at(/^assistant:text:Rendered name-keychain with the name "Ada" in red letters/),
      at(/^user:text:Thanks\. Which colour did you use for the letters\?$/),
      at(/^assistant:text:I used red \(#ff0000\) for the letters\.$/),
      at(new RegExp(`^user:text:${NEXT.replace(/[?]/g, '\\?')}$`)),
    ]
    expect(order.every((i) => i >= 0), JSON.stringify(texts, null, 1)).toBe(true)
    expect([...order].sort((a, b) => a - b)).toEqual(order)
    // The durable tools' results, as the workflow delivered them.
    expect(textOf(all.find((b) => b.tool_use_id === 'toolu_000001')?.content)).toContain('"tool": "get_schema"')
    expect(textOf(all.find((b) => b.tool_use_id === 'toolu_000003')?.content)).toContain('"tool": "render_model"')
    // The resumed turn was appended to the same session, after the durable checkpoint.
    const after = store.getEntries({ projectKey, sessionId: meta.session_id }) as Entry[]
    expect(after.slice(0, entries.length)).toEqual(entries)
    const added = after.slice(entries.length).filter((e) => e.type === 'user' && e.message)
    expect(textOf(added[0]?.message?.content)).toBe(NEXT)
    expect((added[0] as { parentUuid?: string } | undefined)?.parentUuid).toBe(meta.checkpoint)
  }, 120_000)
})
