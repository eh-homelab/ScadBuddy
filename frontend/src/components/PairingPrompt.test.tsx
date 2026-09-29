import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { isUserOnly } from '../agent/dom'
import { takeSnapshot } from '../agent/snapshot'
import type { TabLinkState } from '../agent/link'
import { PairingPrompt } from './PairingPrompt'

const REQUEST = { id: 'p1', label: 'MCP token “laptop”', expiresAt: '2026-09-29T12:00:00.000Z' }
const state = (over: Partial<TabLinkState> = {}): TabLinkState => ({
  connected: true,
  pending: [],
  paired: [],
  results: {},
  ...over,
})

function setup(over: Partial<TabLinkState>) {
  const handlers = { onAccept: vi.fn(), onDeny: vi.fn(), onEnd: vi.fn() }
  const view = render(<PairingPrompt state={state(over)} {...handlers} />)
  return { ...handlers, ...view }
}

describe('PairingPrompt (#254, AI spec §8.5)', () => {
  it('shows nothing while no agent asks and none is paired', () => {
    const { container } = setup({})
    expect(container).toBeEmptyDOMElement()
  })

  it('asks for the code the agent was given, and allows only with one', async () => {
    const user = userEvent.setup()
    const { onAccept } = setup({ pending: [REQUEST] })
    const form = screen.getByRole('form', { name: 'Pairing request from MCP token “laptop”' })
    expect(form).toHaveTextContent('MCP token “laptop” asks to use this tab')
    const allow = screen.getByRole('button', { name: 'Allow' })
    expect(allow).toBeDisabled()
    await user.type(screen.getByRole('textbox', { name: 'Pairing code' }), ' abcd-efgh ')
    await user.click(allow)
    expect(onAccept).toHaveBeenCalledWith('p1', 'abcd-efgh')
  })

  it('denies, and says why an accept failed', async () => {
    const user = userEvent.setup()
    const { onDeny } = setup({
      pending: [REQUEST],
      results: { p1: { ok: false, message: 'That is not the code. 4 tries left.' } },
    })
    expect(screen.getByRole('alert')).toHaveTextContent('That is not the code. 4 tries left.')
    await user.click(screen.getByRole('button', { name: 'Deny' }))
    expect(onDeny).toHaveBeenCalledWith('p1')
  })

  it('lists a paired agent with a way to disconnect it', async () => {
    const user = userEvent.setup()
    const { onEnd } = setup({ paired: [REQUEST] })
    expect(screen.getByRole('status')).toHaveTextContent('MCP token “laptop” can use this tab until')
    await user.click(screen.getByRole('button', { name: 'Disconnect' }))
    expect(onEnd).toHaveBeenCalledWith('p1')
  })

  it('never hands the code being typed to an agent reading the page (#746)', async () => {
    const user = userEvent.setup()
    setup({ pending: [REQUEST] })
    await user.type(screen.getByRole('textbox', { name: 'Pairing code' }), 'abcd-efgh')
    const snapshot = JSON.stringify(takeSnapshot({ route: '/', page: {}, tools: [] }))
    expect(snapshot).not.toContain('abcd-efgh')
    expect(snapshot).toContain('(typed, hidden)')
  })

  it("is the user's alone: every control in it is user-only, so an agent's click and fill refuse it", () => {
    setup({ pending: [REQUEST], paired: [{ ...REQUEST, id: 'p0' }] })
    for (const control of [...screen.getAllByRole('button'), screen.getByRole('textbox', { name: 'Pairing code' })]) {
      expect(isUserOnly(control), control.textContent ?? '').toBe(true)
    }
  })
})
