import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { loginObject } from '../../api/fleetLogin'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AiCredentialSection } from './AiCredentialSection'

const inner = {
  accessToken: 'sk-ant-oat01-access-fleet-WXYZ',
  refreshToken: 'sk-ant-ort01-refresh-fleet',
  expiresAt: 1795000000000,
  subscriptionType: 'max',
}

/** The JSON bodies of the fleet login posts, as they are sent. */
function posts(): unknown[] {
  const bodies: unknown[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'POST' && new URL(request.url).pathname === '/api/v1/fleet/logins') {
      void request.clone().json().then((body) => bodies.push(body))
    }
  })
  return bodies
}

afterEach(() => server.events.removeAllListeners())

describe('loginObject', () => {
  it('takes the whole file or the object in it, and nothing else', () => {
    expect(loginObject(JSON.stringify({ claudeAiOauth: inner }))).toBe(JSON.stringify(inner))
    expect(loginObject(JSON.stringify(inner))).toBe(JSON.stringify(inner))
    expect(loginObject('not json')).toBeNull()
    expect(loginObject('[1]')).toBeNull()
    expect(loginObject('{"claudeAiOauth": "x"}')).toBeNull()
  })
})

describe('FleetLoginForm (#7984)', () => {
  it('adds a pasted login for the fleet, and the list shows it as the fleet’s', async () => {
    const sent = posts()
    const { user } = renderPage(<AiCredentialSection />)
    await screen.findAllByTestId('ai-credential')
    await user.click(screen.getByRole('button', { name: 'Add a Claude login for the fleet' }))
    await user.type(screen.getByLabelText('Name (optional)', { selector: '#fleet-login-name' }), 'spark')
    await user.click(screen.getByLabelText('Login file'))
    await user.paste(JSON.stringify({ claudeAiOauth: inner }))
    await user.click(screen.getByRole('button', { name: 'Add login' }))
    expect(await screen.findByText(/Added spark for the fleet/)).toBeInTheDocument()
    expect(sent).toEqual([{ kind: 'claude_login', secret: JSON.stringify(inner), name: 'spark' }])
    expect(screen.getByLabelText('Login file')).toHaveValue('')
    await waitFor(() => expect(screen.getAllByTestId('ai-credential')).toHaveLength(3))
    const row = screen.getAllByTestId('ai-credential')[2]!
    expect(within(row).getByTestId('ai-credential-fleet-note')).toBeInTheDocument()
  })

  it('says what to paste when it is not a login, and sends nothing', async () => {
    const sent = posts()
    const { user } = renderPage(<AiCredentialSection />)
    await screen.findAllByTestId('ai-credential')
    await user.click(screen.getByRole('button', { name: 'Add a Claude login for the fleet' }))
    await user.type(screen.getByLabelText('Login file'), 'sk-ant-oat01-token')
    await user.click(screen.getByRole('button', { name: 'Add login' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Paste the whole .credentials.json file')
    expect(sent).toEqual([])
  })

  it('shows the server’s refusal', async () => {
    server.use(
      http.post('/api/v1/fleet/logins', () =>
        HttpResponse.json(
          { title: 'Forbidden', status: 403, detail: 'Only a person signed in themselves may do this.' },
          { status: 403, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderPage(<AiCredentialSection />)
    await screen.findAllByTestId('ai-credential')
    await user.click(screen.getByRole('button', { name: 'Add a Claude login for the fleet' }))
    await user.click(screen.getByLabelText('Login file'))
    await user.paste(JSON.stringify(inner))
    await user.click(screen.getByRole('button', { name: 'Add login' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Only a person signed in themselves may do this.')
  })
})
