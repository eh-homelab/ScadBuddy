import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import type { HookInput } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import { liveAccess } from '../src/access/live.js'
import { type AccessRow, BUILTIN_SOURCES, packageServerSource, registrySource, remoteSource, SOURCE_ITEM } from '../src/access/model.js'
import { AccessResolver } from '../src/access/resolver.js'
import type { AccessReader } from '../src/access/store.js'
import { createBackendClient } from '../src/api/backend.js'
import { HTTP_TOOL, HTTP_TOOL_NAME, httpAccessTierOf } from '../src/harness/httpRequest.js'
import type { ApprovalGate } from '../src/harness/permissions.js'
import { buildHarnessOptions, type HarnessRun } from '../src/harness/run.js'
import { SeamLedger } from '../src/harness/seamLedger.js'
import { BuiltInTools } from '../src/plugins/builtInTools.js'
import { TOOL_USE_META } from '../src/telemetry/trace.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { defineTool, json, type Tool } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'
import { memoryAccess } from './support/memoryAccess.js'
import { turnRunnerForTest } from './support/turnRunner.js'

// Spec §4.4 (#4209, #2171): the classic seam decides each call from access as it is
// now, not as the turn read it. A tier lowered mid-turn stops parking on the next
// call, a raised one starts, `off` is refused; the in-process server's #2100 check
// compares against the tier the seam applied to this call (SeamLedger).

function bare(t: Tool): boolean {
  try {
    t.parse({})
    return true
  } catch {
    return false
  }
}
const readTool = ALL_TOOLS.find((t) => t.risk === 'read' && bare(t))!
const outwardTool = ALL_TOOLS.find((t) => t.risk === 'outward')!
const OWN = (tool: string) => `mcp__scadbuddy__${tool}`
const owner = { kind: 'browser' as const, id: 'b', label: 'You' }
const signal = new AbortController().signal
const BY = 'browser:b'

const base: HarnessRun = {
  paths: { stateDir: '/var/lib/scadbuddy-agent' },
  credential: { kind: 'anthropic_api_key', secret: 'sk-ant-api03-unit-test-key-000011112222' },
  prompt: 'hi',
}

const approveAll: ApprovalGate = async (request) => ({ approved: true, input: request.input })

/** A turn over `reader`: its policy read now (the turn's start), and the live seam over the same store. */
async function turnOver(reader: AccessReader, run: Partial<HarnessRun> = {}, tools: readonly Tool[] = ALL_TOOLS) {
  const ledger = new SeamLedger()
  const policy = await new BuiltInTools(reader).policy()
  const wired = harnessTools(services(), tools)
  const options = buildHarnessOptions({
    ...base,
    tierOf: wired.tierOf,
    builtInPolicy: policy,
    live: { access: liveAccess(new AccessResolver(reader, tools), { tools, remote: ['hs'], packageServers: [] }), ledger },
    ...run,
  })
  const ask = (name: string, toolUseID = 't', input: Record<string, unknown> = {}) =>
    options.canUseTool!(name, input, { signal, toolUseID, requestId: 'r' })
  const hook = (name: string, toolUseId = 'toolu_1') =>
    options.hooks!.PreToolUse![0]!.hooks[0]!(
      { hook_event_name: 'PreToolUse', tool_name: name, tool_input: {}, tool_use_id: toolUseId, session_id: 's', transcript_path: '/dev/null', cwd: '/tmp' } as HookInput,
      toolUseId,
      { signal },
    )
  return { ledger, policy, ask, hook }
}

async function listVia(server: McpServer) {
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
  await server.connect(serverSide)
  const client = new Client({ name: 'access-live-test', version: '0' })
  await client.connect(clientSide)
  return client
}

const row = (source: string, item: string, access: string): AccessRow => ({ source, item, access, updated_by: BY })
/** A remote plugin a turn offers is switched on. */
const on = (source: string): AccessRow => row(source, SOURCE_ITEM, 'on')
const browserTool = ALL_TOOLS.find((t) => t.name.startsWith('browser_') && bare(t))!

describe('the classic seam reads access per call', () => {
  it('a tier lowered mid-turn stops parking on the next call', async () => {
    const { access } = memoryAccess([row(registrySource(outwardTool.name), outwardTool.name, 'outward')])
    const { ask } = await turnOver(access)
    expect(await ask(OWN(outwardTool.name))).toMatchObject({ behavior: 'deny', message: expect.stringMatching(/approval/) })
    await access.setItem(registrySource(outwardTool.name), outwardTool.name, 'write')
    expect(await ask(OWN(outwardTool.name))).toMatchObject({ behavior: 'allow' })
  })

  it('a tier raised mid-turn parks the next call', async () => {
    const { access } = memoryAccess()
    const { ask, hook } = await turnOver(access)
    expect(await ask(OWN(readTool.name))).toMatchObject({ behavior: 'allow' })
    await access.setItem(registrySource(readTool.name), readTool.name, 'outward')
    expect(await ask(OWN(readTool.name))).toMatchObject({ behavior: 'deny', message: expect.stringMatching(/approval/) })
    expect(await hook(OWN(readTool.name))).toMatchObject({ hookSpecificOutput: { permissionDecision: 'deny' } })
  })

  it('a tool set off mid-turn is refused at the seam', async () => {
    const { access } = memoryAccess()
    const { ask, hook } = await turnOver(access, { approvalGate: approveAll })
    await access.setItem(registrySource(readTool.name), readTool.name, 'off')
    expect(await ask(OWN(readTool.name))).toMatchObject({ behavior: 'deny', message: expect.stringMatching(/disabled in Settings/) })
    expect(await hook(OWN(readTool.name))).toMatchObject({
      hookSpecificOutput: { permissionDecision: 'deny', permissionDecisionReason: expect.stringMatching(/disabled in Settings/) },
    })
  })

  it('a call the seam parked after a mid-turn raise runs once approved', async () => {
    const { access } = memoryAccess()
    let calls = 0
    const backend = createBackendClient(BACKEND, async () => {
      calls++
      return Response.json({})
    })
    const svc = services({ backend, toolOverrides: new BuiltInTools(access).registryOverrides() })
    const { ask, ledger, policy } = await turnOver(access, { approvalGate: approveAll })
    await access.setItem(registrySource(readTool.name), readTool.name, 'outward')
    // Parked by the seam at its tier now, and approved.
    expect(await ask(OWN(readTool.name), 'toolu_1')).toMatchObject({ behavior: 'allow' })
    expect(ledger.tierOf('toolu_1')).toBe('outward')

    const servers = harnessTools(svc).mcpServers({ owner }, undefined, { builtIn: policy, ledger })
    const client = await listVia(servers.scadbuddy!.instance)
    const run = JSON.stringify(await client.callTool({ name: readTool.name, arguments: {}, _meta: { [TOOL_USE_META]: 'toolu_1' } }))
    expect(run).not.toMatch(/raised to \\"outward\\" during this turn/)
    expect(calls).toBeGreaterThan(0)

    // A call the seam never decided keeps the turn policy's tier: #2100 still refuses it.
    const before = calls
    const unasked = JSON.stringify(await client.callTool({ name: readTool.name, arguments: {}, _meta: { [TOOL_USE_META]: 'toolu_2' } }))
    await client.close()
    expect(unasked).toMatch(/raised to \\"outward\\" during this turn/)
    expect(calls).toBe(before)
  })

  it('a remote tool lowered mid-turn stops parking', async () => {
    const { access } = memoryAccess([on(remoteSource('hs'))])
    const { ask } = await turnOver(access, {
      remotePlugins: [{ name: 'hs', url: 'http://127.0.0.1:1/p/token', toolTiers: {}, disabledTools: [] }],
    })
    expect(await ask('mcp__hs__recall')).toMatchObject({ behavior: 'deny', message: expect.stringMatching(/approval/) })
    await access.setItem(remoteSource('hs'), 'recall', 'read')
    expect(await ask('mcp__hs__recall')).toMatchObject({ behavior: 'allow' })
  })

  it('a remote plugin switched off mid-turn is refused at the seam', async () => {
    const { access } = memoryAccess([on(remoteSource('hs')), row(remoteSource('hs'), 'recall', 'read')])
    const { ask, hook } = await turnOver(access, {
      approvalGate: approveAll,
      remotePlugins: [{ name: 'hs', url: 'http://127.0.0.1:1/p/token', toolTiers: { recall: 'read' }, disabledTools: [] }],
    })
    expect(await ask('mcp__hs__recall')).toMatchObject({ behavior: 'allow' })
    await access.setEnabled(remoteSource('hs'), false)
    expect(await ask('mcp__hs__recall')).toMatchObject({ behavior: 'deny', message: expect.stringMatching(/disabled in Settings/) })
    expect(await hook('mcp__hs__recall')).toMatchObject({ hookSpecificOutput: { permissionDecision: 'deny' } })
  })

  it('a browser tool is refused at the seam once builtin:browser is switched off mid-turn', async () => {
    const { access } = memoryAccess()
    const { ask } = await turnOver(access, { approvalGate: approveAll })
    expect(await ask(OWN(browserTool.name))).not.toMatchObject({ message: expect.stringMatching(/disabled in Settings/) })
    await access.setEnabled(BUILTIN_SOURCES.browser, false)
    expect(await ask(OWN(browserTool.name))).toMatchObject({ behavior: 'deny', message: expect.stringMatching(/disabled in Settings/) })
  })

  it('an http_request item reset mid-turn parks a POST again', async () => {
    const { access } = memoryAccess([row(BUILTIN_SOURCES.http, HTTP_TOOL, 'read')])
    // The turn read http_request's item at its start: `read`, whatever the method.
    const { ask } = await turnOver(access, { tierOf: httpAccessTierOf('read') })
    const post = { method: 'POST', url: 'https://example.com/' }
    expect(await ask(HTTP_TOOL_NAME, 't', post)).toMatchObject({ behavior: 'allow' })
    await access.resetItem(BUILTIN_SOURCES.http, HTTP_TOOL)
    expect(await ask(HTTP_TOOL_NAME, 't', post)).toMatchObject({ behavior: 'deny', message: expect.stringMatching(/approval/) })
    expect(await ask(HTTP_TOOL_NAME, 't', { method: 'GET', url: 'https://example.com/' })).toMatchObject({ behavior: 'allow' })
  })

  it('the seam fails closed when access cannot be read', async () => {
    const { access } = memoryAccess()
    let down = false
    const reader: AccessReader = { rows: (sources) => (down ? Promise.reject(new Error('db down')) : access.rows(sources)) }
    const { ask, hook } = await turnOver(reader, { approvalGate: approveAll })
    down = true
    expect(await ask(OWN(readTool.name))).toMatchObject({ behavior: 'deny', message: expect.stringMatching(/cannot be read/) })
    expect(await hook(OWN(readTool.name))).toMatchObject({
      hookSpecificOutput: { permissionDecision: 'deny', permissionDecisionReason: expect.stringMatching(/cannot be read/) },
    })
  })

  it('a resolving tool lowered mid-turn runs without its prepareApproval', async () => {
    const prepare = vi.fn(async () => ({ input: {}, summary: 'sends' }))
    const send = defineTool({
      name: 'send_it',
      label: 'send_it',
      description: 'sends',
      input: z.object({}),
      risk: 'outward',
      routes: [],
      prepareApproval: prepare,
      handler: async () => json({ sent: true }),
    })
    const { access } = memoryAccess()
    const wired = harnessTools(services({ toolOverrides: new AccessResolver(access, [send]).registryOverrides() }), [send])
    const { ask } = await turnOver(access, { approvalGate: approveAll, prepareApproval: wired.prepareApproval({ owner }) }, [send])
    await access.setItem(registrySource(send.name), send.name, 'write')
    expect(await ask(OWN(send.name))).toMatchObject({ behavior: 'allow' })
    expect(prepare).not.toHaveBeenCalled()
  })
})

describe('liveAccess', () => {
  it("reads each call from its own source, and leaves what ai_access does not cover to the turn", async () => {
    const { access } = memoryAccess([
      on(remoteSource('hs')),
      row(packageServerSource('pkg', 'srv'), 'find', 'read'),
      row(BUILTIN_SOURCES.http, HTTP_TOOL, 'write'),
    ])
    const read = liveAccess(new AccessResolver(access), {
      tools: [readTool],
      remote: ['hs'],
      packageServers: [{ name: 'plugin_pkg_srv', pkg: 'pkg', server: 'srv' }],
    })
    expect(await read(OWN(readTool.name))).toBe('read')
    expect(await read('mcp__hs__recall')).toBe('outward')
    expect(await read('mcp__plugin_pkg_srv__find')).toBe('read')
    expect(await read(HTTP_TOOL_NAME)).toBe('write')
    await access.setEnabled(BUILTIN_SOURCES.http, false)
    expect(await read(HTTP_TOOL_NAME)).toBe('off')
    await access.forget(BUILTIN_SOURCES.http)
    expect(await read(HTTP_TOOL_NAME, { method: 'DELETE' })).toBe('outward')
    expect(await read(HTTP_TOOL_NAME, { method: 'HEAD' })).toBe('read')
    // A package server has no switch here yet (#4757): its tools answer with no '*' row.
    expect(await read('mcp__plugin_pkg_srv__find')).toBe('read')
    await access.setEnabled(remoteSource('hs'), false)
    expect(await read('mcp__hs__recall')).toBe('off')
    for (const name of ['Skill', 'Agent', 'Bash', OWN(outwardTool.name), 'mcp__other__x']) expect(await read(name)).toBeUndefined()
  })
})

describe('a classic turn (TurnRunner)', () => {
  it("gives the seam each call's access, and each turn a ledger of its own", async () => {
    // The turn's own tiers say read; ai_access says off, and the seam reads that.
    const { runner, session, principal, gateCalls } = await turnRunnerForTest({
      pluginToolTiers: { recall: 'read' },
      access: [on(remoteSource('hindsight')), row(remoteSource('hindsight'), 'recall', 'off')],
    })
    const turn = await runner.assemble(session, principal)
    turn.release()
    const live = turn.run.live!
    expect(await live.access('mcp__hindsight__recall')).toBe('off')
    turn.slot.set(gateCalls.context('turn-1'))
    // The turn's seam, without the plugin directories this rig names but never creates.
    const options = buildHarnessOptions({ ...base, tierOf: turn.run.tierOf!, remotePlugins: turn.run.remotePlugins!, live })
    expect(await options.canUseTool!('mcp__hindsight__recall', {}, { signal, toolUseID: 'toolu_1', requestId: 'r' })).toMatchObject({
      behavior: 'deny',
      message: expect.stringMatching(/disabled in Settings/),
    })
    live.ledger!.record('toolu_2', 'outward')
    expect(live.ledger!.tierOf('toolu_2')).toBe('outward')
    // The next turn the process serves starts with an empty ledger.
    turn.slot.set(gateCalls.context('turn-2'))
    expect(live.ledger!.tierOf('toolu_2')).toBeUndefined()
    turn.slot.clear()
    live.ledger!.record('toolu_3', 'read')
    expect(live.ledger!.tierOf('toolu_3')).toBeUndefined()
  })
})
