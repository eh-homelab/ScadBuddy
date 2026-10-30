import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { isUserOnly } from '../../../agent/dom'
import { aiPlugins } from '../../../api/aiPlugins'
import { aiCapabilities } from '../../../api/capabilities'
import { server } from '../../../mocks/server'
import { accessName, choose, optionsOf } from '../../../test/select'
import { enter, renderSettingsSection } from '../testing'
import { Component as BuiltIn } from './builtin'

// Radix Select scrolls its selected option into view; jsdom has no layout.
Element.prototype.scrollIntoView ??= () => {}

const availability = vi.hoisted(() => ({ available: true }))
vi.mock('../../../agent/chat/availability', () => ({
  useAiAvailability: () => availability,
}))

const route = '/settings/tools'
const WEBMCP = 'This browser’s agent (WebMCP)'
const WEBMCP_SWITCH = 'Let this browser’s built-in agent use ScadBuddy tools (WebMCP)'

const card = (title: string) => screen.findByRole('region', { name: `Source ${title}` })
const select = (name: string) => screen.getByRole('combobox', { name: accessName(name) })

async function itemAccess(source: string, item: string) {
  const sources = await aiCapabilities.list()
  return sources.find((s) => s.id === source)?.items.find((i) => i.id === item)?.access
}

async function onlyWebMcp() {
  expect(await screen.findByRole('region', { name: WEBMCP })).toBeInTheDocument()
  expect(screen.getByRole('switch', { name: WEBMCP_SWITCH })).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: /^Source / })).toBeNull()
}

describe('Built-in (#2171)', () => {
  afterEach(() => {
    availability.available = true
    vi.restoreAllMocks()
  })

  it("shows ScadBuddy's tools, the browser tab with its origin lists, HTTP requests and ScadBuddy's skills and subagents", async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    const regions = await screen.findAllByRole('region', { name: /^Source / })
    expect(regions.map((r) => r.getAttribute('aria-label'))).toEqual([
      'Source ScadBuddy tools',
      'Source Your browser tab',
      'Source HTTP requests',
      'Source ScadBuddy skills and subagents',
    ])
    // ScadBuddy's own tools have no switch (#4757).
    expect(within(await card('ScadBuddy tools')).queryByRole('switch')).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Your browser tab' }))
    const browser = await card('Your browser tab')
    expect(await within(browser).findByTestId('browser-origins')).toBeInTheDocument()
    expect(within(browser).getByRole('combobox', { name: accessName('browser_click') })).toBeInTheDocument()
  })

  it("keeps the browser tab's unsaved origin lists through a fold", async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    await card('Your browser tab')
    const fold = screen.getByRole('button', { name: 'Your browser tab' })
    await user.click(fold)
    const allow = await screen.findByLabelText('Opens without asking')
    await enter(user, allow, '\nprinter.lan')
    await user.click(fold)
    expect(screen.getByLabelText('Opens without asking')).not.toBeVisible()
    await user.click(fold)
    expect((screen.getByLabelText('Opens without asking') as HTMLTextAreaElement).value).toMatch(/printer\.lan$/)
  })

  it("says the camera's privacy switch is get_printer_camera's access", async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    const scadbuddy = await card('ScadBuddy tools')
    await user.click(screen.getByRole('button', { name: 'ScadBuddy tools' }))
    expect(within(scadbuddy).getByText(/Set “See a printer’s camera” to off/)).toBeVisible()
  })

  it('says it is loading until the tools are read', async () => {
    let release!: () => void
    const held = new Promise<void>((resolve) => (release = resolve))
    server.use(
      // Answers nothing once released, so the feature's own handler answers.
      http.get('/api/v1/ai/capabilities', async () => {
        await held
      }),
    )
    renderSettingsSection(<BuiltIn />, { route })
    expect(await screen.findByText('Loading the built-in tools…')).toHaveAttribute('role', 'status')
    release()
    await card('ScadBuddy tools')
    expect(screen.queryByText('Loading the built-in tools…')).toBeNull()
  })

  it('a failed read other than 503 says so, and keeps the origin lists', async () => {
    server.use(http.get('/api/v1/ai/capabilities', () => HttpResponse.json({ detail: 'agent broke' }, { status: 500 })))
    renderSettingsSection(<BuiltIn />, { route })
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the built-in tools: agent broke')
    expect(await screen.findByTestId('browser-origins')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: WEBMCP })).toBeInTheDocument()
  })

  it('the printer camera is a row: switching get_printer_camera off stores off', async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    await card('ScadBuddy tools')
    await user.click(screen.getByRole('button', { name: 'ScadBuddy tools' }))
    await choose(user, select('get_printer_camera'), 'off')
    const dialog = await screen.findByRole('alertdialog', { name: "Lower See a printer's camera?" })
    await user.click(within(dialog).getByRole('button', { name: 'Lower to off' }))
    await waitFor(() => expect(select('get_printer_camera')).toHaveTextContent('off'))
    expect(await itemAccess('builtin:scadbuddy', 'get_printer_camera')).toBe('off')
  })

  it('lowers send_to_printer to write after the confirmation', async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    await card('ScadBuddy tools')
    await user.click(screen.getByRole('button', { name: 'ScadBuddy tools' }))
    await choose(user, select('send_to_printer'), 'write')
    const dialog = await screen.findByRole('alertdialog', { name: 'Lower Send to printer?' })
    expect(await itemAccess('builtin:scadbuddy', 'send_to_printer')).toBe('outward')
    await user.click(within(dialog).getByRole('button', { name: 'Lower to write' }))
    await waitFor(() => expect(select('send_to_printer')).toHaveTextContent('write'))
    expect(await itemAccess('builtin:scadbuddy', 'send_to_printer')).toBe('write')
  })

  it('turns HTTP requests off with the source switch', async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    const toggle = within(await card('HTTP requests')).getByRole('switch', { name: 'Enabled (HTTP requests)' })
    expect(toggle).toBeChecked()
    expect(isUserOnly(toggle)).toBe(false)
    await user.click(toggle)
    await waitFor(async () => expect(within(await card('HTTP requests')).getByRole('switch', { name: 'Enabled (HTTP requests)' })).not.toBeChecked())
    expect((await aiCapabilities.list()).find((s) => s.id === 'builtin:http')?.enabled).toBe(false)
  })

  it('switches the browser tab off; its tools read off', async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    const toggle = within(await card('Your browser tab')).getByRole('switch', { name: 'Enabled (Your browser tab)' })
    expect(toggle).toBeChecked()
    expect(isUserOnly(toggle)).toBe(false)
    await user.click(toggle)
    await waitFor(() => expect(within(screen.getByRole('region', { name: 'Source Your browser tab' })).getByRole('switch')).not.toBeChecked())
    await user.click(screen.getByRole('button', { name: 'Your browser tab' }))
    const browser = await card('Your browser tab')
    // The select keeps the tool's own access; the row says why it is off.
    expect(within(browser).getByRole('combobox', { name: accessName('browser_click') })).toHaveTextContent('write')
    expect(within(browser).getByText('Off while "Your browser tab" is switched off')).toBeInTheDocument()
    const item = (await aiCapabilities.list()).find((s) => s.id === 'builtin:browser')?.items.find((i) => i.id === 'browser_click')
    expect(item).toMatchObject({ access: 'off', own_access: 'write' })
  })

  it("shows ScadBuddy's skills and subagents with their descriptions", async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    const own = await card('ScadBuddy skills and subagents')
    await user.click(screen.getByRole('button', { name: 'ScadBuddy skills and subagents' }))
    for (const name of ['authoring', 'customize', 'print', 'model-author', 'print-analyst']) {
      expect(within(own).getByRole('combobox', { name: accessName(name) })).toHaveTextContent('on')
    }
    // A skill or a subagent is on or off, nothing between (spec §4.1).
    expect(await optionsOf(user, within(own).getByRole('combobox', { name: accessName('authoring') }))).toEqual(['off', 'on (default)'])
    expect(await optionsOf(user, within(own).getByRole('combobox', { name: accessName('model-author') }))).toEqual(['off', 'on (default)'])
    expect(within(own).getByText('scadbuddy:model-author')).toBeInTheDocument()
    expect(within(own).getByText(/^Creates and iterates on ScadBuddy OpenSCAD templates/)).toBeVisible()
    expect(within(own).getByText(/^How to write and edit ScadBuddy OpenSCAD templates/)).toBeVisible()
  })

  it('turns a ScadBuddy subagent off after the confirmation', async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    const own = await card('ScadBuddy skills and subagents')
    await user.click(screen.getByRole('button', { name: 'ScadBuddy skills and subagents' }))
    const subagent = within(own).getByRole('combobox', { name: accessName('model-author') })
    expect(isUserOnly(subagent)).toBe(false)
    await choose(user, subagent, 'off')
    const dialog = await screen.findByRole('alertdialog', { name: 'Lower model-author?' })
    expect(isUserOnly(within(dialog).getByRole('button', { name: 'Lower to off' }))).toBe(false)
    expect(await itemAccess('builtin:own-plugin', 'subagent:model-author')).toBe('on')
    await user.click(within(dialog).getByRole('button', { name: 'Lower to off' }))
    await waitFor(() => expect(within(own).getByRole('combobox', { name: accessName('model-author') })).toHaveTextContent('off'))
    expect(await itemAccess('builtin:own-plugin', 'subagent:model-author')).toBe('off')
    expect(within(own).getByRole('button', { name: 'Reset model-author' })).toBeInTheDocument()
  })

  it("the own plugin's switch is on its card, not user-only", async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    const toggle = within(await card('ScadBuddy skills and subagents')).getByRole('switch', {
      name: 'Enabled (ScadBuddy skills and subagents)',
    })
    expect(toggle).toBeChecked()
    expect(isUserOnly(toggle)).toBe(false)
    await user.click(toggle)
    await waitFor(() =>
      expect(within(screen.getByRole('region', { name: 'Source ScadBuddy skills and subagents' })).getByRole('switch')).not.toBeChecked(),
    )
    const own = (await aiCapabilities.list()).find((s) => s.id === 'builtin:own-plugin')
    expect(own?.enabled).toBe(false)
    expect(own?.items.every((i) => i.access === 'off' && i.own_access === 'on')).toBe(true)
    // One switch: the built-in package the Packages section lists reads it too, as the agent's `*` row.
    expect((await aiPlugins.listPackages()).find((p) => p.name === 'scadbuddy')?.enabled).toBe(false)
  })

  it('keeps the WebMCP switch, this browser only', async () => {
    renderSettingsSection(<BuiltIn />, { route })
    const webmcp = await screen.findByRole('region', { name: WEBMCP })
    expect(within(webmcp).getByText('This browser only')).toBeInTheDocument()
    const toggle = within(webmcp).getByRole('switch', { name: WEBMCP_SWITCH })
    expect(toggle).not.toBeChecked()
    // #254: it admits an outside in-browser agent (AI spec §8.5), so it stays the user's.
    expect(isUserOnly(toggle)).toBe(true)
    expect(toggle).toHaveAccessibleDescription(/AI design spec §8\.5/)
  })

  it('shows only WebMCP while the assistant is unavailable', async () => {
    availability.available = false
    const list = vi.spyOn(aiCapabilities, 'list')
    renderSettingsSection(<BuiltIn />, { route })
    await onlyWebMcp()
    expect(list).not.toHaveBeenCalled()
  })

  it('shows only WebMCP when the capabilities answer 503', async () => {
    server.use(http.get('/api/v1/ai/capabilities', () => HttpResponse.json({ detail: 'no database' }, { status: 503 })))
    const list = vi.spyOn(aiCapabilities, 'list')
    renderSettingsSection(<BuiltIn />, { route })
    await waitFor(() => expect(list).toHaveBeenCalled())
    await expect(list.mock.results[0]!.value).rejects.toMatchObject({ status: 503 })
    await onlyWebMcp()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
