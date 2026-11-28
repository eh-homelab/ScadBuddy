import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import type { ServiceCredentialView } from '../../api/types'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { ServiceCredentials } from './ServiceCredentials'

// #4975 phase 4 — Settings → System → Service credentials, against src/mocks/features/serviceCredentials.ts.

describe('ServiceCredentials', () => {
  it('lists each service’s credentials, never a secret', async () => {
    renderPage(<ServiceCredentials />)
    const agent = await screen.findByRole('list', { name: 'Agent credentials' })
    expect(within(agent).getByText(/last used/)).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Print worker credentials' })).getByText(/never used/)).toBeInTheDocument()
  })

  it('lists and revokes an exchanged credential, with no rotation (#5298)', async () => {
    const user = userEvent.setup()
    renderPage(<ServiceCredentials />)
    const section = await screen.findByRole('region', { name: 'Projects worker' })
    expect(within(section).queryByRole('button', { name: 'Rotate now' })).not.toBeInTheDocument()
    const list = within(section).getByRole('list', { name: 'Projects worker credentials' })
    await user.click(await within(list).findByRole('button', { name: 'Revoke' }))
    expect(await within(list).findByText('revoked')).toBeInTheDocument()
    expect(within(list).queryByRole('button', { name: 'Revoke' })).not.toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Render worker' })).getByText('None issued.')).toBeInTheDocument()
  })

  it('rotates and revokes', async () => {
    const user = userEvent.setup()
    renderPage(<ServiceCredentials />)
    const printWorker = await screen.findByRole('region', { name: 'Print worker' })
    expect(within(printWorker).queryByRole('button', { name: 'Rotate now' })).not.toBeInTheDocument()
    const section = screen.getByRole('region', { name: 'Agent' })
    await user.click(within(section).getByRole('button', { name: 'Rotate now' }))
    const list = screen.getByRole('list', { name: 'Agent credentials' })
    expect(await within(list).findAllByRole('button', { name: 'Revoke' })).toHaveLength(2)
    await user.click(within(list).getAllByRole('button', { name: 'Revoke' })[0]!)
    expect(await within(list).findByText('revoked')).toBeInTheDocument()
  })

  it('folds a service\u2019s expired and revoked credentials behind a toggle, and says when none is live (#8538)', async () => {
    const row = (id: string, caller: 'print' | 'projects', ended: 'expired' | 'revoked' | null): ServiceCredentialView => ({
      id,
      caller,
      scopes: [`crud.${caller}`],
      created_at: '2026-11-01T12:00:00Z',
      expires_at: ended === 'expired' ? '2026-11-01T12:15:00Z' : '2026-12-01T12:00:00Z',
      revoked_at: ended === 'revoked' ? '2026-11-01T12:05:00Z' : null,
      last_used_at: null,
      proof: 'k8s',
      pod_name: `scadbuddy-${caller}-0`,
      pod_uid: null,
      machine: null,
      live: ended === null,
    })
    server.use(
      http.get('/api/v1/auth/service-credentials', () =>
        HttpResponse.json([
          row('00000000000000a1', 'print', null),
          row('00000000000000a2', 'print', 'expired'),
          row('00000000000000a3', 'print', 'revoked'),
          row('00000000000000b1', 'projects', 'expired'),
        ]),
      ),
    )
    /** A row's id line: an ended row's id shares it with "expired" or "revoked". */
    const idLine = (id: string) => (_: string, element: Element | null) =>
      element?.tagName === 'P' && element.textContent?.startsWith(id) === true
    const user = userEvent.setup()
    renderPage(<ServiceCredentials />)
    const print = await screen.findByRole('region', { name: 'Print worker' })
    const list = within(print).getByRole('list', { name: 'Print worker credentials' })
    expect(await within(list).findByText(idLine('00000000000000a1'))).toBeInTheDocument()
    expect(within(list).queryByText(idLine('00000000000000a2'))).toBeNull()
    expect(within(list).queryByText(idLine('00000000000000a3'))).toBeNull()

    const toggle = within(print).getByRole('button', { name: 'Show 2 expired or revoked' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await user.click(toggle)
    for (const id of ['00000000000000a1', '00000000000000a2', '00000000000000a3']) {
      expect(within(list).getByText(idLine(id))).toBeInTheDocument()
    }
    expect(within(print).getByRole('button', { name: 'Hide 2 expired or revoked' })).toHaveAttribute('aria-expanded', 'true')

    const projects = screen.getByRole('region', { name: 'Projects worker' })
    expect(within(projects).getByText('None live.')).toBeInTheDocument()
    expect(within(projects).queryByText(idLine('00000000000000b1'))).toBeNull()
    expect(within(projects).getByRole('button', { name: 'Show 1 expired or revoked' })).toBeInTheDocument()
  })
})
