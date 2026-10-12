import { screen, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { flowRequests } from '../mocks/features/flows'
import { renderPage } from '../test/utils'
import { FlowPage } from './FlowPage'

// #2350 — one flow version over the msw flows (src/mocks/features/flows.ts): its
// versions, its script read only, and Start run.

function render(id: string) {
  return renderPage(
    <Routes>
      <Route path="/workflows/flows/:id" element={<FlowPage />} />
      <Route path="/workflows/runs/:id" element={<p>Run page</p>} />
    </Routes>,
    { route: `/workflows/flows/${id}` },
  )
}

describe('FlowPage', () => {
  it('shows the script read only, with Python highlighted', async () => {
    render('def-swap')
    expect(await screen.findByRole('heading', { name: 'swap v1' })).toBeInTheDocument()
    const script = await screen.findByLabelText('Script')
    expect(script.tagName).toBe('PRE')
    expect(script).toHaveTextContent('await wait_for_human("Swap to pink?", 600)')
    expect([...script.querySelectorAll('[data-token="comment"]')].map((t) => t.textContent)).toEqual([
      '# Render the box, then ask before swapping the colour.',
    ])
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.getByText(/Registered by an assistant \(session sess-1\)/)).toBeInTheDocument()
  })

  it("lists the name's versions, newest first, from listFlowVersions", async () => {
    const versions = vi.spyOn(api, 'listFlowVersions')
    render('def-plates-1')
    const list = await screen.findByRole('region', { name: 'Versions' })
    await within(list).findByRole('link', { name: 'v2' })
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent?.split(' ')[0])).toEqual(['v2', 'v1'])
    expect(within(list).getByRole('link', { name: 'v2' })).toHaveAttribute('href', '/workflows/flows/def-plates-2')
    expect(within(list).getByText('v1')).toHaveAttribute('aria-current', 'page')
    expect(versions).toHaveBeenCalledWith('plates')
  })

  it('starts a run with a key and goes to it', async () => {
    const { user } = render('def-swap')
    await user.click(await screen.findByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('Run page')).toBeInTheDocument()
    expect(flowRequests()).toEqual([
      { path: '/api/v1/workflows/def-swap/runs', key: expect.stringMatching(/^[0-9a-f]{32}$/), body: {} },
    ])
  })

  it('says when there is no such flow', async () => {
    render('def-gone')
    expect(await screen.findByRole('alert')).toHaveTextContent('No such flow.')
  })
})
