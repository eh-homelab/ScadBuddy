import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { BrowserOriginsSetting } from './BrowserOriginsSetting'

describe('BrowserOriginsSetting (#982)', () => {
  it('shows the lists, one rule per line, and saves an edit', async () => {
    const { user } = renderPage(<BrowserOriginsSetting />)
    const allow = await screen.findByRole('textbox', { name: 'Opens without asking' })
    expect((allow as HTMLTextAreaElement).value.split('\n')).toContain('makerworld.com')
    expect((screen.getByRole('textbox', { name: 'Never opens' }) as HTMLTextAreaElement).value.split('\n')).toContain('169.254.0.0/16')
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    await user.type(allow, '\n *.cults3d.com ')
    await user.click(save)
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect((await api.getBrowserOrigins()).allow.at(-1)).toBe('*.cults3d.com')

    await user.click(screen.getByRole('button', { name: 'Restore defaults' }))
    await waitFor(async () => expect((await api.getBrowserOrigins()).allow).not.toContain('*.cults3d.com'))
  })

  it('shows why a save was refused, and keeps the edit', async () => {
    server.use(
      http.put('/api/v1/ai/settings/browser-origins', () =>
        HttpResponse.json({ detail: 'deny list: "nope nope" is not a host, an origin or an address range' }, { status: 400 }),
      ),
    )
    const { user } = renderPage(<BrowserOriginsSetting />)
    const deny = await screen.findByRole('textbox', { name: 'Never opens' })
    await user.type(deny, '\nnope nope')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('"nope nope" is not a host')
    expect((deny as HTMLTextAreaElement).value).toContain('nope nope')
  })

  it('lists the sites approved in chats, and revokes one', async () => {
    const { user } = renderPage(<BrowserOriginsSetting />)
    const list = await screen.findByRole('list', { name: 'Sites approved in chats' })
    expect(within(list).getByText('https://cults3d.com')).toBeInTheDocument()
    await user.click(within(list).getByRole('button', { name: /Revoke https:\/\/cults3d.com/ }))
    await waitFor(() => expect(screen.getByText(/None yet/)).toBeInTheDocument())
    expect((await api.getBrowserOrigins()).approved).toEqual([])
  })

  it('is for the user only: an agent can change nothing', async () => {
    renderPage(<BrowserOriginsSetting />)
    await screen.findByRole('textbox', { name: 'Opens without asking' })
    for (const el of [
      ...screen.getAllByRole('textbox'),
      screen.getByRole('button', { name: 'Save' }),
      screen.getByRole('button', { name: 'Restore defaults' }),
      ...screen.getAllByRole('button', { name: /Revoke/ }),
    ]) {
      expect(el.closest('[data-agent-user-only]')).not.toBeNull()
    }
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(http.get('/api/v1/ai/settings/browser-origins', () => HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 })))
    renderPage(<BrowserOriginsSetting />)
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('AI headless browser')).not.toBeInTheDocument()
  })
})
