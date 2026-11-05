import { readFile } from 'node:fs/promises'
import { defaultPayloadConverter, type PayloadCodec } from '@temporalio/common'
import { describe, expect, it } from 'vitest'
import { decodeWith, rebuildConversation, resumeTranscript, turnToolName } from '../src/sessions/recoverDurable.js'

// #4772: a closed DurableSession's conversation rebuilt from its run's history, as the plugin's
// workflow built it (temporalio-claude-agent-sdk `_workflow.py` `_loop`), and the results its next
// segment would have delivered. The history here is made by hand from Step 7.1's recorded
// conversation (fixtures/durable_conversation/); recoverDurable.temporal.test.ts reads a real one.

const FIXTURE = new URL('./fixtures/durable_conversation/', import.meta.url)
type Entry = { type: string; uuid?: string; sessionId?: string; attachment?: { type?: string; toolUseID?: string }; message?: { content?: unknown } }
type Event = Record<string, unknown>

async function entries(): Promise<Entry[]> {
  return (await readFile(new URL('entries.jsonl', FIXTURE), 'utf8'))
    .split('\n')
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Entry)
}

const payloads = (value: unknown) => ({ payloads: [defaultPayloadConverter.toPayload(value)] })
const identity: PayloadCodec = { encode: async (p) => p, decode: async (p) => p }
const decode = decodeWith(identity)

/** A history written event by event: activities scheduled and closed, as Temporal records them. */
function history() {
  const events: Event[] = []
  let id = 0
  const add = (e: Event) => {
    events.push({ eventId: String(++id), ...e })
    return String(id)
  }
  return {
    events,
    schedule: (name: string, activityId: string, input: unknown) => add({ activityTaskScheduledEventAttributes: { activityType: { name }, activityId, input: payloads(input) } }),
    complete: (scheduledEventId: string, result: unknown) => add({ activityTaskCompletedEventAttributes: { scheduledEventId, result: payloads(result) } }),
    fail: (scheduledEventId: string, message: string) =>
      add({ activityTaskFailedEventAttributes: { scheduledEventId, failure: { message: 'Activity task failed', cause: { message, applicationFailureInfo: {} } } } }),
  }
}

/** The first segment of the fixture's first turn: it pauses at get_schema (toolu_000001), its deferral marker the checkpoint. */
async function pausedAtGetSchema() {
  const all = await entries()
  const marker = all.findIndex((e) => e.attachment?.type === 'hook_deferred_tool' && e.attachment.toolUseID === 'toolu_000001')
  const added = all.slice(0, marker + 1)
  const h = history()
  const seg = h.schedule('run_claude_segment', '1', { conversation: { entries: 0 }, injected: {} })
  h.complete(seg, {
    session_id: 'claude-session',
    checkpoint: all[marker]!.uuid,
    transcript_keep: 0,
    transcript_add: added,
    deferred: { id: 'toolu_000001', name: 'get_schema', input: {}, kind: 'durable' },
    siblings: [],
    is_error: false,
  })
  return { h, added }
}

/** The tool result the cut appends after the paused call's marker. */
function heldResult(out: Entry[] | undefined): { content: unknown; is_error?: boolean } | undefined {
  const last = out?.at(-1)
  const block = (last?.message?.content as { type?: string; tool_use_id?: string; content?: unknown; is_error?: boolean }[] | undefined)?.[0]
  return block?.type === 'tool_result' && block.tool_use_id === 'toolu_000001' ? { content: block.content, ...(block.is_error ? { is_error: true } : {}) } : undefined
}

describe('rebuilding a closed DurableSession conversation from its history', () => {
  it('folds each committed segment: keep the first entries, append what it added', async () => {
    const all = await entries()
    const h = history()
    const first = h.schedule('run_claude_segment', '1', { conversation: { entries: 0 } })
    h.complete(first, { session_id: 's', checkpoint: all[10]!.uuid, transcript_keep: 0, transcript_add: all.slice(0, 12), is_error: false })
    // A segment that failed commits nothing.
    const failed = h.schedule('run_claude_segment', '2', { conversation: { entries: 12 } })
    h.fail(failed, 'the model segment failed')
    // One that rewrote its tail keeps fewer than it was given.
    const second = h.schedule('run_claude_segment', '3', { conversation: { entries: 12 } })
    h.complete(second, { session_id: 's', checkpoint: all.at(-2)!.uuid, transcript_keep: 10, transcript_add: all.slice(10), is_error: false })
    const rebuilt = await rebuildConversation(h.events as never, decode)
    expect(rebuilt).toMatchObject({ segments: 3, committed: 2, pending: 'none', consistent: true })
    expect(rebuilt.conversation?.entries).toEqual(all)
    expect(rebuilt.conversation?.checkpoint).toBe(all.at(-2)!.uuid)
  })

  it("a run that closed at a paused call delivers the call's activity result after its marker", async () => {
    const { h, added } = await pausedAtGetSchema()
    const tool = h.schedule('get_schema', 'tool-toolu_000001', {})
    h.complete(tool, { schema: 'name' })
    const rebuilt = await rebuildConversation(h.events as never, decode)
    expect(rebuilt).toMatchObject({ committed: 1, pending: 'tool_activities' })
    expect(rebuilt.conversation?.pending).toEqual({ toolu_000001: { content: { schema: 'name' }, is_error: false, blocks: null } })
    const out = resumeTranscript(rebuilt.conversation!, 'b2c3d4e5-0000-4000-8000-000000000000') as Entry[]
    expect(out).toHaveLength(added.length + 1)
    expect(heldResult(out)).toEqual({ content: JSON.stringify({ schema: 'name' }) })
    expect(new Set(out.map((e) => e.sessionId).filter(Boolean))).toEqual(new Set(['b2c3d4e5-0000-4000-8000-000000000000']))
    // The call, and its deferral marker, name the tool as an AgentSession's turn offers it.
    const call = out.flatMap((e) => (Array.isArray(e.message?.content) ? (e.message.content as { type?: string; id?: string; name?: string }[]) : [])).find((b) => b.type === 'tool_use' && b.id === 'toolu_000001')
    expect(call?.name).toBe(turnToolName('mcp__durable__get_schema'))
    expect(call?.name).toBe('mcp__scadbuddy__get_schema')
    expect((out.find((e) => e.attachment?.type === 'hook_deferred_tool')?.attachment as { toolName?: string }).toolName).toBe('mcp__scadbuddy__get_schema')
  })

  it("a failed call's error, and a call that never finished, are what the plugin would have told Claude", async () => {
    {
      const { h } = await pausedAtGetSchema()
      h.fail(h.schedule('get_schema', 'tool-toolu_000001', {}), 'no such model')
      expect(heldResult(resumeTranscript((await rebuildConversation(h.events as never, decode)).conversation!, 'x') as Entry[])).toEqual({
        content: 'Tool failed: no such model',
        is_error: true,
      })
    }
    {
      const { h } = await pausedAtGetSchema()
      h.schedule('get_schema', 'tool-toolu_000001', {})
      expect(heldResult(resumeTranscript((await rebuildConversation(h.events as never, decode)).conversation!, 'x') as Entry[])?.content).toMatch(/^This tool call was interrupted/)
    }
    {
      const { h } = await pausedAtGetSchema()
      expect(heldResult(resumeTranscript((await rebuildConversation(h.events as never, decode)).conversation!, 'x') as Entry[])?.content).toMatch(/^This tool call did not run/)
    }
  })

  it("a next segment that was scheduled delivers its own injected results, and the derived ones are checked against them", async () => {
    const { h, added } = await pausedAtGetSchema()
    h.complete(h.schedule('get_schema', 'tool-toolu_000001', {}), { schema: 'name' })
    const held = { toolu_000001: { content: { schema: 'name' }, is_error: false, blocks: null } }
    h.fail(h.schedule('run_claude_segment', '2', { conversation: { entries: added.length }, injected: held }), 'the model segment failed')
    const rebuilt = await rebuildConversation(h.events as never, decode)
    expect(rebuilt).toMatchObject({ pending: 'next_segment', derivedChecked: 1, derivedMatched: 1, consistent: true })
    expect(rebuilt.conversation?.pending).toEqual(held)
  })

  it('a segment given another conversation than the rebuild holds marks the rebuild inconsistent', async () => {
    const { h } = await pausedAtGetSchema()
    h.schedule('run_claude_segment', '2', { conversation: { entries: 3 }, injected: {} })
    expect((await rebuildConversation(h.events as never, decode)).consistent).toBe(false)
  })

  it('a run that continued as new is inconsistent: its first segment was given a conversation this run never built', async () => {
    const all = await entries()
    const h = history()
    const seg = h.schedule('run_claude_segment', '1', { conversation: { entries: 5 }, injected: {} })
    h.complete(seg, { session_id: 's', checkpoint: all.at(-2)!.uuid, transcript_keep: 5, transcript_add: all.slice(5), is_error: false })
    expect((await rebuildConversation(h.events as never, decode)).consistent).toBe(false)
  })

  it('derived held results that disagreed with a later segment are not trusted for the last pause', async () => {
    const all = await entries()
    const first = all.findIndex((e) => e.attachment?.type === 'hook_deferred_tool' && e.attachment.toolUseID === 'toolu_000001')
    const second = all.findIndex((e) => e.attachment?.type === 'hook_deferred_tool' && e.attachment.toolUseID === 'toolu_000003')
    const h = history()
    h.complete(h.schedule('run_claude_segment', '1', { conversation: { entries: 0 } }), {
      session_id: 's',
      checkpoint: all[first]!.uuid,
      transcript_keep: 0,
      transcript_add: all.slice(0, first + 1),
      deferred: { id: 'toolu_000001', name: 'get_schema', input: {} },
      is_error: false,
    })
    h.complete(h.schedule('get_schema', 'tool-toolu_000001', {}), { schema: 'name' })
    // The next segment delivered something else than the activity returned.
    const next = h.schedule('run_claude_segment', '2', { conversation: { entries: first + 1 }, injected: { toolu_000001: { content: 'other', is_error: false, blocks: null } } })
    h.complete(next, {
      session_id: 's',
      checkpoint: all[second]!.uuid,
      transcript_keep: first + 1,
      transcript_add: all.slice(first + 1, second + 1),
      deferred: { id: 'toolu_000003', name: 'render_model', input: {} },
      is_error: false,
    })
    h.complete(h.schedule('render_model', 'tool-toolu_000003', {}), { rendered: true })
    const rebuilt = await rebuildConversation(h.events as never, decode)
    expect(rebuilt).toMatchObject({ pending: 'tool_activities', derivedChecked: 1, derivedMatched: 0, consistent: false })
  })

  it('a run whose segments never committed has no conversation', async () => {
    const h = history()
    h.fail(h.schedule('run_claude_segment', '1', { conversation: { entries: 0 } }), 'the model segment failed')
    expect(await rebuildConversation(h.events as never, decode)).toMatchObject({ conversation: undefined, segments: 1, committed: 0 })
  })
})
