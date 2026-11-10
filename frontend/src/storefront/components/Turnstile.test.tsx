import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { RequestForm } from './RequestForm'
import { TURNSTILE_SCRIPT, resetTurnstileForTests } from '../turnstile'

function scripts(): HTMLScriptElement[] {
  return Array.from(document.querySelectorAll<HTMLScriptElement>('script')).filter((s) => s.src === TURNSTILE_SCRIPT)
}

/** Cloudflare's script, as the page sees it once loaded: each widget passes at once. */
function loadFakeTurnstile() {
  let n = 0
  const api = {
    render: vi.fn((_el: HTMLElement, options: { action: string; callback: (token: string) => void }) => {
      n += 1
      options.callback(`token-${n}`)
      return `widget-${n}`
    }),
    remove: vi.fn(),
  }
  window.turnstile = api
  for (const script of scripts()) script.onload?.(new Event('load'))
  return api
}

beforeEach(() => {
  resetTurnstileForTests()
})

afterEach(() => {
  for (const script of scripts()) script.remove()
})

describe('Turnstile on a design request', () => {
  it('loads nothing from Cloudflare while the check is off', async () => {
    const { user } = renderPage(<RequestForm />)
    await user.type(await screen.findByLabelText('What should we make?'), 'A hook')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send request' })).toBeEnabled())
    expect(scripts()).toHaveLength(0)
    expect(screen.queryByTestId('turnstile')).not.toBeInTheDocument()
  })

  it('loads the script only once the widget mounts, and sends its token once', async () => {
    server.use(http.get('/api/store/v1/turnstile', () => HttpResponse.json({ site_key: '0x4AAA-site' })))
    const tokens: (string | null)[] = []
    server.use(
      http.post('/api/store/v1/draft/requests', ({ request }) => {
        tokens.push(request.headers.get('X-Turnstile-Token'))
        return HttpResponse.json({ id: 'd1', currency: 'USD', minor_exponent: 2, items: [] })
      }),
    )
    expect(scripts()).toHaveLength(0)
    const { user } = renderPage(<RequestForm />)
    await screen.findByTestId('turnstile')
    await waitFor(() => expect(scripts()).toHaveLength(1))
    const button = screen.getByRole('button', { name: 'Send request' })
    // No token yet: the form waits for the check.
    expect(button).toBeDisabled()
    const api = loadFakeTurnstile()
    await waitFor(() => expect(api.render).toHaveBeenCalledTimes(1))
    expect(api.render.mock.calls[0]![1]).toMatchObject({ sitekey: '0x4AAA-site', action: 'request' })
    await user.type(screen.getByLabelText('What should we make?'), 'A hook')
    await user.click(button)
    expect(await screen.findByText('Your request is in your order.')).toBeInTheDocument()
    expect(tokens).toEqual(['token-1'])
    // The sent form drops its widget; the next request draws a new one for a new token,
    // with the script already loaded.
    await waitFor(() => expect(api.remove).toHaveBeenCalledWith('widget-1'))
    await user.click(screen.getByRole('button', { name: 'Ask for another design' }))
    await waitFor(() => expect(api.render).toHaveBeenCalledTimes(2))
    expect(scripts()).toHaveLength(1)
  })

  it('asks for a new token after a refused send', async () => {
    server.use(http.get('/api/store/v1/turnstile', () => HttpResponse.json({ site_key: '0x4AAA-site' })))
    const tokens: (string | null)[] = []
    server.use(
      http.post('/api/store/v1/draft/requests', ({ request }) => {
        tokens.push(request.headers.get('X-Turnstile-Token'))
        return HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/bot_check_failed', status: 403, detail: 'The check that you are not a robot did not pass. Try it again.' },
          { status: 403 },
        )
      }),
    )
    const { user } = renderPage(<RequestForm />)
    await screen.findByTestId('turnstile')
    await waitFor(() => expect(scripts()).toHaveLength(1))
    const api = loadFakeTurnstile()
    await user.type(screen.getByLabelText('What should we make?'), 'A hook')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send request' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByText('The check that you are not a robot did not pass. Try it again.')).toBeInTheDocument()
    await waitFor(() => expect(api.render).toHaveBeenCalledTimes(2))
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    await waitFor(() => expect(tokens).toEqual(['token-1', 'token-2']))
  })

  it('reads the site key again after a send its failed read let through is refused', async () => {
    let reads = 0
    server.use(
      http.get('/api/store/v1/turnstile', () => {
        reads += 1
        return reads === 1 ? HttpResponse.json({ detail: 'down' }, { status: 503 }) : HttpResponse.json({ site_key: '0x4AAA-site' })
      }),
      http.post('/api/store/v1/draft/requests', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/bot_check_failed', status: 403, detail: 'The check that you are not a robot did not pass. Try it again.' },
          { status: 403 },
        ),
      ),
    )
    const { user } = renderPage(<RequestForm />)
    await user.type(await screen.findByLabelText('What should we make?'), 'A hook')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send request' })).toBeEnabled())
    expect(screen.queryByTestId('turnstile')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByText('The check that you are not a robot did not pass. Try it again.')).toBeInTheDocument()
    expect(await screen.findByTestId('turnstile')).toBeInTheDocument()
    expect(reads).toBe(2)
  })

  it('reads the site key again after a bot_check_failed even when the last read said off', async () => {
    let reads = 0
    server.use(
      http.get('/api/store/v1/turnstile', () => {
        reads += 1
        return HttpResponse.json({ site_key: reads === 1 ? null : '0x4AAA-site' })
      }),
      http.post('/api/store/v1/draft/requests', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/bot_check_failed', status: 403, detail: 'The check that you are not a robot did not pass. Try it again.' },
          { status: 403 },
        ),
      ),
    )
    const { user } = renderPage(<RequestForm />)
    await user.type(await screen.findByLabelText('What should we make?'), 'A hook')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send request' })).toBeEnabled())
    expect(reads).toBe(1)
    expect(screen.queryByTestId('turnstile')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByTestId('turnstile')).toBeInTheDocument()
    expect(reads).toBe(2)
  })

  it('keeps a successful read across sends that pass', async () => {
    let reads = 0
    server.use(
      http.get('/api/store/v1/turnstile', () => {
        reads += 1
        return HttpResponse.json({ site_key: null })
      }),
    )
    const { user } = renderPage(<RequestForm />)
    await user.type(await screen.findByLabelText('What should we make?'), 'A hook')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send request' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByText('Your request is in your order.')).toBeInTheDocument()
    expect(reads).toBe(1)
  })
})
