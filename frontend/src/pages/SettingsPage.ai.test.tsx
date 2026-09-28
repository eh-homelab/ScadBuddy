import { screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { renderPage } from '../test/utils'
import { SettingsPage } from './SettingsPage'

// #251 — "MCP access tokens" talks to the agent service, so Settings shows it only
// where the AI features are available (`useAiAvailability()`), as the assistant does.

const availability = vi.hoisted(() => ({ available: false }))
vi.mock('../agent/chat/availability', () => ({
  useAiAvailability: () => availability,
}))

async function seeded() {
  await waitFor(() =>
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue(
      'https://bambuddy.internal.nullreference.io',
    ),
  )
}

describe('SettingsPage and the AI availability gate', () => {
  afterEach(() => {
    availability.available = false
    vi.restoreAllMocks()
  })

  it('hides MCP access tokens, and asks the agent service nothing, when AI is unavailable', async () => {
    const list = vi.spyOn(api, 'listMcpTokens')
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.queryByRole('region', { name: 'MCP access tokens' })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(list).not.toHaveBeenCalled()
  })

  it('shows MCP access tokens when AI is available', async () => {
    availability.available = true
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.getByRole('region', { name: 'MCP access tokens' })).toBeInTheDocument()
    expect(await screen.findByRole('list', { name: 'Tokens' })).toBeInTheDocument()
  })
})
