import { panelWasOpen, rememberPanelOpen, rememberSession, rememberedSession, sessionToRestore } from './panelMemory'

// #2372 — what a reloaded tab brings back.
describe('the assistant panel’s memory (#2372)', () => {
  const sessions = {
    newest: { status: 'idle', origin: 'chat' },
    mcp: { status: 'running', origin: 'mcp' },
    waiting: { status: 'waiting_approval', origin: 'chat' },
    running: { status: 'running', origin: 'chat' },
  } as const
  const order = ['newest', 'mcp', 'waiting', 'running']

  it('restores the chat the tab showed while it is listed', () => {
    expect(sessionToRestore('newest', order, sessions)).toBe('newest')
  })

  it('else the newest chat still under way, never an MCP client’s session', () => {
    expect(sessionToRestore('gone', order, sessions)).toBe('waiting')
    expect(sessionToRestore(null, ['newest', 'mcp'], sessions)).toBeNull()
  })

  it('remembers per tab, and forgets a closed panel and a new chat', () => {
    rememberPanelOpen(true)
    rememberSession('s1')
    expect([panelWasOpen(), rememberedSession()]).toEqual([true, 's1'])
    rememberPanelOpen(false)
    rememberSession(null)
    expect([panelWasOpen(), rememberedSession()]).toEqual([false, null])
  })
})
