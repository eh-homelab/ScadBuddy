import { spawn } from 'node:child_process'
import { mkdtemp } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { createSdkMcpServer, InMemorySessionStore, query, type SDKMessage, tool } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { Inbox, userMessage } from '../src/harness/inbox.js'
import { type FakeAnthropic, startFakeAnthropic } from './support/fakeAnthropic.js'

// Spec 2026-10-13 §2.1: what the unified runtime relies on in the bundled Claude Code.
// A failure here after an SDK bump means the spec's facts moved: re-measure, then decide.
const KEY = 'sk-ant-api03-unit-test-key-000011112222'
let fake: FakeAnthropic | undefined
afterEach(async () => fake?.close())

async function env(f: FakeAnthropic) {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'sdk-runtime-'))
  return { ANTHROPIC_API_KEY: KEY, ANTHROPIC_BASE_URL: f.url, CLAUDE_CONFIG_DIR: dir, HOME: dir, PATH: process.env.PATH, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1' }
}
const base = () => ({ pathToClaudeCodeExecutable: bundledCliPath(), tools: [], settingSources: [] as [] })

async function nextResult(it: AsyncIterator<SDKMessage>) {
  for (;;) {
    const { value, done } = await it.next()
    if (done) throw new Error('query ended')
    if (value.type === 'result') return value
  }
}

describe('the bundled Claude Code (spec §2.1)', () => {
  it('E1: one streaming-input query is one process for several turns', async () => {
    let n = 0
    fake = await startFakeAnthropic(() => ({ text: `reply ${++n}` }))
    let spawns = 0
    const inbox = new Inbox()
    const q = query({ prompt: inbox, options: { ...base(), env: await env(fake),
      spawnClaudeCodeProcess: (o) => { spawns++; return spawn(o.command, o.args, { cwd: o.cwd, env: o.env, stdio: ['pipe', 'pipe', 'inherit'] }) } } })
    const it = q[Symbol.asyncIterator]()
    for (const text of ['one', 'two', 'three']) { inbox.push(userMessage(text)); await nextResult(it) }
    expect(spawns).toBe(1)
    expect(fake.messageCalls().at(-1)?.body?.messages?.filter((m) => m.role !== 'system').length).toBe(5)
    q.close()
  }, 120_000)

  it('E4: a killed call is never re-run on resume and is marked unknown', async () => {
    let n = 0
    fake = await startFakeAnthropic(() => (++n === 1 ? { toolUse: { name: 'mcp__x__slow', input: { a: 1 } } } : { text: `reply ${n}` }))
    const store = new InMemorySessionStore()
    let started!: () => void
    const running = new Promise<void>((r) => (started = r))
    let calls = 0
    const server = createSdkMcpServer({ name: 'x', tools: [tool('slow', 'slow', { a: z.number() }, async () => {
      calls++; started(); await new Promise((r) => setTimeout(r, 60_000)); return { content: [{ type: 'text', text: 'done' }] } })] })
    let child: ReturnType<typeof spawn> | undefined
    let sessionId = ''
    const q = query({ prompt: 'go', options: { ...base(), env: await env(fake), sessionStore: store, sessionStoreFlush: 'eager',
      mcpServers: { x: server }, allowedTools: ['mcp__x__slow'],
      spawnClaudeCodeProcess: (o) => (child = spawn(o.command, o.args, { cwd: o.cwd, env: o.env, stdio: ['pipe', 'pipe', 'inherit'] })) } })
    void (async () => { try { for await (const m of q) if ('session_id' in m && m.session_id) sessionId = m.session_id } catch { /* the SIGKILL ends the stream with an error */ } })()
    await running
    await new Promise((r) => setTimeout(r, 500))
    child?.kill('SIGKILL')
    await new Promise((r) => setTimeout(r, 1500))
    const before = fake.messageCalls().length
    let asked = false
    for await (const _ of query({ prompt: 'continue', options: { ...base(), env: await env(fake), sessionStore: store, resume: sessionId,
      mcpServers: { x: server }, allowedTools: ['mcp__x__slow'], canUseTool: async (_n, input) => { asked = true; return { behavior: 'allow', updatedInput: input } } } })) { /* drain */ }
    const resumed = JSON.stringify(fake.messageCalls()[before]?.body?.messages)
    expect(calls).toBe(1)
    expect(asked).toBe(false)
    expect(resumed).toContain('outcome is unknown')
  }, 120_000)

  it('E5: maxBudgetUsd is cumulative per process; maxTurns is per message', async () => {
    let n = 0
    fake = await startFakeAnthropic(() => ({ text: `reply ${++n}` }))
    const subtypes = async (opts: object) => {
      const inbox = new Inbox()
      const q = query({ prompt: inbox, options: { ...base(), env: await env(fake!), model: 'claude-sonnet-4-5', ...opts } })
      const it = q[Symbol.asyncIterator]()
      const out: string[] = []
      for (let i = 0; i < 3; i++) { inbox.push(userMessage(`t${i}`)); out.push((await nextResult(it)).subtype) }
      q.close()
      return out
    }
    expect(await subtypes({ maxBudgetUsd: 0.00012 })).toEqual(['success', 'error_max_budget_usd', 'error_max_budget_usd'])
    expect(await subtypes({ maxTurns: 2 })).toEqual(['success', 'success', 'success'])
  }, 120_000)
})
