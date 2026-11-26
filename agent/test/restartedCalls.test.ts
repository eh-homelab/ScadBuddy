import type { SessionStore, SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import type { Client } from '@temporalio/client'
import { WorkflowNotFoundError } from '@temporalio/client'
import { describe, expect, it, vi } from 'vitest'
import type { HarnessRun } from '../src/harness/run.js'
import {
  danglingCalls,
  endedText,
  restartedCallText,
  resultsEntry,
  stillRunningText,
  toolResultBlock,
} from '../src/sessions/restartedCalls.js'
import { createTurnSlot } from '../src/sessions/turnSlot.js'
import type { AssembledTurn } from '../src/sessions/turnRunner.js'
import { type WarmQuery, WarmSessions } from '../src/sessions/warm.js'
import { cutToolCall, toolCallStarter } from '../src/tools/activityProjection.js'
import { toolCallWorkflowId } from '../src/temporal/toolCall.js'

// The calls a lost placement left in flight (#7874): a shutdown's cut drops what Claude Code writes
// after its interrupt (warm.ts `seal`), and the restarted placement gives each dangling call its
// result (restartedCalls.ts, turnHost.ts `#supply`), a ToolCall's own when it has one
// (activityProjection.ts `cutToolCall`). The whole path, on the bundled Claude Code against the
// fake endpoint, is test/sessionAffinity.temporal.test.ts "SIGTERM mid-turn: a turn still running
// past the grace", skipped since 2026-11-26 by the owner's decision (#7874); until it runs again,
// only these tests and sessions.e2e.test.ts "keeps a turn stopped by a restart" check the path.

const SID = '00000000-0000-4000-8000-000000000001'
let n = 0
const uuid = () => `00000000-0000-4000-8000-${String(++n).padStart(12, '0')}`

/** A main-transcript chain: each entry's parent is the one before. */
function chain(...entries: (Omit<SessionStoreEntry, 'uuid' | 'parentUuid'> & { type: string })[]): SessionStoreEntry[] {
  const out: SessionStoreEntry[] = []
  for (const e of entries) out.push({ ...e, uuid: uuid(), parentUuid: out.at(-1)?.uuid ?? null, isSidechain: false, sessionId: SID, cwd: '/w', version: '2.1.314' })
  return out
}
const prompt = (text: string) => ({ type: 'user', message: { role: 'user', content: text } })
const toolUse = (apiId: string, id: string, name: string) => ({ type: 'assistant', message: { id: apiId, role: 'assistant', content: [{ type: 'tool_use', id, name, input: {} }] } })
const text = (apiId: string, t: string) => ({ type: 'assistant', message: { id: apiId, role: 'assistant', content: [{ type: 'text', text: t }] } })
const result = (id: string) => ({ type: 'user', message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: id, content: 'ok' }] } })

describe('danglingCalls', () => {
  it("names each call of the last reply that has no result, and the chain's last entry", () => {
    const entries = chain(prompt('go'), text('m1', 'on it'), toolUse('m1', 'toolu_a', 'ToolSearch'), toolUse('m1', 'toolu_b', 'mcp__scadbuddy__render_model'), result('toolu_a'))
    // A subagent's entry and a metadata row do not count.
    entries.push({ type: 'assistant', uuid: uuid(), parentUuid: null, isSidechain: true, message: { id: 'sub', content: [{ type: 'tool_use', id: 'toolu_sub', name: 'Read' }] } })
    entries.push({ type: 'cost-state', sessionId: SID, totalCostUSD: 1 })
    const found = danglingCalls(entries)
    expect(found.calls).toEqual([{ id: 'toolu_b', name: 'mcp__scadbuddy__render_model' }])
    expect(found.leaf?.uuid).toBe(entries[4]!.uuid)
  })

  it('names none when every call has its result, or the last reply calls nothing', () => {
    expect(danglingCalls(chain(prompt('go'), toolUse('m1', 'toolu_a', 'Read'), result('toolu_a'))).calls).toEqual([])
    expect(danglingCalls(chain(prompt('go'), toolUse('m1', 'toolu_a', 'Read'), result('toolu_a'), text('m2', 'done'))).calls).toEqual([])
    expect(danglingCalls(chain(prompt('go'))).calls).toEqual([])
    expect(danglingCalls([]).calls).toEqual([])
  })

  it('never names a call of an earlier reply: its result could not follow its tool_use any more', () => {
    // An older reply whose call never got a result (a crash before this fix): Claude Code's to mark.
    const entries = chain(prompt('go'), toolUse('m1', 'toolu_old', 'Read'), prompt('again'), toolUse('m2', 'toolu_new', 'Grep'))
    expect(danglingCalls(entries).calls).toEqual([{ id: 'toolu_new', name: 'Grep' }])
  })
})

describe('the results a restarted placement gives', () => {
  const call = { id: 'toolu_x', name: 'ToolSearch' }

  it("says what is true of each end, and never Claude Code's refusal", () => {
    const blocks = [
      toolResultBlock(call, { kind: 'none' }),
      toolResultBlock(call, { kind: 'running' }),
      toolResultBlock(call, { kind: 'ended', status: 'cancelled' }),
    ]
    expect(blocks.map((b) => (b.content as { text: string }[])[0]!.text)).toEqual([
      restartedCallText('ToolSearch'),
      stillRunningText('ToolSearch', 'toolu_x'),
      endedText('ToolSearch', 'cancelled'),
    ])
    for (const b of blocks) {
      expect(b).toMatchObject({ type: 'tool_result', tool_use_id: 'toolu_x', is_error: true })
      expect(JSON.stringify(b)).not.toMatch(/doesn't want to proceed|rejected|permission/i)
    }
    expect(stillRunningText('render_model', 'toolu_x')).toContain('do not call it again')
  })

  it("gives a ToolCall's own result as Claude Code gives the API an MCP result: its text and its images", () => {
    const ok = toolResultBlock(call, {
      kind: 'result',
      result: { content: [{ type: 'text', text: 'found 3' }, { type: 'image', data: 'AAAA', mimeType: 'image/png' }] },
    })
    expect(ok).toEqual({
      type: 'tool_result',
      tool_use_id: 'toolu_x',
      content: [
        { type: 'text', text: 'found 3' },
        { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'AAAA' } },
      ],
    })
    const failed = toolResultBlock(call, { kind: 'result', result: { isError: true, content: [{ type: 'text', text: 'ToolSearch failed: boom' }] } })
    expect(failed).toMatchObject({ is_error: true, content: [{ type: 'text', text: 'ToolSearch failed: boom' }] })
  })

  it("is one user entry after the chain's last, in this session, in the shape of Claude Code's own", () => {
    const leaf = { type: 'assistant', uuid: 'u-leaf', parentUuid: 'u-0', sessionId: 'parent-session', cwd: '/w', version: '2.1.314', gitBranch: 'HEAD' }
    const block = toolResultBlock(call, { kind: 'none' })
    expect(resultsEntry(SID, leaf, [block], 'u-new', new Date('2026-11-26T01:55:35Z'))).toEqual({
      type: 'user',
      uuid: 'u-new',
      parentUuid: 'u-leaf',
      isSidechain: false,
      userType: 'external',
      sessionId: SID,
      cwd: '/w',
      version: '2.1.314',
      gitBranch: 'HEAD',
      timestamp: '2026-11-26T01:55:35.000Z',
      message: { role: 'user', content: [block] },
    })
  })
})

/** A stub client whose one ToolCall goes through `statuses`, one per describe, then stays at the last. */
function stubClient(statuses: string[] | 'missing', outcome: { result?: unknown; error?: Error } = {}) {
  let at = 0
  const handle = {
    describe: vi.fn(async () => {
      if (statuses === 'missing') throw new WorkflowNotFoundError('no such workflow', 'w', undefined)
      const name = statuses[Math.min(at++, statuses.length - 1)]!
      return { status: { name } }
    }),
    result: vi.fn(async () => {
      if (outcome.error) throw outcome.error
      return outcome.result
    }),
  }
  const getHandle = vi.fn(() => handle)
  return { client: { workflow: { getHandle } } as unknown as Client, handle, getHandle }
}

describe('cutToolCall', () => {
  const blobs = { get: async () => undefined }
  const call = { id: 'toolu_c', name: 'mcp__scadbuddy__render_model' }
  const wait = (over: Partial<{ waitMs: number; signal: AbortSignal; beat: () => void }> = {}) => ({ waitMs: 5_000, signal: new AbortController().signal, pollMs: 1, ...over })

  it("waits for a running call and gives its own result, by the call's workflow id", async () => {
    const { client, getHandle } = stubClient(['RUNNING', 'RUNNING', 'COMPLETED'], { result: { ok: true, content: [{ type: 'text', text: 'rendered' }] } })
    const beat = vi.fn()
    await expect(cutToolCall({ client, blobs }, SID, call, wait({ beat }))).resolves.toEqual({ kind: 'result', result: { content: [{ type: 'text', text: 'rendered' }] } })
    expect(getHandle).toHaveBeenCalledWith(toolCallWorkflowId(SID, 'toolu_c'))
    expect(beat).toHaveBeenCalled()
  })

  it('gives a failed call its failure, as the starter does', async () => {
    const { client } = stubClient(['FAILED'], { error: Object.assign(new Error('workflow failed'), { cause: new Error('the backend said no') }) })
    await expect(cutToolCall({ client, blobs }, SID, call, wait())).resolves.toEqual({
      kind: 'result',
      result: { isError: true, content: [{ type: 'text', text: 'mcp__scadbuddy__render_model failed: the backend said no' }] },
    })
  })

  it('says a call still runs past the wait, or once the turn stops, and never starts one', async () => {
    const { client, handle } = stubClient(['RUNNING'])
    await expect(cutToolCall({ client, blobs }, SID, call, wait({ waitMs: 20 }))).resolves.toEqual({ kind: 'running' })
    const stopped = new AbortController()
    stopped.abort()
    await expect(cutToolCall({ client, blobs }, SID, call, wait({ signal: stopped.signal }))).resolves.toEqual({ kind: 'running' })
    expect(handle.result).not.toHaveBeenCalled()
  })

  it('names a call that ended with no result, and one that was never a ToolCall', async () => {
    await expect(cutToolCall({ client: stubClient(['CANCELLED']).client, blobs }, SID, call, wait())).resolves.toEqual({ kind: 'ended', status: 'cancelled' })
    await expect(cutToolCall({ client: stubClient(['TIMED_OUT']).client, blobs }, SID, call, wait())).resolves.toEqual({ kind: 'ended', status: 'timed out' })
    await expect(cutToolCall({ client: stubClient('missing').client, blobs }, SID, call, wait())).resolves.toEqual({ kind: 'none' })
  })
})

describe('a ToolCall whose turn a shutdown cut', () => {
  it('is not cancelled when its turn stops, so the restarted placement reads its result; any other stop cancels it', async () => {
    const run = async (keeps: boolean) => {
      let finish!: (v: unknown) => void
      const handle = { result: () => new Promise((resolve) => (finish = resolve)), cancel: vi.fn(async () => {}) }
      const client = { workflow: { start: async () => handle, getHandle: () => handle } } as unknown as Client
      const start = toolCallStarter({ client, blobs: { get: async () => undefined }, keeps: () => keeps })
      const stop = new AbortController()
      const answer = start({ sessionId: SID, toolUseId: 'toolu_k', tool: 'bench', input: {}, signal: stop.signal })
      await vi.waitFor(() => expect(finish).toBeDefined())
      stop.abort()
      finish({ ok: true, content: [] })
      await answer
      return handle.cancel.mock.calls.length
    }
    expect(await run(true)).toBe(0)
    expect(await run(false)).toBe(1)
  })
})

describe('WarmSessions.seal', () => {
  it("drops the conversation's entries the process writes from then on, and lands its metadata", async () => {
    const appended: SessionStoreEntry[][] = []
    const inner: SessionStore = {
      append: async (_key, entries) => void appended.push(entries),
      load: async () => null,
    }
    let mirror: SessionStore | undefined
    const warm = new WarmSessions({
      store: { lastOfType: async () => null, head: async () => ({ exists: false, since: null }), moved: async () => false },
      maxEntries: 1,
      idleMs: 600_000,
      start: (run: HarnessRun) => {
        mirror = run.sessionStore
        return idle()
      },
    })
    const { entry } = await warm.acquire(SID, assembly(inner), { kind: 'anthropic_api_key', secret: 'sk-ant-test' }, 1)
    const key = { projectKey: '', sessionId: SID }
    await mirror!.append(key, [{ type: 'user', uuid: 'u-real', message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_a' }] } }])
    warm.seal(entry)
    // What Claude Code writes as it answers the interrupt, and as it exits.
    await mirror!.append(key, [
      { type: 'user', uuid: 'u-refused', parentUuid: 'u-real', message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_b', content: "The user doesn't want to proceed with this tool use." }] } },
      { type: 'user', uuid: 'u-interrupted', parentUuid: 'u-refused', message: { content: '[Request interrupted by user for tool use]' } },
    ])
    await mirror!.append(key, [{ type: 'last-prompt', sessionId: SID }, { type: 'cost-state', sessionId: SID, totalCostUSD: 1 }])
    // A subagent's own transcript is not the conversation's chain.
    await mirror!.append({ ...key, subpath: 'subagents/agent-1' }, [{ type: 'user', uuid: 'u-sub' }])
    expect(appended.flat().map((e) => e.uuid ?? e.type)).toEqual(['u-real', 'last-prompt', 'cost-state', 'u-sub'])
    await warm.close()
  })

  it('lands what comes before its first stopped call, the turn\'s prompt among them (sessions.e2e "keeps a turn stopped by a restart")', async () => {
    const appended: SessionStoreEntry[][] = []
    const inner: SessionStore = {
      append: async (_key, entries) => void appended.push(entries),
      load: async () => null,
    }
    let mirror: SessionStore | undefined
    const warm = new WarmSessions({
      store: { lastOfType: async () => null, head: async () => ({ exists: false, since: null }), moved: async () => false },
      maxEntries: 1,
      idleMs: 600_000,
      start: (run: HarnessRun) => {
        mirror = run.sessionStore
        return idle()
      },
    })
    const { entry } = await warm.acquire(SID, assembly(inner), { kind: 'anthropic_api_key', secret: 'sk-ant-test' }, 1)
    const key = { projectKey: '', sessionId: SID }
    warm.seal(entry)
    // The cut came while the request was with the model: Claude Code hands the prompt over only now.
    await mirror!.append(key, [{ type: 'user', uuid: 'u-prompt', message: { content: 'and the second word is kettle' } }])
    await mirror!.append(key, [
      { type: 'assistant', uuid: 'a-call', parentUuid: 'u-prompt', message: { content: [{ type: 'tool_use', id: 'toolu_c', name: 'ToolSearch' }] } },
      { type: 'user', uuid: 'u-refused', parentUuid: 'a-call', message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_c', content: "The user doesn't want to proceed with this tool use." }] } },
      { type: 'user', uuid: 'u-after', parentUuid: 'u-refused', message: { content: 'text after the refusal' } },
    ])
    expect(appended.flat().map((e) => e.uuid ?? e.type)).toEqual(['u-prompt', 'a-call'])
    await warm.close()
  })
})

/** A process that idles until closed. */
function idle(): WarmQuery {
  let wake: (() => void) | undefined
  let closed = false
  return {
    [Symbol.asyncIterator]: () => ({
      next: () => (closed ? Promise.resolve({ done: true, value: undefined }) : new Promise((resolve) => (wake = () => resolve({ done: true, value: undefined })))),
    }),
    interrupt: () => Promise.resolve(undefined),
    close: () => {
      closed = true
      wake?.()
    },
  }
}

function assembly(sessionStore: SessionStore): AssembledTurn {
  return {
    run: { paths: { stateDir: '/nonexistent' }, sessionStore },
    candidates: [],
    fingerprintParts: {
      credentialId: 'c1',
      credentialUpdatedAt: '1',
      model: null,
      effort: null,
      agentType: null,
      ownPlugin: null,
      plugins: [],
      packageHashes: [],
      allowedPackageHashes: [],
      tiers: null,
      owner: 'user:alice',
      systemAppend: '',
      systemPromptBase: 'claude_code',
      toolSearch: 'off',
      toolLoadings: [],
      disabledTools: [],
      raisedTiers: [],
      httpRequest: false,
      toolsAsActivities: false,
      httpTier: null,
      builtinTools: [],
      nativeTools: [],
      servedBuiltins: [],
      backgroundTasks: false,
      recallMaxQueryChars: null,
      transientRetries: null,
    },
    slot: createTurnSlot(),
    secrets: [],
    eventTierOf: () => undefined,
    shownTierOf: () => undefined,
    observe: () => Promise.resolve(),
    terminate: vi.fn(async () => {}),
    release: vi.fn(),
  } as unknown as AssembledTurn
}
