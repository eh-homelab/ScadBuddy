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
    expect(await screen.findByRole('alert')).toHaveTextContent(/try again shortly \(wait \d+ s\)\./)
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
    expect(screen.getByText('Deleted. The assistant is off until a credential is saved.')).toBeInTheDocument()
  })

  it('says the next credential is in use when one moves up (#1093)', async () => {
    server.use(
      http.delete(base, () =>
        HttpResponse.json({
          configured: true,
          kind: 'gateway',
          base_url: 'https://llm.example',
          last4: 'Zz99',
          updated_at: '2026-10-01T09:00:00Z',
          usable: true,
          can_save: true,
          cannot_save_reason: null,
        }),
      ),
    )
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Delete credential' }))
    expect(await screen.findByText('Deleted. The next credential is in use now.')).toBeInTheDocument()
    expect(screen.getByTestId('ai-credential-current')).toHaveTextContent('Zz99')
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
    ['nothing routes /api/v1/ai: the SPA fallback answers', () =>
      new HttpResponse('<!doctype html><html></html>', { status: 200, headers: { 'Content-Type': 'text/html' } })],
    ['a proxy answers 404', () => HttpResponse.json({ detail: 'Not Found' }, { status: 404 })],
    ['no agent database', () =>
      HttpResponse.json(
        { detail: 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)', code: 'no_database' },
        { status: 503 },
      )],
  ])('is hidden when the agent is not deployed (%s)', async (_, answer) => {
    server.use(http.get(base, answer))
    const answered = credentialRead()
    renderPage(<AiCredentialSection />)
    await answered
    await waitFor(() => expect(screen.queryByText('Loading')).not.toBeInTheDocument())
    expect(screen.queryByRole('heading', { name: 'Claude credential' })).not.toBeInTheDocument()
  })

  it('shows a malformed JSON answer with a Retry, rather than hiding', async () => {
    server.use(
      http.get(
        base,
        () => new HttpResponse('{"configured": tr', { status: 200, headers: { 'Content-Type': 'application/json' } }),
        { once: true },
      ),
    )
    const { user } = renderPage(<AiCredentialSection />)
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByTestId('ai-credential-current')).toHaveTextContent('Anthropic API key')
  })

  it('shows a saved key too short to have a last four', async () => {
    const { user } = renderPage(<AiCredentialSection />)
    await user.type(await screen.findByLabelText('Anthropic API key'), 'sk-short')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    const current = screen.getByTestId('ai-credential-current')
    expect(current).toHaveTextContent('Anthropic API key')
    expect(within(current).queryByText(/••••/)).not.toBeInTheDocument()
  })

  it('shows a 503 without the no_database code, even with the same text', async () => {
    server.use(
      http.get(base, () =>
        HttpResponse.json(
          { detail: 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)' },
          { status: 503 },
        ),
      ),
    )
    renderPage(<AiCredentialSection />)
    expect(await screen.findByRole('alert')).toHaveTextContent('AI features need the database')
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
    // The first test stays in flight until the assertion is done, whatever the load.
    let finish: () => void = () => {}
    const held = new Promise<void>((resolve) => {
      finish = resolve
    })
    let running = false
    server.use(
      http.post(`${base}/test`, async () => {
        if (running) {
          return HttpResponse.json(
            { detail: 'a connection test is already running' },
            { status: 429, headers: { 'Retry-After': '10' } },
          )
        }
        running = true
        await held
        return HttpResponse.json({ ok: true, detail: 'ok', duration_ms: 1, model: null })
      }),
    )
    const { user } = renderPage(<AiCredentialSection />)
    const test = await screen.findByRole('button', { name: 'Test' })
    const first = api.testAiCredential()
    await waitFor(() => expect(running).toBe(true))
    await user.click(test)
    expect(await screen.findByRole('alert')).toHaveTextContent('a connection test is already running (wait 10 s).')
    finish()
    await first
  })
})
