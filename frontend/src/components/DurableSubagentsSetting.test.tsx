import { act, screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, api } from '../api/client'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { DurableSubagentsSetting } from './DurableSubagentsSetting'

const URL_LABEL = 'Tools URL'
const PEERS_LABEL = 'Allowed worker addresses'
const IN_CLUSTER = 'http://scadbuddy.bambuddy.svc:8081/internal/durable/subagent-mcp'

describe('DurableSubagentsSetting', () => {
  it('is loopback only until set, and saves a URL and ranges', async () => {
    const { user } = renderPage(<DurableSubagentsSetting />)
    const url = await screen.findByRole('textbox', { name: URL_LABEL })
    expect(url).toHaveValue('')
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    await user.type(url, IN_CLUSTER)
    await user.type(screen.getByRole('textbox', { name: PEERS_LABEL }), '10.42.0.0/16, 10.43.1.2')
    await user.click(save)
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect(await api.getDurableSubagents()).toEqual({ url: IN_CLUSTER, peers: ['10.42.0.0/16', '10.43.1.2'] })
  })

  it('saves empty fields as back to loopback', async () => {
    await api.putDurableSubagents({ url: IN_CLUSTER, peers: ['10.42.0.0/16'] })
    const { user } = renderPage(<DurableSubagentsSetting />)
    await user.clear(await screen.findByRole('textbox', { name: URL_LABEL }))
    await user.clear(screen.getByRole('textbox', { name: PEERS_LABEL }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect(await api.getDurableSubagents()).toEqual({ url: null, peers: [] })
  })

  it('is for the user only: an agent cannot save it', async () => {
    renderPage(<DurableSubagentsSetting />)
    await screen.findByRole('textbox', { name: URL_LABEL })
    expect(screen.getByRole('button', { name: 'Save' }).closest('[data-agent-user-only]')).not.toBeNull()
  })

  it('shows why a save was refused', async () => {
    const { user } = renderPage(<DurableSubagentsSetting />)
    await user.type(await screen.findByRole('textbox', { name: PEERS_LABEL }), 'example.com')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('peers: each entry must be an IP address or CIDR range')
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(
      http.get('/api/v1/ai/settings/durable-subagents', () =>
        HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 }),
      ),
    )
    const read = vi.spyOn(api, 'getDurableSubagents')
    renderPage(<DurableSubagentsSetting />)
    // #2884: it is hidden while loading too, so the absence means something only once
    // the GET has answered 503 and the component has rendered that.
    await waitFor(() => expect(read).toHaveBeenCalled())
    await act(async () => {
      await expect(read.mock.results[0]?.value).rejects.toBeInstanceOf(ApiError)
    })
    expect(screen.queryByText('Durable subagents')).not.toBeInTheDocument()
    read.mockRestore()
  })
})
