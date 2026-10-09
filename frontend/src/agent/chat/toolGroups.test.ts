import { describe, expect, it } from 'vitest'
import type { FeedItem } from './state'
import { feedBlocks, groupStatus, isLiveStatus, toolStatus, type ToolStatus } from './toolGroups'

type Tool = Extract<FeedItem, { kind: 'tool' }>

const tool = (id: string, extra: Partial<Tool> = {}): Tool => ({ kind: 'tool', id, name: 'mcp__scadbuddy__get_model', input: {}, risk: 'read', ...extra })
const done = (id: string, ok = true): Tool => tool(id, { result: { ok, summary: ok ? 'ok' : 'boom', sources: [] } })
const text = (id: string): FeedItem => ({ kind: 'assistant', id, text: 'hi', done: true })
const approval = (id: string, toolId: string, state: Extract<FeedItem, { kind: 'approval' }>['state']): FeedItem => ({
  kind: 'approval',
  id,
  tool: toolId,
  summary: 'Print?',
  state,
})
const question = (id: string, toolId: string, state: Extract<FeedItem, { kind: 'question' }>['state']): FeedItem => ({
  kind: 'question',
  id,
  tool: toolId,
  questions: [],
  state,
})

describe('feedBlocks (#782)', () => {
  it('groups consecutive tool calls, and anything else ends a group', () => {
    const items: FeedItem[] = [text('m1'), done('t1'), done('t2'), text('m2'), done('t3'), done('t4'), done('t5')]
    const blocks = feedBlocks(items)
    expect(blocks.map((b) => (b.kind === 'tools' ? b.calls.map((c) => c.id) : b.item.id))).toEqual([
      'm1',
      ['t1', 't2'],
      'm2',
      ['t3', 't4', 't5'],
    ])
  })

  it('keeps approval and question cards out of every group, where they are', () => {
    const items: FeedItem[] = [done('t1'), tool('t2'), approval('a1', 't2', 'pending'), tool('t3'), question('q1', 't3', 'pending')]
    expect(feedBlocks(items).map((b) => (b.kind === 'tools' ? b.calls.map((c) => c.id) : b.item.id))).toEqual([
      ['t1', 't2'],
      'a1',
      ['t3'],
      'q1',
    ])
  })

  it('keys a group by its first call, so it keeps its key as calls join it', () => {
    expect(feedBlocks([done('t1')])[0]).toMatchObject({ kind: 'tools', id: 't1' })
    expect(feedBlocks([done('t1'), tool('t2')])[0]).toMatchObject({ kind: 'tools', id: 't1' })
  })
})

describe('toolStatus (#782)', () => {
  const statusOf = (items: FeedItem[], id: string): ToolStatus => toolStatus(items.find((i) => i.id === id) as Tool, items)

  it('is done or failed from the result, whatever else names the call', () => {
    expect(statusOf([done('t1')], 't1')).toBe('done')
    expect(statusOf([done('t1', false)], 't1')).toBe('failed')
    expect(statusOf([done('t1'), approval('a1', 't1', 'approved')], 't1')).toBe('done')
  })

  it('waits on its approval while that is pending or on its way', () => {
    expect(statusOf([tool('t1'), approval('a1', 't1', 'pending')], 't1')).toBe('waiting_approval')
    expect(statusOf([tool('t1'), approval('a1', 't1', 'sent')], 't1')).toBe('waiting_approval')
    expect(statusOf([tool('t1'), approval('a1', 't1', 'approved')], 't1')).toBe('running')
  })

  it('did not run when its approval was denied, expired or cancelled and no result came', () => {
    for (const state of ['denied', 'expired', 'cancelled', 'closed'] as const) {
      expect(statusOf([tool('t1'), approval('a1', 't1', state)], 't1')).toBe('not_run')
    }
  })

  it('waits for the user while its question is open', () => {
    expect(statusOf([tool('t1'), question('q1', 't1', 'pending')], 't1')).toBe('waiting_input')
    expect(statusOf([tool('t1'), question('q1', 't1', 'answered')], 't1')).toBe('running')
  })

  it('is running otherwise; in a settled session a call with no result stopped', () => {
    expect(statusOf([tool('t1')], 't1')).toBe('running')
    expect(toolStatus(tool('t1'), [tool('t1')], { settled: true })).toBe('stopped')
  })
})

describe('groupStatus (#782)', () => {
  it('is the first live call’s, then failed if any failed, else done', () => {
    expect(groupStatus(['done', 'waiting_approval', 'running'])).toBe('waiting_approval')
    expect(groupStatus(['done', 'failed', 'done'])).toBe('failed')
    expect(groupStatus(['done', 'not_run'])).toBe('not_run')
    expect(groupStatus(['done', 'done'])).toBe('done')
    expect(isLiveStatus('running')).toBe(true)
    expect(isLiveStatus('waiting_input')).toBe(true)
    expect(isLiveStatus('not_run')).toBe(false)
  })
})
