import { screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMockAgentTransport, type MockAgentTransport } from '../../mocks/agent'
import type { ClientMessage } from '../../agent/chat/protocol'
import { HttpResponse, http } from 'msw'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AssistantChat } from './AssistantChat'
import { MODE_KEY } from '../../agent/chat/modePreference'

let agent: MockAgentTransport
const factory = () => {
  agent = createMockAgentTransport({ stepMs: 0 })
  return agent
}

const userMessages = () =>
  agent.sent.filter((m): m is Extract<ClientMessage, { type: 'user.message' }> => m.type === 'user.message')

function renderChat() {
  return renderPage(<AssistantChat factory={factory} onClose={() => {}} focusKey={0} />)
}

async function openPicker(user: ReturnType<typeof renderChat>['user']) {
  await user.click(screen.getByText('Advanced', { selector: 'summary' }))
  return screen.getByRole('combobox', { name: 'Session mode' })
}

async function send(user: ReturnType<typeof renderChat>['user'], text = 'Make the name bigger') {
  await user.type(await screen.findByRole('textbox', { name: 'Message the assistant' }), `${text}{Enter}`)
}

beforeEach(() => window.localStorage.removeItem(MODE_KEY))
afterEach(() => vi.restoreAllMocks())

describe('ModePicker in a new chat (#1056)', () => {
  it('offers Classic and Durable under Advanced, with what Durable means', async () => {
    const { user } = renderChat()
    const select = await openPicker(user)
    expect(screen.getByRole('option', { name: 'Classic' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Durable' })).toBeInTheDocument()
    await user.selectOptions(select, 'durable')
    expect(
      screen.getByText('Survives restarts; approvals wait as long as needed. Plugins are not available.'),
    ).toBeInTheDocument()
  })

  it('takes the server default when nothing is stored, and sends no mode of its own', async () => {
    const { user } = renderChat()
    const select = await openPicker(user)
    // No selection of its own: the option names the server's default, and nothing is sent.
    expect(await screen.findByRole('option', { name: 'Default (Durable)' })).toBeInTheDocument()
    expect(select).toHaveValue('')
    await send(user)
    await waitFor(() => expect(userMessages()).toHaveLength(1))
    expect(userMessages()[0]).not.toHaveProperty('mode')
  })

  it('remembers a choice and the next message carries it', async () => {
    const { user } = renderChat()
    const select = await openPicker(user)
    await user.selectOptions(select, 'classic')
    await user.selectOptions(select, 'durable')
    expect(window.localStorage.getItem(MODE_KEY)).toBe('durable')
    await send(user)
    await waitFor(() => expect(userMessages()).toHaveLength(1))
    expect(userMessages()[0]).toMatchObject({ mode: 'durable' })
  })

  it('starts from the stored choice over the server default', async () => {
    window.localStorage.setItem(MODE_KEY, 'classic')
    const { user } = renderChat()
    expect(await openPicker(user)).toHaveValue('classic')
  })

  it('still renders and sends when storage throws', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const { user } = renderChat()
    const select = await openPicker(user)
    await screen.findByRole('option', { name: 'Default (Durable)' })
    await user.selectOptions(select, 'classic')
    await send(user)
    await waitFor(() => expect(userMessages()).toHaveLength(1))
    expect(userMessages()[0]).toMatchObject({ mode: 'classic' })
  })

  it('is gone after the first message, and later messages never carry a mode', async () => {
    const { user } = renderChat()
    const select = await openPicker(user)
    await user.selectOptions(select, 'durable')
    await send(user)
    await screen.findByRole('region', { name: 'Needs your approval' })
    expect(screen.queryByRole('combobox', { name: 'Session mode' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(screen.getByTestId('agent-status')).toHaveTextContent('Idle'))
    await send(user, 'And again')
    await waitFor(() => expect(userMessages()).toHaveLength(2))
    expect(userMessages()[1]).toHaveProperty('sessionId')
    expect(userMessages()[1]).not.toHaveProperty('mode')
  })
})

it('does not pretend a default when the server default cannot be read', async () => {
  server.use(http.get('/api/v1/ai/settings/session-mode', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
  const { user } = renderChat()
  const select = await openPicker(user)
  expect(await screen.findByRole('option', { name: 'Server default' })).toBeInTheDocument()
  expect(select).toHaveValue('')
  expect(screen.getByText('Uses the default set in Settings.')).toBeInTheDocument()
})

describe('a durable session in the panel (#1056)', () => {
  it('shows a Durable badge and Stop sends session.interrupt', async () => {
    const { user } = renderChat()
    const select = await openPicker(user)
    await user.selectOptions(select, 'durable')
    await send(user)
    await screen.findByRole('region', { name: 'Needs your approval' })
    expect(screen.getByText('Durable')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    expect(agent.sent.filter((m) => m.type === 'session.interrupt')).toEqual([
      { v: 1, type: 'session.interrupt', sessionId: 'chat-1' },
    ])
  })

  it('does not badge a classic session', async () => {
    const { user } = renderChat()
    const select = await openPicker(user)
    await user.selectOptions(select, 'classic')
    await send(user)
    await screen.findByRole('region', { name: 'Needs your approval' })
    expect(screen.queryByText('Durable')).not.toBeInTheDocument()
  })
})
