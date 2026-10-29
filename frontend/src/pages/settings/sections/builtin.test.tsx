import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { isUserOnly } from '../../../agent/dom'
import { aiCapabilities } from '../../../api/capabilities'
import { server } from '../../../mocks/server'
import { choose } from '../../../test/select'
import { renderSettingsSection } from '../testing'
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
const select = (name: string) => screen.getByRole('combobox', { name: `Access of ${name}` })

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

  it("shows ScadBuddy's tools, the browser tab with its origin lists, and HTTP requests", async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    const regions = await screen.findAllByRole('region', { name: /^Source / })
    expect(regions.map((r) => r.getAttribute('aria-label'))).toEqual([
      'Source ScadBuddy tools',
      'Source Your browser tab',
      'Source HTTP requests',
    ])
    expect(screen.queryByRole('region', { name: 'Source ScadBuddy skills and subagents' })).toBeNull()
    // The browser tab is not switchable until #4757.
    expect(within(await card('Your browser tab')).queryByRole('switch')).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Your browser tab' }))
    const browser = await card('Your browser tab')
    expect(await within(browser).findByTestId('browser-origins')).toBeInTheDocument()
    expect(within(browser).getByRole('combobox', { name: 'Access of browser_click' })).toBeInTheDocument()
  })

  it('the printer camera is a row: switching get_printer_camera off stores off', async () => {
    const { user } = renderSettingsSection(<BuiltIn />, { route })
    await card('ScadBuddy tools')
    await user.click(screen.getByRole('button', { name: 'ScadBuddy tools' }))
    await choose(user, select('get_printer_camera'), 'off')
    const dialog = await screen.findByRole('alertdialog', { name: 'Lower Printer camera?' })
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
    const toggle = within(await card('HTTP requests')).getByRole('switch', { name: 'Enabled' })
    expect(toggle).toBeChecked()
    expect(isUserOnly(toggle)).toBe(false)
    await user.click(toggle)
    await waitFor(async () => expect(within(await card('HTTP requests')).getByRole('switch', { name: 'Enabled' })).not.toBeChecked())
    expect((await aiCapabilities.list()).find((s) => s.id === 'builtin:http')?.enabled).toBe(false)
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
