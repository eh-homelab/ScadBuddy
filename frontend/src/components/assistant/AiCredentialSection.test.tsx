import { act, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { recheckAiAvailability } from '../../agent/chat/availability'
import { api } from '../../api/client'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AiCredentialSection } from './AiCredentialSection'

const base = '/api/v1/ai/credentials'

/** Resolves once msw has answered the credential read. */
function credentialRead(): Promise<void> {
  return new Promise((resolve) => {
    server.events.on('response:mocked', ({ request }) => {
      if (request.method === 'GET' && new URL(request.url).pathname === base) resolve()
    })
  })
}

afterEach(() => server.events.removeAllListeners())

describe('AiCredentialSection (#1000)', () => {
  it('shows the stored kind and last four, never the secret', async () => {
    renderPage(<AiCredentialSection />)
    const current = await screen.findByTestId('ai-credential-current')
    expect(current).toHaveTextContent('Anthropic API key')
    expect(within(current).getByLabelText('ending in Q7xA')).toBeInTheDocument()
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue('')
    expect(screen.getByLabelText('Anthropic API key')).toHaveAttribute('type', 'password')
  })

  it('replaces the key with a gateway, sending base URL and token once', async () => {
    const puts: unknown[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'PUT' && new URL(request.url).pathname === base) {
        void request.clone().json().then((body) => puts.push(body))
      }
    })
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('radio', { name: 'Gateway (base URL and token)' }))
    const save = screen.getByRole('button', { name: 'Save' })
    await user.type(screen.getByLabelText('Gateway token'), 'gw-token-ZZ12')
    expect(save).toBeDisabled()
    await user.type(screen.getByLabelText('Base URL'), 'https://gateway.example/anthropic/')
    await user.click(save)

    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    const current = screen.getByTestId('ai-credential-current')
    expect(current).toHaveTextContent('Gateway at https://gateway.example/anthropic')
    expect(within(current).getByLabelText('ending in ZZ12')).toBeInTheDocument()
    expect(screen.getByLabelText('Gateway token')).toHaveValue('')
    expect(puts).toEqual([
      { kind: 'gateway', base_url: 'https://gateway.example/anthropic/', secret: 'gw-token-ZZ12' },
    ])
  })

  it('tests the credential, and shows the wait when rate-limited', async () => {
    const { user } = renderPage(<AiCredentialSection />)
    const test = await screen.findByRole('button', { name: 'Test' })
    await user.click(test)
    expect(await screen.findByTestId('ai-credential-test')).toHaveTextContent('Works (claude-sonnet-5-5)')

    await user.click(test)
    expect(await screen.findByRole('alert')).toHaveTextContent(/try again shortly \(wait (9|10) s\)\./)
  })

  it('shows a failed test with its reason', async () => {
    server.use(
      http.post(`${base}/test`, () =>
        HttpResponse.json({ ok: false, detail: 'authentication_error', duration_ms: 500, model: null }),
      ),
    )
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: 'Test' }))
    expect(await screen.findByTestId('ai-credential-test')).toHaveTextContent('Failed: authentication_error')
  })

  it('deletes only after a confirmation', async () => {
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.getByTestId('ai-credential-current')).toHaveTextContent('Anthropic API key')

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Delete credential' }))
    expect(await screen.findByText(/No credential saved/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Test' })).not.toBeInTheDocument()
  })

  it('says why it cannot save, and disables Save', async () => {
    server.use(
      http.get(base, () =>
        HttpResponse.json({
          configured: false,
          kind: null,
          base_url: null,
          last4: null,
          updated_at: null,
          usable: false,
          can_save: false,
          cannot_save_reason: 'no key-encryption key: SCADBUDDY_SECRET_KEY_FILE is not set',
        }),
      ),
    )
    const { user } = renderPage(<AiCredentialSection />)
    expect(await screen.findByText(/Saving is not possible: no key-encryption key/)).toBeInTheDocument()
    await user.type(screen.getByLabelText('Anthropic API key'), 'sk-ant-abcd')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('shows the agent refusing a save', async () => {
    server.use(http.put(base, () => HttpResponse.json({ detail: 'secret must not contain whitespace' }, { status: 400 })))
    const { user } = renderPage(<AiCredentialSection />)
    await user.type(await screen.findByLabelText('Anthropic API key'), 'sk-ant-abcd')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('secret must not contain whitespace')
  })

  it('is for the user only', async () => {
    renderPage(<AiCredentialSection />)
    const save = await screen.findByRole('button', { name: 'Save' })
    expect(save.closest('[data-agent-user-only]')).not.toBeNull()
  })

  it('shows a loading row until the read answers', async () => {
    renderPage(<AiCredentialSection />)
    expect(screen.getByRole('heading', { name: 'Claude credential' })).toBeInTheDocument()
    expect(screen.getByText('Loading')).toBeInTheDocument()
    expect(await screen.findByTestId('ai-credential-current')).toBeInTheDocument()
  })

  it.each([
    ['no /api/v1/ai route', 404, 'Not Found'],
    ['no agent database', 503, 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'],
  ])('is hidden when the agent is not deployed (%s)', async (_, status, message) => {
    server.use(http.get(base, () => HttpResponse.json({ detail: message }, { status })))
    const answered = credentialRead()
    renderPage(<AiCredentialSection />)
    await answered
    await waitFor(() => expect(screen.queryByText('Loading')).not.toBeInTheDocument())
    expect(screen.queryByRole('heading', { name: 'Claude credential' })).not.toBeInTheDocument()
  })

  it('shows any other failed read with a Retry', async () => {
    let calls = 0
    server.use(
      http.get(base, () => {
        calls += 1
        return calls === 1
          ? HttpResponse.json(
              { detail: 'the AI database is unreachable or its migrations have not applied; see /healthz' },
              { status: 503 },
            )
          : undefined
      }),
    )
    const { user } = renderPage(<AiCredentialSection />)
    expect(await screen.findByRole('alert')).toHaveTextContent('migrations have not applied')
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByTestId('ai-credential-current')).toHaveTextContent('Anthropic API key')
  })

  it('reads again when the agent changes state after a failed read', async () => {
    server.use(
      http.get(base, () => HttpResponse.json({ detail: 'the AI database is unreachable' }, { status: 503 }), {
        once: true,
      }),
    )
    renderPage(<AiCredentialSection />)
    expect(await screen.findByRole('alert')).toHaveTextContent('the AI database is unreachable')
    server.use(
      http.get('/api/v1/ai/status', () =>
        HttpResponse.json({ available: false, state: 'disabled', ai: 'disabled', reason: 'no credential' }),
      ),
    )
    await act(() => recheckAiAvailability({ force: true }))
    expect(await screen.findByTestId('ai-credential-current')).toHaveTextContent('Anthropic API key')
  })

  it('says a test is already running', async () => {
    const { user } = renderPage(<AiCredentialSection />)
    const test = await screen.findByRole('button', { name: 'Test' })
    const first = api.testAiCredential()
    await user.click(test)
    expect(await screen.findByRole('alert')).toHaveTextContent('a connection test is already running (wait 10 s).')
    await first
  })
})
