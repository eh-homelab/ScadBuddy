import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import type { McpToken } from '../../../api/mcpTokens'
import { resetMockState } from '../../../mocks/handlers'
import { region, renderSettingsSection } from '../testing'
import { Component as FlowAnswerers } from './flow-answerers'

const ACTIVE = '70ce0000-0000-4000-8000-00000000000a'
const REVOKED = '70ce0000-0000-4000-8000-00000000000b'

function token(id: string, name: string, fields: Partial<McpToken> = {}): McpToken {
  return {
    id,
    name,
    tier: 'write',
    created_at: '2026-11-25T00:00:00Z',
    expires_at: null,
    last_used_at: null,
    revoked_at: null,
    status: 'active',
    approval_grant: false,
    ...fields,
  }
}

const save = () => within(region("Flows' questions")).getByRole('button', { name: "Save Flows' questions" })

// #7471 — who besides a person may answer a flow's question.
describe("Flows' questions", () => {
  beforeEach(() => {
    resetMockState()
    vi.spyOn(api, 'listMcpTokens').mockResolvedValue({
      auth_mode: 'bearer',
      tokens: [
        token(ACTIVE, 'bench'),
        token(REVOKED, 'old laptop', { status: 'revoked', revoked_at: '2026-11-24T00:00:00Z' }),
      ],
    })
  })
  afterEach(() => vi.restoreAllMocks())

  it('starts as people only, with the two choices and the active tokens by name', async () => {
    renderSettingsSection(<FlowAnswerers />, { route: '/settings/assistant' })
    expect(await screen.findByText('People only: nothing is chosen.')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'The session that started the run' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Any of your assistant sessions' })).not.toBeChecked()
    expect(await screen.findByRole('checkbox', { name: 'MCP token bench' })).not.toBeChecked()
    expect(screen.queryByRole('checkbox', { name: /old laptop/ })).toBeNull()
  })

  it('saves the list', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<FlowAnswerers />, { route: '/settings/assistant' })
    await user.click(await screen.findByRole('checkbox', { name: 'MCP token bench' }))
    await user.click(screen.getByRole('checkbox', { name: 'Any of your assistant sessions' }))
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toMatchObject({ flow_answerers: ['sessions', `token:${ACTIVE}`] })
    expect((await api.getSettings()).flow_answerers).toEqual(['sessions', `token:${ACTIVE}`])
  })

  it('shows a revoked token that the list still holds, so it can be removed', async () => {
    await api.putSettings({ flow_answerers: [`token:${REVOKED}`] })
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<FlowAnswerers />, { route: '/settings/assistant' })
    const revoked = await screen.findByRole('checkbox', { name: 'MCP token old laptop (revoked)' })
    expect(revoked).toBeChecked()
    await user.click(revoked)
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toMatchObject({ flow_answerers: [] })
  })
})
