import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useRef, type ReactNode } from 'react'
import { Route, Routes } from 'react-router'
import { bridge } from '../../agent/bridge'
import type { ClientMessage } from '../../agent/chat/protocol'
import { useFullscreen } from '../../lib/useFullscreen'
import { EXTERNAL_SESSION_ID, createMockAgentTransport, type MockAgentTransport } from '../../mocks/agent'
import { setPendingApprovals } from '../../mocks/features/pendingInput'
import { setSessionResources } from '../../mocks/features/assistantSessions'
import { renderPage } from '../../test/utils'
import { AppShell } from '../AppShell'

/** What `useAiAvailability` answers; `set` re-renders whoever reads it, as the real one does. */
const availability = vi.hoisted(() => {
  type Value = { available: boolean; state?: string; chat?: 'refused' }
  let value: Value = { available: true }
  const listeners = new Set<() => void>()
  return {
    get: () => value,
    set: (next: Value) => {
      value = next
      for (const listener of listeners) listener()
    },
    subscribe: (listener: () => void) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
})
vi.mock('../../agent/chat/availability', async () => {
  const { useSyncExternalStore } = await import('react')
  return { useAiAvailability: () => useSyncExternalStore(availability.subscribe, availability.get) }
})

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
  availability.set({ available: true })
})

describe('assistant panel', () => {
  it('is hidden, shortcut included, when AI is off', async () => {
    availability.set({ available: false, state: 'not_configured' })
    renderShell()
    expect(screen.queryByRole('button', { name: 'Assistant' })).not.toBeInTheDocument()
    fireEvent.keyDown(window, { key: '`', code: 'Backquote', ctrlKey: true })
    await act(async () => {})
    expect(screen.queryByRole('complementary', { name: 'Assistant' })).not.toBeInTheDocument()
  })

  it('keeps an open panel through an outage, and drops it only when the agent says AI is off', async () => {
    const { user } = renderShell()
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    const box = await screen.findByRole('textbox', { name: 'Message the assistant' })
    await user.type(box, 'Make the name bigger{Enter}')
    const transcript = (await screen.findAllByText('Make the name bigger')).length

    // The agent is restarting: its status read fails, and the panel stays, transcript and all.
    act(() => availability.set({ available: false, state: 'unreachable' }))
    expect(screen.getByRole('complementary', { name: 'Assistant' })).toBeInTheDocument()
    expect(screen.getAllByText('Make the name bigger')).toHaveLength(transcript)
    expect(screen.getByRole('button', { name: 'Assistant' })).toBeInTheDocument()
    act(() => availability.set({ available: false, state: 'unavailable' }))
    expect(screen.getByRole('complementary', { name: 'Assistant' })).toBeInTheDocument()

    // Its chat gate refuses this address: off for this page.
    act(() => availability.set({ available: false, state: 'unavailable', chat: 'refused' }))
    expect(screen.queryByRole('complementary', { name: 'Assistant' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Assistant' })).not.toBeInTheDocument()

    // Back, then switched off (no credential): the same.
    act(() => availability.set({ available: true, state: 'configured' }))
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    expect(await screen.findByRole('complementary', { name: 'Assistant' })).toBeInTheDocument()
    act(() => availability.set({ available: false, state: 'not_configured' }))
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

  it('shows tool arguments only in Advanced, and remembers the switch per browser', async () => {
    window.localStorage.removeItem('scadbuddy.assistant.advanced')
    const { user } = await openAndSend()
    expect(screen.queryAllByTestId('agent-tool-arguments')).toEqual([])
    const advanced = screen.getByRole('button', { name: 'Advanced' })
    expect(advanced).toHaveAttribute('aria-pressed', 'false')
    await user.click(advanced)
    expect(advanced).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getAllByTestId('agent-tool-arguments').length).toBeGreaterThan(0)
    expect(window.localStorage.getItem('scadbuddy.assistant.advanced')).toBe('1')
    window.localStorage.removeItem('scadbuddy.assistant.advanced')
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

  it('shows waiting approvals on the header button with the panel closed, and in the tab title (#815)', async () => {
    setPendingApprovals(2)
    document.title = 'ScadBuddy'
    const { user } = renderShell()
    const button = await screen.findByRole('button', { name: 'Assistant, 2 waiting for you' })
    expect(button).toHaveAttribute('title', 'Assistant (Ctrl+`): 2 waiting for you (2 approvals)')
    expect(within(button).getByTestId('assistant-attention')).toHaveTextContent('2')
    // Announced, not only shown: the badge appears while focus is elsewhere.
    const live = screen.getByTestId('assistant-attention-live')
    expect(live).toHaveAttribute('aria-live', 'polite')
    expect(live).toHaveTextContent('2 waiting for you')
    await waitFor(() => expect(document.title).toBe('(2) ScadBuddy'))

    // Decided elsewhere: toggling the panel reads again, and the badge goes.
    setPendingApprovals(0)
    await user.click(button)
    const plain = await screen.findByRole('button', { name: 'Assistant' })
    expect(within(plain).queryByTestId('assistant-attention')).not.toBeInTheDocument()
    expect(screen.getByTestId('assistant-attention-live')).toBeEmptyDOMElement()
    expect(plain).toHaveAttribute('title', 'Assistant (Ctrl+`)')
    await waitFor(() => expect(document.title).toBe('ScadBuddy'))
  })

  it('shows the badge embedded in Bambuddy, but leaves the frame\'s unseen title alone (#815)', async () => {
    setPendingApprovals(1)
    document.title = 'ScadBuddy'
    renderPage(
      <Routes>
        <Route element={<AppShell embedded assistantTransport={factory} />}>
          <Route path="*" element={<p>page</p>} />
        </Route>
      </Routes>,
      { route: '/' },
    )
    const button = await screen.findByRole('button', { name: 'Assistant, 1 waiting for you' })
    expect(within(button).getByTestId('assistant-attention')).toHaveTextContent('1')
    expect(document.title).toBe('ScadBuddy')
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

  it("shows what a session touched, linking to each resource's page (#931)", async () => {
    const { user } = renderShell('/')
    await user.click(screen.getByRole('button', { name: 'Assistant' }))
    await user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    await user.click(screen.getByRole('button', { name: /Tune the gridfinity bin/ }))
    await screen.findByText('Done: the bin is now 3 units (21 mm) tall.')

    const touched = screen.getByRole('button', { name: 'Touched' })
    expect(touched).toHaveAttribute('aria-expanded', 'false')
    await user.click(touched)
    expect(touched).toHaveAttribute('aria-expanded', 'true')
    const panel = screen.getByRole('region', { name: 'What this session touched' })
    const revisions = await within(panel).findByRole('group', { name: 'Revisions' })
    expect(within(revisions).getByRole('link', { name: /3f9c2a1/ })).toHaveAttribute(
      'href',
      '/m/gridfinity-bin?version=3f9c2a1b7d4e',
    )

    await user.click(touched)
    expect(screen.queryByRole('region', { name: 'What this session touched' })).not.toBeInTheDocument()
  })

  it('reads Touched again when a tool call finishes, without waiting for the turn (#931)', async () => {
    let inject: (frame: unknown) => void = () => {}
    const spying = () => {
      const inner = factory()
      return {
        ...inner,
        connect: (h: Parameters<typeof inner.connect>[0]) => {
          inject = (frame) => act(() => h.onFrame(frame as never))
          inner.connect(h)
        },
      }
    }
    const view = renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} assistantTransport={spying} />}>
          <Route path="*" element={<p>page</p>} />
        </Route>
      </Routes>,
      { route: '/' },
    )
    await view.user.click(screen.getByRole('button', { name: 'Assistant' }))
    await view.user.click(await screen.findByRole('button', { name: 'Sessions (1)' }))
    await view.user.click(screen.getByRole('button', { name: /Tune the gridfinity bin/ }))
    await screen.findByText('Done: the bin is now 3 units (21 mm) tall.')
    await view.user.click(screen.getByRole('button', { name: 'Touched' }))
    const panel = screen.getByRole('region', { name: 'What this session touched' })
    await within(panel).findByRole('group', { name: 'Presets' })

    setSessionResources(EXTERNAL_SESSION_ID, [
      { type: 'output', id: 'out-mid', action: 'created', model: 'gridfinity-bin', before: null, after: null, tool: 'save_output', at: '2026-10-03T09:01:00.000Z' },
    ])
    const status = screen.getByTestId('agent-status').textContent
    const base = { v: 1, sessionId: EXTERNAL_SESSION_ID }
    inject({ ...base, type: 'tool.call', id: 'tool-ext-2', name: 'mcp__scadbuddy__save_output', input: { slug: 'gridfinity-bin' }, risk: 'write' })
    inject({ ...base, type: 'tool.result', id: 'tool-ext-2', ok: true, summary: 'Saved.' })

    expect(await within(panel).findByRole('link', { name: /out-mid/ })).toHaveAttribute('href', '/edit/out-mid')
    // Read on the tool result alone: the session's status never moved.
    expect(screen.getByTestId('agent-status').textContent).toBe(status)
  })

  it('reports a malformed frame instead of rendering it', async () => {
    const bad = () => ({
      connect: ({ onFrame }: { onFrame: (f: unknown) => void }) => {
        queueMicrotask(() => onFrame({ v: 1, type: 'approval.required', sessionId: 's', id: 'a', tool: 't', summary: 'x', risk: 'write' }))
      },
      send: () => 'sent' as const,
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
