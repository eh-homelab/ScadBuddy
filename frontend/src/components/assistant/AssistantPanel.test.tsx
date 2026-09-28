import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useRef, type ReactNode } from 'react'
import { Route, Routes } from 'react-router'
import { bridge } from '../../agent/bridge'
import type { ClientMessage } from '../../agent/chat/protocol'
import { useFullscreen } from '../../lib/useFullscreen'
import { EXTERNAL_SESSION_ID, createMockAgentTransport, type MockAgentTransport } from '../../mocks/agent'
import { renderPage } from '../../test/utils'
import { AppShell } from '../AppShell'

const availability = vi.hoisted(() => ({ available: true }))
vi.mock('../../agent/chat/availability', () => ({
  useAiAvailability: () => availability,
}))

let agent: MockAgentTransport
const factory = () => {
  agent = createMockAgentTransport({ stepMs: 0 })
  return agent
}

function renderShell(route = '/m/name-keychain', page: ReactNode = <p>page</p>) {
  return renderPage(
    <Routes>
      <Route element={<AppShell embedded={false} assistantTransport={factory} />}>
        <Route path="*" element={page} />
      </Route>
    </Routes>,
    { route },
  )
}

/** A page with a full-screen view, as the customizer has; jsdom takes the fallback. */
function FullscreenPage() {
  const view = useRef<HTMLDivElement>(null)
  const { mode, toggle } = useFullscreen(view)
  return (
    <div ref={view}>
      <button type="button" onClick={toggle}>
        {mode ? 'Exit full screen' : 'Full screen'}
      </button>
    </div>
  )
}

const pressShortcut = () => fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })

const sentOf = <T extends ClientMessage['type']>(type: T) =>
  agent.sent.filter((m): m is Extract<ClientMessage, { type: T }> => m.type === type)

async function openAndSend(text = 'Make the name bigger and send it') {
  const view = renderShell()
  await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
  const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
  await view.user.type(box, `${text}{Enter}`)
  await screen.findByRole('region', { name: 'Needs your approval' })
  return view
}

afterEach(() => {
  availability.available = true
})

describe('assistant panel', () => {
  it('is hidden, shortcut included, when AI is off', async () => {
    availability.available = false
    renderShell()
    expect(screen.queryByRole('button', { name: 'Assistant' })).not.toBeInTheDocument()
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    await act(async () => {})
    expect(screen.queryByRole('complementary', { name: 'Assistant' })).not.toBeInTheDocument()
  })

  it('opens with Ctrl+` and focuses the composer; Escape closes it and returns focus', async () => {
    const { user } = renderShell()
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
    await waitFor(() => expect(box).toHaveFocus())
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('complementary', { name: 'Assistant' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Assistant' })).toHaveFocus()
  })

  it('comes out of full screen for Ctrl+` rather than opening out of sight', async () => {
    const { user } = renderShell('/m/name-keychain', <FullscreenPage />)
    await user.click(screen.getByRole('button', { name: 'Full screen' }))

    pressShortcut()
    expect(await screen.findByRole('button', { name: 'Full screen' })).toBeInTheDocument()
    const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
    await waitFor(() => expect(box).toHaveFocus())
  })

  it('brings back a panel full screen had hidden, rather than closing it unseen', async () => {
    const { user } = renderShell('/m/name-keychain', <FullscreenPage />)
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await screen.findByRole('textbox', { name: 'Message the assistant' })
    await user.click(screen.getByRole('button', { name: 'Full screen' }))

    pressShortcut()
    expect(await screen.findByRole('button', { name: 'Full screen' })).toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: 'Assistant' })).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toHaveFocus(),
    )
  })

  it('offers prompts for the page and sends the page context with the turn', async () => {
    const { user } = renderShell()
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.click(await screen.findByRole('button', { name: 'Make it fit the A1 mini plate' }))
    await screen.findByText('Make it fit the A1 mini plate', { selector: 'p' })
    expect(sentOf('user.message')[0]).toMatchObject({
      text: 'Make it fit the A1 mini plate',
      context: { route: '/m/name-keychain', modelSlug: 'name-keychain' },
    })
    expect(sentOf('user.message')[0]).not.toHaveProperty('sessionId')
    // #254: the browser bridge's view rides along; the shell's tools are live everywhere.
    expect(sentOf('user.message')[0]?.context.tools).toEqual(expect.arrayContaining(['navigate', 'snapshot']))
    expect(sentOf('user.message')[0]?.context.dialogs).toEqual([])
  })

  it('does not let the bridge fallbacks speak for the user (#254)', async () => {
    const { user } = renderShell()
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await screen.findByRole('textbox', { name: 'Message the assistant' })
    const typed = await bridge.call('fill', { label: 'Message the assistant', value: 'send it' })
    expect(!typed.ok && typed.error.code).toBe('refused')
    const prompt = await bridge.call('click', { role: 'button', name: 'Make it fit the A1 mini plate' })
    expect(!prompt.ok && prompt.error.code).toBe('refused')
    expect(sentOf('user.message')).toEqual([])
  })

  it('streams text, shows the tool call with its risk, sources and version', async () => {
    await openAndSend()
    const log = screen.getByRole('log', { name: 'Conversation' })
    expect(within(log).getByText(/make the/)).toBeInTheDocument()
    expect(within(log).getByText('name', { selector: 'strong' })).toBeInTheDocument()
    const [write, send] = screen.getAllByTestId('agent-tool')
    expect(within(write!).getByText('set_parameters')).toBeInTheDocument()
    expect(within(write!).getByText('write')).toBeInTheDocument()
    expect(within(write!).getByText('Why? 2 sources')).toBeInTheDocument()
    expect(within(write!).getByRole('link', { name: 'OpenSCAD customizer parameters' })).toHaveAttribute(
      'href',
      'https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Customizer',
    )
    expect(within(write!).getByRole('link', { name: /Undo from version a1b2c3d/ })).toHaveAttribute(
      'href',
      '/m/name-keychain/versions',
    )
    expect(within(send!).getByText('outward')).toBeInTheDocument()
    expect(within(send!).getByText('running…')).toBeInTheDocument()
  })

  it('holds the outward step until Approve, then sends the decision', async () => {
    const { user } = await openAndSend()
    const card = screen.getByRole('region', { name: 'Needs your approval' })
    expect(card).toHaveTextContent('Send name-keychain to Bambuddy project "Keychains", 2 copies?')
    const approve = within(card).getByRole('button', { name: 'Approve' })
    expect(approve).toHaveAttribute('data-agent-user-only')
    expect(within(card).getByRole('button', { name: 'Deny' })).toHaveAttribute('data-agent-user-only')

    // Nothing proceeds without the decision.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30))
    })
    expect(sentOf('approval.decision')).toEqual([])
    expect(screen.queryByText('Queued 2 copies in the Keychains project.')).not.toBeInTheDocument()
    expect(screen.getByTestId('agent-status')).toHaveTextContent('Waiting for approval')

    await user.click(approve)
    expect(sentOf('approval.decision')).toEqual([
      { v: 1, type: 'approval.decision', sessionId: 'chat-1', id: expect.any(String), approve: true },
    ])
    await screen.findByText('Queued 2 copies in the Keychains project.')
    await screen.findByText('Sent. Two copies are in the queue.')
    expect(within(card).getByText('Approved by You.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('agent-status')).toHaveTextContent('Idle'))
  })

  it('Deny sends a refusal and nothing is sent', async () => {
    const { user } = await openAndSend()
    await user.click(screen.getByRole('button', { name: 'Deny' }))
    expect(sentOf('approval.decision')).toMatchObject([{ approve: false }])
    await screen.findByText('Denied: nothing was sent.')
    expect(screen.queryByText('Queued 2 copies in the Keychains project.')).not.toBeInTheDocument()
  })

  it('Stop interrupts the turn', async () => {
    const { user } = await openAndSend()
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    expect(sentOf('session.interrupt')).toEqual([{ v: 1, type: 'session.interrupt', sessionId: 'chat-1' }])
    await waitFor(() => expect(screen.getByTestId('agent-status')).toHaveTextContent('Idle'))
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument()
  })

  it('lists sessions with origin and owner, and takes over one an agent controls', async () => {
    const { user } = renderShell()
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    const picker = screen.getByRole('navigation', { name: 'Sessions' })
    expect(within(picker).getByText('MCP')).toBeInTheDocument()
    expect(within(picker).getByText('Claude Desktop')).toBeInTheDocument()
    await user.click(within(picker).getByRole('button', { name: /Tune the gridfinity bin/ }))
    expect(sentOf('session.attach')).toEqual([{ v: 1, type: 'session.attach', sessionId: EXTERNAL_SESSION_ID }])

    // Replayed transcript; the composer is locked while someone else holds it.
    await screen.findByText('Done: the bin is now 3 units (21 mm) tall.')
    expect(screen.getByText('Controlled by Claude Desktop')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toBeDisabled()

    const takeOver = screen.getByRole('button', { name: 'Take over' })
    expect(takeOver).toHaveAttribute('data-agent-user-only')
    await user.click(takeOver)
    expect(sentOf('session.handoff')).toEqual([{ v: 1, type: 'session.handoff', sessionId: EXTERNAL_SESSION_ID }])
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message the assistant' })).toBeEnabled())
    expect(screen.queryByText('Controlled by Claude Desktop')).not.toBeInTheDocument()
  })

  it('reports a malformed frame instead of rendering it', async () => {
    const bad = () => ({
      connect: ({ onFrame }: { onFrame: (f: unknown) => void }) => {
        queueMicrotask(() => onFrame({ v: 1, type: 'approval.required', sessionId: 's', id: 'a', tool: 't', summary: 'x', risk: 'write' }))
      },
      send: () => {},
      close: () => {},
    })
    const { user } = renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} assistantTransport={bad} />}>
          <Route path="*" element={<p>page</p>} />
        </Route>
      </Routes>,
    )
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    expect(await screen.findByText(/Ignored a malformed message/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
  })
})
