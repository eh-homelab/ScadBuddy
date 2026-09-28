import { screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { SettingsPage } from '../pages/SettingsPage'
import { renderPage } from '../test/utils'
import { AgentBridge, bridge as appBridge } from './bridge'
import { useGlobalAgentTools } from './global'
import { connectWebMcp } from './webmcp'
import { isWebMcpEnabled, setWebMcpEnabled, WEBMCP_STORAGE_KEY } from './webmcpPreference'

interface Registered {
  name: string
  signal?: AbortSignal
  annotations?: { readOnlyHint?: boolean; consequentialHint?: boolean }
}

let registered: Registered[] = []

/** A stand-in for `document.modelContext` (the WebMCP draft's `registerTool(tool, { signal })`). */
beforeEach(() => {
  registered = []
  Object.defineProperty(document, 'modelContext', {
    configurable: true,
    value: {
      registerTool: async (
        tool: { name: string; annotations?: Registered['annotations'] },
        options?: { signal?: AbortSignal },
      ) => {
        registered.push({ name: tool.name, signal: options?.signal, annotations: tool.annotations })
      },
    },
  })
})

afterEach(() => {
  delete (document as { modelContext?: unknown }).modelContext
  setWebMcpEnabled(false)
  window.localStorage.clear()
})

/** What the browser would list now: registered and not yet aborted. */
function active(): string[] {
  return registered.filter((entry) => !entry.signal?.aborted).map((entry) => entry.name)
}

describe('WebMCP', () => {
  it('registers nothing until the user opts in, and unregisters when they opt out', async () => {
    const bridge = new AgentBridge()
    bridge.register({ search: () => [], open_print_dialog: () => null }, { label: 'page' })
    const disconnect = connectWebMcp(bridge)

    // Off by default: give any sync a chance to (wrongly) run.
    expect(isWebMcpEnabled()).toBe(false)
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(registered).toEqual([])

    setWebMcpEnabled(true)
    await waitFor(() => expect(active().sort()).toEqual(['open_print_dialog', 'search']))
    expect(registered.find((entry) => entry.name === 'open_print_dialog')?.annotations).toEqual({
      readOnlyHint: false,
      consequentialHint: true,
    })

    // A route change re-registers the live set, still only while on.
    const off = bridge.register({ get_params: () => null }, { label: 'customize' })
    await waitFor(() => expect(active()).toContain('get_params'))
    off()
    await waitFor(() => expect(active()).not.toContain('get_params'))

    setWebMcpEnabled(false)
    expect(active()).toEqual([])
    expect(registered.every((entry) => entry.signal?.aborted)).toBe(true)

    disconnect()
  })

  it('reads as off when storage is unavailable', () => {
    const original = Object.getOwnPropertyDescriptor(window, 'localStorage')
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new DOMException('blocked', 'SecurityError')
      },
    })
    try {
      expect(isWebMcpEnabled()).toBe(false)
      // A toggle still works for the page, just not across reloads.
      setWebMcpEnabled(true)
      expect(isWebMcpEnabled()).toBe(true)
      setWebMcpEnabled(false)
    } finally {
      if (original) Object.defineProperty(window, 'localStorage', original)
    }
  })
})

function Shell() {
  useGlobalAgentTools()
  return <SettingsPage />
}

describe('the Settings toggle', () => {
  const label = 'Let this browser’s built-in agent use ScadBuddy tools (WebMCP)'

  it('is off by default, stores the choice per browser, and an agent cannot flip it', async () => {
    const { user } = renderPage(<Shell />)
    const toggle = await screen.findByRole('checkbox', { name: label })
    expect(toggle).not.toBeChecked()

    const agent = await appBridge.call('click', { role: 'checkbox', name: label })
    expect(!agent.ok && agent.error.code).toBe('refused')
    expect(toggle).not.toBeChecked()

    await user.click(toggle)
    expect(toggle).toBeChecked()
    expect(window.localStorage.getItem(WEBMCP_STORAGE_KEY)).toBe('on')

    await user.click(toggle)
    expect(toggle).not.toBeChecked()
    expect(window.localStorage.getItem(WEBMCP_STORAGE_KEY)).toBeNull()
  })
})
