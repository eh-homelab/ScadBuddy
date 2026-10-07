import { screen, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { renderPage } from '../test/utils'
import { AppShell } from './AppShell'

vi.mock('../agent/chat/availability', () => ({
  useAiAvailability: () => ({ available: true, state: 'configured' }),
}))
// A chunk that failed to load: the lazy import rejects, as it does once the stale-chunk
// reload has given up.
vi.mock('./AgentLink', () => {
  throw new Error('Failed to fetch dynamically imported module: AgentLink-abc.js')
})
vi.mock('./assistant/AssistantPanel', () => {
  throw new Error('Failed to fetch dynamically imported module: AssistantPanel-abc.js')
})

describe('the shell’s own lazy chunks (#1002)', () => {
  it('keeps the page when the agent link or the assistant panel fails to load', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const { user } = renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} tabLink={null} />}>
          <Route path="*" element={<p>page</p>} />
        </Route>
      </Routes>,
    )
    await user.click(await screen.findByRole('button', { name: 'Assistant' }))

    const panel = screen.getByRole('complementary', { name: 'Assistant' })
    expect(await within(panel).findByRole('alert')).toHaveTextContent('The assistant failed to load')
    expect(within(panel).getByRole('button', { name: 'Reload page' })).toBeInTheDocument()
    expect(screen.getByText('page')).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument()
  })
})
