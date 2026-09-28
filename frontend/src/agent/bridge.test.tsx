import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { TOOLS } from './catalog'
import { AgentBridge } from './bridge'
import { AgentToolError, type ToolImpls } from './types'
import { AgentBridgeContext, useAgentHandlers } from './useAgentHandlers'

function Provider({ label, impls, describe }: { label: string; impls: ToolImpls; describe?: () => unknown }) {
  useAgentHandlers(label, impls, describe)
  return null
}

function mount(bridge: AgentBridge, props: { label: string; impls: ToolImpls; describe?: () => unknown }) {
  return render(
    <AgentBridgeContext.Provider value={bridge}>
      <Provider {...props} />
    </AgentBridgeContext.Provider>,
  )
}

describe('AgentBridge', () => {
  it('answers unknown_tool for a name no page could provide', async () => {
    const outcome = await new AgentBridge().call('rm_rf', {})
    expect(outcome).toEqual({
      ok: false,
      error: { code: 'unknown_tool', message: 'There is no tool called "rm_rf".' },
    })
  })

  it('answers unavailable, naming where the tool lives, until its page mounts', async () => {
    const bridge = new AgentBridge()
    bridge.setRoute('/settings')

    const before = await bridge.call('get_params', {})
    expect(before.ok).toBe(false)
    expect(!before.ok && before.error.code).toBe('unavailable')
    expect(!before.ok && before.error.message).toMatch(/customizer.*\/settings/)

    const view = mount(bridge, { label: 'customize', impls: { get_params: () => ({ params: [] }) } })
    expect(await bridge.call('get_params', {})).toEqual({ ok: true, result: { params: [] } })
    expect(bridge.liveNames()).toEqual(['get_params'])

    // Unmounting takes the handler away again — as an error, not silence.
    view.unmount()
    const after = await bridge.call('get_params', {})
    expect(!after.ok && after.error.code).toBe('unavailable')
    expect(bridge.liveNames()).toEqual([])
  })

  it('rejects arguments that do not match the schema, before the handler runs', async () => {
    const bridge = new AgentBridge()
    const calls: unknown[] = []
    mount(bridge, { label: 'customize', impls: { set_param: (args) => calls.push(args) } })

    const outcome = await bridge.call('set_param', { name: 'size', value: { nested: true } })
    expect(outcome.ok).toBe(false)
    if (outcome.ok) return
    expect(outcome.error.code).toBe('invalid_args')
    expect(outcome.error.issues?.map((issue) => issue.path)).toEqual(['value'])

    const extra = await bridge.call('set_param', { name: 'size', value: 3, sneaky: 1 })
    expect(!extra.ok && extra.error.code).toBe('invalid_args')
    expect(calls).toEqual([])
  })

  it('hands the handler parsed arguments, defaults applied', async () => {
    const bridge = new AgentBridge()
    let seen: unknown
    mount(bridge, { label: 'customize', impls: { render: (args) => (seen = args) } })
    await bridge.call('render', {})
    expect(seen).toEqual({ timeout_ms: 30_000 })
  })

  it('keeps a handler error typed, and turns anything else into failed', async () => {
    const bridge = new AgentBridge()
    mount(bridge, {
      label: 'page',
      impls: {
        search: () => {
          throw new AgentToolError('refused', 'no')
        },
        open_model: () => {
          throw new Error('boom')
        },
      },
    })
    expect(await bridge.call('search', { query: '' })).toEqual({
      ok: false,
      error: { code: 'refused', message: 'no' },
    })
    expect(await bridge.call('open_model', { slug: 'x' })).toEqual({
      ok: false,
      error: { code: 'failed', message: 'boom' },
    })
  })

  it('runs the newest mounted handler, and falls back when it goes', async () => {
    const bridge = new AgentBridge()
    mount(bridge, { label: 'first', impls: { search: () => 'first' } })
    const second = mount(bridge, { label: 'second', impls: { search: () => 'second' } })
    expect(await bridge.call('search', { query: '' })).toEqual({ ok: true, result: 'second' })
    second.unmount()
    expect(await bridge.call('search', { query: '' })).toEqual({ ok: true, result: 'first' })
  })

  it('sees the latest render of the component, not the one it registered in', async () => {
    const bridge = new AgentBridge()
    const view = mount(bridge, { label: 'page', impls: { search: () => 'old' } })
    view.rerender(
      <AgentBridgeContext.Provider value={bridge}>
        <Provider label="page" impls={{ search: () => 'new' }} />
      </AgentBridgeContext.Provider>,
    )
    expect(await bridge.call('search', { query: '' })).toEqual({ ok: true, result: 'new' })
  })

  it('lists the live tools with JSON schemas, or every tool marked live or not', async () => {
    const bridge = new AgentBridge()
    mount(bridge, { label: 'customize', impls: { set_param: () => null, render: () => null } })

    const live = await bridge.listTools()
    expect(live.map((tool) => tool.name).sort()).toEqual(['render', 'set_param'])
    const setParam = live.find((tool) => tool.name === 'set_param')
    expect(setParam).toMatchObject({ risk: 'write', scope: 'customize', live: true })
    expect(setParam?.inputSchema).toMatchObject({
      type: 'object',
      required: ['name', 'value'],
      properties: { name: { type: 'string' } },
    })
    // A default makes the field optional to the caller.
    const renderTool = live.find((tool) => tool.name === 'render')
    expect(renderTool?.inputSchema.required ?? []).not.toContain('timeout_ms')

    const all = await bridge.listTools({ all: true })
    expect(all.map((tool) => tool.name)).toEqual(Object.keys(TOOLS))
    expect(all.find((tool) => tool.name === 'open_print_dialog')).toMatchObject({
      risk: 'outward',
      live: false,
    })
  })

  it('tells subscribers when the live tools change', () => {
    const bridge = new AgentBridge()
    let changes = 0
    bridge.subscribe(() => changes++)
    const view = mount(bridge, { label: 'page', impls: { search: () => null } })
    const mounted = changes
    expect(mounted).toBeGreaterThan(0)
    view.unmount()
    expect(changes).toBeGreaterThan(mounted)
  })

  it('puts what each page describes under snapshot().page', () => {
    const bridge = new AgentBridge()
    bridge.setRoute('/m/name-keychain')
    mount(bridge, { label: 'customize', impls: { get_params: () => null }, describe: () => ({ slug: 'name-keychain' }) })
    const snapshot = bridge.snapshot()
    expect(snapshot.route).toBe('/m/name-keychain')
    expect(snapshot.page).toEqual({ customize: { slug: 'name-keychain' } })
    expect(snapshot.tools).toEqual(['get_params'])
  })
})

describe('the catalogue', () => {
  it('has one outward tool, and it only opens a dialog', () => {
    const outward = Object.entries(TOOLS)
      .filter(([, spec]) => spec.risk === 'outward')
      .map(([name]) => name)
    expect(outward).toEqual(['open_print_dialog'])
  })
})
