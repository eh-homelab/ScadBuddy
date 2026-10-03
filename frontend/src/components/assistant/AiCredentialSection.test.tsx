import { act, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { recheckAiAvailability } from '../../agent/chat/availability'
import { credentialEntry, setCredentials } from '../../mocks/features/aiCredential'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AiCredentialSection } from './AiCredentialSection'

const base = '/api/v1/ai/credentials'
const entries = `${base}/entries`
const KEY = 'Anthropic API key ••••Q7xA'
const GATEWAY = 'Gateway ••••GW99'

/** Resolves once msw has answered the credential list read. */
function listRead(): Promise<void> {
  return new Promise((resolve) => {
    server.events.on('response:mocked', ({ request }) => {
      if (request.method === 'GET' && new URL(request.url).pathname === entries) resolve()
    })
  })
}

/** The JSON bodies of the requests matching method and path, as they are sent. */
function bodiesOf(method: string, path: string): unknown[] {
  const bodies: unknown[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === method && new URL(request.url).pathname === path) {
      void request.clone().json().then((body) => bodies.push(body))
    }
  })
  return bodies
}

const rows = () => screen.getAllByTestId('ai-credential')

afterEach(() => server.events.removeAllListeners())

describe('AiCredentialSection (#1000, #1093)', () => {
  it('lists the credentials in the order they are tried, with their status, never a secret', async () => {
    renderPage(<AiCredentialSection />)
    await screen.findAllByTestId('ai-credential')
    const [first, second] = rows()
    expect(first).toHaveTextContent('1.Anthropic API key')
    expect(within(first!).getByLabelText('ending in Q7xA')).toBeInTheDocument()
    expect(first).toHaveTextContent('Active')
    expect(second).toHaveTextContent('2.Gateway')
    expect(second).toHaveTextContent('at https://gateway.example/anthropic')
    expect(second).toHaveTextContent(/Rate limited until/)
    expect(second).toHaveTextContent('rate_limit_error: 429')
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue('')
    expect(screen.getByLabelText('Anthropic API key')).toHaveAttribute('type', 'password')
  })

  it('moves a credential up, sending the whole order', async () => {
    const orders = bodiesOf('PUT', `${base}/order`)
    const { user } = renderPage(<AiCredentialSection />)
    expect(await screen.findByRole('button', { name: `Move ${KEY} up` })).toBeDisabled()
    expect(screen.getByRole('button', { name: `Move ${GATEWAY} down` })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: `Move ${GATEWAY} up` }))
    await waitFor(() => expect(rows()[0]).toHaveTextContent('1.Gateway'))
    expect(orders).toEqual([{ ids: ['c2', 'default'] }])
  })

  it('shows a stale order refused and reads the list again', async () => {
    server.use(
      http.put(`${base}/order`, () => HttpResponse.json({ detail: 'the list changed, read it again' }, { status: 409 }), {
        once: true,
      }),
    )
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: `Move ${GATEWAY} up` }))
    expect(await screen.findByRole('alert')).toHaveTextContent('the list changed, read it again')
    expect(rows()[0]).toHaveTextContent('1.Anthropic API key')
  })

  it('resets a rate-limited or disabled credential, and only those', async () => {
    const { user } = renderPage(<AiCredentialSection />)
    await screen.findAllByTestId('ai-credential')
    expect(screen.queryByRole('button', { name: `Reset ${KEY}` })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: `Reset ${GATEWAY}` }))
    expect(await screen.findByRole('status')).toHaveTextContent(`${GATEWAY} is active again.`)
    await waitFor(() => expect(rows()[1]).toHaveTextContent('Active'))
    expect(rows()[1]).not.toHaveTextContent('rate_limit_error')
  })

  it('shows a disabled credential with why, and says when none is usable', async () => {
    setCredentials([
      credentialEntry({
        id: 'default',
        last4: 'Q7xA',
        status: 'disabled',
        last_error: 'authentication_error: invalid x-api-key',
      }),
    ])
    renderPage(<AiCredentialSection />)
    const row = (await screen.findAllByTestId('ai-credential'))[0]!
    expect(row).toHaveTextContent('Disabled')
    expect(row).toHaveTextContent('authentication_error: invalid x-api-key')
    expect(screen.getByTestId('ai-credentials-none-usable')).toHaveTextContent('each one needs a reset or a new key')
  })

  it('says when the first rate-limited one is usable again', async () => {
    setCredentials([
      credentialEntry({ id: 'default', last4: 'Q7xA', status: 'cooling_down', cooldown_until: '2099-01-01T12:30:00Z' }),
    ])
    renderPage(<AiCredentialSection />)
    expect(await screen.findByTestId('ai-credentials-none-usable')).toHaveTextContent(
      /The first rate-limited one is usable again at/,
    )
  })

  it('replaces one credential’s key, keeping its kind and base URL', async () => {
    const saves = bodiesOf('PUT', `${entries}/c2`)
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: `Replace the key of ${GATEWAY}` }))
    const field = screen.getByLabelText('New gateway token')
    expect(field).toHaveAttribute('type', 'password')
    await user.type(field, 'gw-token-new-NEW1')
    await user.click(screen.getByRole('button', { name: 'Save key' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved a new key for credential 2 (Gateway ••••NEW1).')
    await waitFor(() => expect(within(rows()[1]!).getByLabelText('ending in NEW1')).toBeInTheDocument())
    expect(rows()[1]).toHaveTextContent('Active')
    expect(screen.queryByLabelText('New gateway token')).not.toBeInTheDocument()
    expect(saves).toEqual([{ kind: 'gateway', base_url: 'https://gateway.example/anthropic', secret: 'gw-token-new-NEW1' }])
  })

  it('keeps the replace form open when the agent refuses the key', async () => {
    server.use(
      http.put(`${entries}/default`, () =>
        HttpResponse.json({ detail: 'secret must not contain whitespace' }, { status: 400 }),
      ),
    )
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: `Replace the key of ${KEY}` }))
    await user.type(screen.getByLabelText('New API key'), 'sk-ant-new')
    await user.click(screen.getByRole('button', { name: 'Save key' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('secret must not contain whitespace')
    expect(screen.getByLabelText('New API key')).toBeInTheDocument()
  })

  it('adds a gateway last, sending base URL and token once', async () => {
    const creates = bodiesOf('POST', entries)
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('radio', { name: 'Gateway (base URL and token)' }))
    const add = screen.getByRole('button', { name: 'Add' })
    await user.type(screen.getByLabelText('Gateway token'), 'gw-token-ZZ12')
    expect(add).toBeDisabled()
    await user.type(screen.getByLabelText('Base URL'), 'https://other.example/anthropic/')
    await user.click(add)

    expect(await screen.findByRole('status')).toHaveTextContent('Added last')
    await waitFor(() => expect(rows()).toHaveLength(3))
    expect(rows()[2]).toHaveTextContent('3.Gateway')
    expect(rows()[2]).toHaveTextContent('at https://other.example/anthropic')
    expect(screen.getByLabelText('Gateway token')).toHaveValue('')
    expect(creates).toEqual([{ kind: 'gateway', base_url: 'https://other.example/anthropic/', secret: 'gw-token-ZZ12' }])
  })

  it('saves the first credential when there is none, and shows a short key without a last four', async () => {
    setCredentials([])
    const { user } = renderPage(<AiCredentialSection />)
    expect(await screen.findByText(/No credential saved/)).toBeInTheDocument()
    await user.type(screen.getByLabelText('Anthropic API key'), 'sk-short')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved. Use Test to check it works.')
    const row = (await screen.findAllByTestId('ai-credential'))[0]!
    expect(row).toHaveTextContent('Anthropic API key')
    expect(within(row).queryByText(/••••/)).not.toBeInTheDocument()
  })

  it('tests one credential, and shows the wait when rate-limited', async () => {
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: `Test ${KEY}` }))
    expect(await within(rows()[0]!).findByTestId('ai-credential-test')).toHaveTextContent('Works (claude-sonnet-5-5)')

    await user.click(screen.getByRole('button', { name: `Test ${GATEWAY}` }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/try again shortly \(wait \d+ s\)\./)
  })

  it('shows a failed test with its reason, on that credential', async () => {
    server.use(
      http.post(`${entries}/c2/test`, () =>
        HttpResponse.json({ ok: false, detail: 'authentication_error', duration_ms: 500, model: null }),
      ),
    )
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: `Test ${GATEWAY}` }))
    expect(await within(rows()[1]!).findByTestId('ai-credential-test')).toHaveTextContent('Failed: authentication_error')
  })

  it('says a test is already running', async () => {
    // The first test stays in flight until the assertion is done, whatever the load.
    let finish: () => void = () => {}
    const held = new Promise<void>((resolve) => {
      finish = resolve
    })
    let running = false
    server.use(
      http.post(`${entries}/:id/test`, async () => {
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
    const test = await screen.findByRole('button', { name: `Test ${KEY}` })
    const first = fetch(`${entries}/c2/test`, { method: 'POST' })
    await waitFor(() => expect(running).toBe(true))
    await user.click(test)
    expect(await screen.findByRole('alert')).toHaveTextContent('a connection test is already running (wait 10 s).')
    finish()
    await first
  })

  it('deletes one credential only after a confirmation, and the next moves up', async () => {
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: `Delete ${KEY}` }))
    const dialog = await screen.findByRole('dialog', { name: `Delete ${KEY}?` })
    // The gateway behind it is rate limited, so nothing else is usable now.
    expect(dialog).toHaveTextContent('No other credential is usable now, so the assistant stops working')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(rows()).toHaveLength(2)

    await user.click(screen.getByRole('button', { name: `Delete ${KEY}` }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Delete credential' }))
    expect(await screen.findByText(`Deleted ${KEY}.`)).toBeInTheDocument()
    expect(screen.getByTestId('ai-credentials-none-usable')).toBeInTheDocument()
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(rows()[0]).toHaveTextContent('1.Gateway')
  })

  it.each([
    ['one behind the one in use', 'c3', `The assistant keeps using ${KEY}.`],
    ['the one in use, with a usable one behind it', 'default', 'The assistant falls back to Anthropic API key ••••BBBB.'],
  ])('says what deleting %s does', async (_, id, message) => {
    setCredentials([
      credentialEntry({ id: 'default', last4: 'Q7xA' }),
      credentialEntry({ id: 'c3', last4: 'BBBB' }),
    ])
    const { user } = renderPage(<AiCredentialSection />)
    const name = id === 'default' ? KEY : 'Anthropic API key ••••BBBB'
    await user.click(await screen.findByRole('button', { name: `Delete ${name}` }))
    expect(await screen.findByRole('dialog')).toHaveTextContent(message)
  })

  it('forgets a failed test once the key is replaced', async () => {
    server.use(
      http.post(`${entries}/default/test`, () =>
        HttpResponse.json({ ok: false, detail: 'authentication_error', duration_ms: 500, model: null }),
      ),
    )
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: `Test ${KEY}` }))
    expect(await within(rows()[0]!).findByTestId('ai-credential-test')).toHaveTextContent('Failed')
    await user.click(screen.getByRole('button', { name: `Replace the key of ${KEY}` }))
    await user.type(screen.getByLabelText('New API key'), 'sk-ant-replaced-1234')
    await user.click(screen.getByRole('button', { name: 'Save key' }))
    expect(await screen.findByText(/Saved a new key for credential 1/)).toBeInTheDocument()
    expect(within(rows()[0]!).queryByTestId('ai-credential-test')).not.toBeInTheDocument()
  })

  it('shows a credential deleted elsewhere refused, and drops it on the next read', async () => {
    const { user } = renderPage(<AiCredentialSection />)
    await screen.findAllByTestId('ai-credential')
    // Another tab deletes the gateway.
    setCredentials([credentialEntry({ id: 'default', last4: 'Q7xA' })])
    await user.click(screen.getByRole('button', { name: `Reset ${GATEWAY}` }))
    expect(await screen.findByRole('alert')).toHaveTextContent('no such credential')
    await waitFor(() => expect(rows()).toHaveLength(1))
  })

  it('reads the list again when a cooldown ends, so the status is not left stale', async () => {
    setCredentials([
      credentialEntry({
        id: 'default',
        last4: 'Q7xA',
        status: 'cooling_down',
        cooldown_until: new Date(Date.now() + 1000).toISOString(),
      }),
    ])
    renderPage(<AiCredentialSection />)
    expect(await screen.findByTestId('ai-credentials-none-usable')).toBeInTheDocument()
    expect(rows()[0]).toHaveTextContent(/Rate limited until/)
    await waitFor(() => expect(rows()[0]).toHaveTextContent('Active'), { timeout: 5000 })
    expect(screen.queryByTestId('ai-credentials-none-usable')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: `Reset ${KEY}` })).not.toBeInTheDocument()
  })

  it('backs off when the agent still reports a cooldown this clock says has ended', async () => {
    let reads = 0
    const past = new Date(Date.now() - 60_000).toISOString()
    server.use(
      http.get(entries, () => {
        reads += 1
        return HttpResponse.json({
          credentials: [
            credentialEntry({ id: 'default', last4: 'Q7xA', status: 'cooling_down', cooldown_until: past }),
          ],
          usable_now: false,
          recovers_at: past,
          can_save: true,
          cannot_save_reason: null,
        })
      }),
    )
    renderPage(<AiCredentialSection />)
    await screen.findAllByTestId('ai-credential')
    // 1 s, then 2 s: three reads in the first 3.5 s, not one every half second.
    await new Promise((resolve) => setTimeout(resolve, 3500))
    expect(reads).toBeGreaterThanOrEqual(2)
    expect(reads).toBeLessThanOrEqual(3)
  }, 10_000)

  it('shows the date of a cooldown that ends on another day', async () => {
    setCredentials([
      credentialEntry({ id: 'default', last4: 'Q7xA', status: 'cooling_down', cooldown_until: '2099-01-02T12:30:00Z' }),
    ])
    renderPage(<AiCredentialSection />)
    expect((await screen.findAllByTestId('ai-credential'))[0]).toHaveTextContent(/Rate limited until .*2099/)
    expect(screen.getByTestId('ai-credentials-none-usable')).toHaveTextContent(/2099/)
  })

  it('keeps the row actions off while a credential is being added', async () => {
    let finish: () => void = () => {}
    const held = new Promise<void>((resolve) => {
      finish = resolve
    })
    server.use(
      http.post(entries, async () => {
        await held
        return HttpResponse.json(credentialEntry({ id: 'c9', last4: 'NEW9' }), { status: 201 })
      }),
    )
    const { user } = renderPage(<AiCredentialSection />)
    await user.type(await screen.findByLabelText('Anthropic API key'), 'sk-ant-adding-NEW9')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(screen.getByRole('button', { name: `Move ${GATEWAY} up` })).toBeDisabled())
    expect(screen.getByRole('button', { name: `Test ${KEY}` })).toBeDisabled()
    expect(screen.getByRole('button', { name: `Delete ${KEY}` })).toBeDisabled()
    finish()
    expect(await screen.findByText(/Added last/)).toBeInTheDocument()
  })

  it('keeps the delete dialog open with the error when the delete fails', async () => {
    server.use(
      http.delete(`${entries}/default`, () =>
        HttpResponse.json({ detail: 'the AI database is unreachable' }, { status: 503 }),
      ),
    )
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: `Delete ${KEY}` }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Delete credential' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('the AI database is unreachable')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(rows()).toHaveLength(2)
  })

  it('clears what was typed when the kind changes, so a secret is never sent as another kind', async () => {
    const creates = bodiesOf('POST', entries)
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('radio', { name: 'Gateway (base URL and token)' }))
    await user.type(screen.getByLabelText('Base URL'), 'https://other.example/anthropic')
    await user.type(screen.getByLabelText('Gateway token'), 'gw-token-SECRET')
    await user.click(screen.getByRole('radio', { name: 'Anthropic API' }))
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
    await user.click(screen.getByRole('radio', { name: 'Gateway (base URL and token)' }))
    expect(screen.getByLabelText('Base URL')).toHaveValue('')
    expect(screen.getByLabelText('Gateway token')).toHaveValue('')
    expect(creates).toEqual([])
  })

  it('re-reads the list when Test finds the credential deleted elsewhere', async () => {
    const { user } = renderPage(<AiCredentialSection />)
    await screen.findAllByTestId('ai-credential')
    setCredentials([credentialEntry({ id: 'default', last4: 'Q7xA' })])
    await user.click(screen.getByRole('button', { name: `Test ${GATEWAY}` }))
    expect(await screen.findByRole('alert')).toHaveTextContent('no such credential')
    await waitFor(() => expect(rows()).toHaveLength(1))
  })

  it('closes the delete dialog when the credential was already deleted elsewhere', async () => {
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: `Delete ${GATEWAY}` }))
    const dialog = await screen.findByRole('dialog')
    setCredentials([credentialEntry({ id: 'default', last4: 'Q7xA' })])
    await user.click(within(dialog).getByRole('button', { name: 'Delete credential' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByRole('alert')).toHaveTextContent('no such credential')
    await waitFor(() => expect(rows()).toHaveLength(1))
  })

  it('keeps the delete dialog open on Escape while the delete runs', async () => {
    let finish: () => void = () => {}
    const held = new Promise<void>((resolve) => {
      finish = resolve
    })
    server.use(
      http.delete(`${entries}/default`, async () => {
        await held
        return HttpResponse.json({ detail: 'the AI database is unreachable' }, { status: 503 })
      }),
    )
    const { user } = renderPage(<AiCredentialSection />)
    await user.click(await screen.findByRole('button', { name: `Delete ${KEY}` }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Delete credential' }))
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    finish()
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('the AI database is unreachable')
  })

  it('offers no Reset on a key the agent cannot decrypt', async () => {
    setCredentials([credentialEntry({ id: 'default', last4: 'Q7xA', usable: false, status: 'disabled' })])
    renderPage(<AiCredentialSection />)
    expect(await screen.findByText(/cannot decrypt this key/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: `Reset ${KEY}` })).not.toBeInTheDocument()
  })

  it('tries again when the read after a cooldown fails', async () => {
    const until = new Date(Date.now() + 500).toISOString()
    setCredentials([credentialEntry({ id: 'default', last4: 'Q7xA', status: 'cooling_down', cooldown_until: until })])
    let reads = 0
    server.use(
      http.get(entries, () => {
        reads += 1
        // The first timed read (the second read in all) fails, as if the agent were restarting.
        return reads === 2 ? HttpResponse.json({ detail: 'Bad Gateway' }, { status: 502 }) : undefined
      }),
    )
    renderPage(<AiCredentialSection />)
    expect((await screen.findAllByTestId('ai-credential'))[0]).toHaveTextContent(/Rate limited until/)
    await waitFor(() => expect(rows()[0]).toHaveTextContent('Active'), { timeout: 6000 })
    expect(reads).toBeGreaterThanOrEqual(3)
  }, 10_000)

  it('says why it cannot save, and disables Add and Replace', async () => {
    setCredentials([credentialEntry({ id: 'default', last4: 'Q7xA' })], false)
    const { user } = renderPage(<AiCredentialSection />)
    expect(await screen.findByText(/Saving is not possible: no key-encryption key/)).toBeInTheDocument()
    await user.type(screen.getByLabelText('Anthropic API key'), 'sk-ant-abcd')
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
    expect(screen.getByRole('button', { name: `Replace the key of ${KEY}` })).toBeDisabled()
  })

  it('warns about a key the agent cannot decrypt', async () => {
    setCredentials([credentialEntry({ id: 'default', last4: 'Q7xA', usable: false })])
    renderPage(<AiCredentialSection />)
    expect(await screen.findByText(/cannot decrypt this key/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: `Test ${KEY}` })).toBeDisabled()
  })

  it('is for the user only, the dialog’s confirm button too', async () => {
    const { user } = renderPage(<AiCredentialSection />)
    const add = await screen.findByRole('button', { name: 'Add' })
    expect(add.closest('[data-agent-user-only]')).not.toBeNull()
    await user.click(screen.getByRole('button', { name: `Delete ${KEY}` }))
    const confirm = within(await screen.findByRole('dialog')).getByRole('button', { name: 'Delete credential' })
    expect(confirm.closest('[data-agent-user-only]')).not.toBeNull()
  })

  it('shows a loading row until the read answers', async () => {
    renderPage(<AiCredentialSection />)
    expect(screen.getByRole('heading', { name: 'Claude credentials' })).toBeInTheDocument()
    expect(screen.getByText('Loading')).toBeInTheDocument()
    expect(await screen.findAllByTestId('ai-credential')).toHaveLength(2)
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
    server.use(http.get(entries, answer))
    const answered = listRead()
    renderPage(<AiCredentialSection />)
    await answered
    await waitFor(() => expect(screen.queryByText('Loading')).not.toBeInTheDocument())
    expect(screen.queryByRole('heading', { name: 'Claude credentials' })).not.toBeInTheDocument()
  })

  it('shows a malformed JSON answer with a Retry, rather than hiding', async () => {
    server.use(
      http.get(
        entries,
        () => new HttpResponse('{"credentials": [', { status: 200, headers: { 'Content-Type': 'application/json' } }),
        { once: true },
      ),
    )
    const { user } = renderPage(<AiCredentialSection />)
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findAllByTestId('ai-credential')).toHaveLength(2)
  })

  it('shows a 503 without the no_database code, even with the same text', async () => {
    server.use(
      http.get(entries, () =>
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
    server.use(
      http.get(
        entries,
        () =>
          HttpResponse.json(
            { detail: 'the AI database is unreachable or its migrations have not applied; see /healthz' },
            { status: 503 },
          ),
        { once: true },
      ),
    )
    const { user } = renderPage(<AiCredentialSection />)
    expect(await screen.findByRole('alert')).toHaveTextContent('migrations have not applied')
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findAllByTestId('ai-credential')).toHaveLength(2)
  })

  it('reads again when the agent changes state after a failed read', async () => {
    server.use(
      http.get(entries, () => HttpResponse.json({ detail: 'the AI database is unreachable' }, { status: 503 }), {
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
    expect(await screen.findAllByTestId('ai-credential')).toHaveLength(2)
  })
})
