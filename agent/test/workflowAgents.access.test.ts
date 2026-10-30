import { describe, expect, it } from 'vitest'
import { offItemReason } from '../src/access/items.js'
import { type AccessRow, BUILTIN_SOURCES } from '../src/access/model.js'
import { managerAgents } from '../src/background/workflowAgents.js'

// #4757 Task 6 review I3: a Workflow agent() whose agentType is a subagent switched off in
// Settings → Tools & plugins is refused before its chat starts, in the call rule's words, in
// either mode (the child chat's turns leave the definition out too: turnRunner.packages.test.ts,
// agent-durable test_session_runner.py).

const OWNER = { kind: 'browser' as const, id: 'browser', label: 'You' }
const row = (item: string): AccessRow => ({ source: BUILTIN_SOURCES.ownPlugin, item, access: 'off', updated_by: 'browser:b' })

function agentsOver(rows: () => Promise<AccessRow[]>) {
  const started: unknown[] = []
  const manager = {
    startChild: async (...args: unknown[]) => {
      started.push(args)
      throw new Error('no chat in this test')
    },
  } as unknown as Parameters<typeof managerAgents>[0]
  const agents = managerAgents(manager, { agentTypes: async () => ['model-author', 'print-analyst'], access: { rows } })
  const run = (agentType: string) =>
    agents.run({ sessionId: 's', owner: OWNER, workflow: 'w', prompt: 'p', opts: { agentType }, signal: new AbortController().signal })
  return { run, started }
}

describe("a Workflow agent()'s subagent type by its access", () => {
  it('refuses a type that is switched off, naming it, and starts no chat', async () => {
    const { run, started } = agentsOver(async () => [row('subagent:print-analyst')])
    expect(await run('scadbuddy:print-analyst')).toEqual({
      ok: false,
      result: null,
      error: offItemReason({ source: BUILTIN_SOURCES.ownPlugin, item: 'subagent:print-analyst' }),
    })
    expect(started).toEqual([])
    // Another type is left alone.
    await expect(run('model-author')).rejects.toThrow('no chat in this test')
  })

  it('refuses every type while the own plugin is switched off', async () => {
    const { run } = agentsOver(async () => [row('*')])
    expect((await run('model-author')).error).toBe(offItemReason({ source: BUILTIN_SOURCES.ownPlugin, item: '*' }))
  })

  it('refuses when the access cannot be read', async () => {
    const { run, started } = agentsOver(async () => {
      throw new Error('down')
    })
    expect((await run('model-author')).error).toBe("Agent type 'model-author' is refused: its Settings → Tools & plugins entry cannot be read")
    expect(started).toEqual([])
  })
})
