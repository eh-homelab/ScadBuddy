import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Draft } from '../api/client'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { DraftPage } from './DraftPage'
import { TURNSTILE_SCRIPT, resetTurnstileForTests } from '../turnstile'

const HOSTILE = '<img src=x onerror=alert(1)>'

const DRAFT: Draft = {
  id: 'd1',
  currency: 'USD',
  minor_exponent: 2,
  shipping_options: [
    { id: 'post', label: 'Post', amount_minor: 700, ships: true },
    { id: 'pickup', label: 'Pick up at the workshop', amount_minor: 0, ships: false },
  ],
  items: [
    { id: 'i1', kind: 'request', title: 'Design request', quantity: 1, inputs: null, notes: HOSTILE, attachments: [] },
    {
      id: 'i2',
      kind: 'model',
      title: 'Name keychain',
      quantity: 2,
      inputs: { name: 'Ada', base_colour: '#E8532F' },
      labels: { name: 'Name', base_colour: 'Base colour' },
      notes: null,
      attachments: [],
    },
    { id: 'i3', kind: 'model', title: 'Luggage tag', quantity: 1, inputs: {}, labels: {}, notes: null, attachments: [] },
  ],
}

describe('DraftPage', () => {
  it('renders a request text as text', async () => {
    server.use(http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)))
    renderPage(<DraftPage />)
    expect(await screen.findByText(HOSTILE)).toBeInTheDocument()
    expect(document.querySelector('img[src="x"]')).toBeNull()
  })

  it('removes an item with an Idempotency-Key', async () => {
    let key: string | null = null
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)),
      http.delete('/api/store/v1/draft/items/:id', ({ request, params }) => {
        key = request.headers.get('Idempotency-Key')
        return HttpResponse.json({ ...DRAFT, items: DRAFT.items.filter((item) => item.id !== params.id) })
      }),
    )
    const { user } = renderPage(<DraftPage />)
    const item = (await screen.findByText('Name keychain')).closest('li')!
    expect(within(item).getByText('Ada')).toBeInTheDocument()
    await user.click(within(item).getByRole('button', { name: 'Remove Name keychain' }))
    await screen.findByText(HOSTILE)
    expect(screen.queryByText('Name keychain')).toBeNull()
    expect(key).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('invites a visitor with an empty cart to browse', async () => {
    renderPage(<DraftPage />)
    expect(await screen.findByText('Your order is empty.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Browse the designs' })).toHaveAttribute('href', '/')
  })

  it('names each value by its label', async () => {
    server.use(http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)))
    renderPage(<DraftPage />)
    const item = (await screen.findByText('Name keychain')).closest('li')!
    expect(within(item).getByText('Base colour')).toBeInTheDocument()
    expect(within(item).queryByText('base_colour')).toBeNull()
  })

  it('removes one item at a time, so a late answer never puts one back', async () => {
    const answers: (() => void)[] = []
    let current = DRAFT
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)),
      http.delete('/api/store/v1/draft/items/:id', async ({ params }) => {
        current = { ...current, items: current.items.filter((item) => item.id !== params.id) }
        const answer = current
        await new Promise<void>((resolve) => answers.push(resolve))
        return HttpResponse.json(answer)
      }),
    )
    const { user } = renderPage(<DraftPage />)
    await user.click(await screen.findByRole('button', { name: 'Remove Name keychain' }))
    expect(screen.getByRole('button', { name: 'Remove Luggage tag' })).toBeDisabled()
    await waitFor(() => expect(answers).toHaveLength(1))
    answers[0]!()
    await waitFor(() => expect(screen.queryByText('Name keychain')).toBeNull())
    await user.click(screen.getByRole('button', { name: 'Remove Luggage tag' }))
    await waitFor(() => expect(answers).toHaveLength(2))
    answers[1]!()
    await waitFor(() => expect(screen.queryByText('Luggage tag')).toBeNull())
    expect(screen.queryByText('Name keychain')).toBeNull()
  })
})

function scripts(): HTMLScriptElement[] {
  return Array.from(document.querySelectorAll<HTMLScriptElement>('script')).filter((s) => s.src === TURNSTILE_SCRIPT)
}

describe('Submitting the order', () => {
  beforeEach(() => {
    resetTurnstileForTests()
  })

  afterEach(() => {
    for (const script of scripts()) script.remove()
  })

  it('shows the shipping options with their prices', async () => {
    server.use(http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)))
    renderPage(<DraftPage />)
    expect(await screen.findByRole('radio', { name: /Post.*\$7\.00/ })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /Pick up at the workshop.*\$0\.00/ })).toBeInTheDocument()
  })

  it('lists no shipping option for a cart the shop has no options for in its currency', async () => {
    // The draft view sends none when the cart's currency is not the shop's now.
    server.use(http.get('/api/store/v1/draft', () => HttpResponse.json({ ...DRAFT, shipping_options: [] })))
    renderPage(<DraftPage />)
    expect(await screen.findByLabelText('Your name')).toBeInTheDocument()
    expect(screen.queryByRole('radio')).toBeNull()
  })

  it('tells the visitor the shipping currency changed when the submit is refused for it', async () => {
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)),
      http.post('/api/store/v1/draft/submit', () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/storefront/shipping_currency_mismatch',
            status: 409,
            detail: 'The shop prices changed currency.',
          },
          { status: 409 },
        ),
      ),
    )
    const { user } = renderPage(<DraftPage />)
    await user.type(await screen.findByLabelText('Your name'), 'Ada')
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.click(screen.getByRole('radio', { name: /Pick up/ }))
    await user.click(screen.getByRole('button', { name: 'Send my order' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The shop prices changed currency.')
  })

  it('asks for an address only when the option ships', async () => {
    server.use(http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)))
    const { user } = renderPage(<DraftPage />)
    await user.click(await screen.findByRole('radio', { name: /Pick up/ }))
    expect(screen.queryByLabelText('Street address')).toBeNull()
    await user.click(screen.getByRole('radio', { name: /Post/ }))
    expect(screen.getByLabelText('Street address')).toBeInTheDocument()
    expect(screen.getByLabelText('Recipient')).toBeInTheDocument()
  })

  it('sends the order with a key and says where the link went', async () => {
    let sent: { key: string | null; body: unknown } | null = null
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)),
      http.post('/api/store/v1/draft/submit', async ({ request }) => {
        sent = { key: request.headers.get('Idempotency-Key'), body: await request.json() }
        return HttpResponse.json({ id: 'd1', number: 'ORD-2026-7', email: 'ada@example.com' })
      }),
    )
    const { user } = renderPage(<DraftPage />)
    await user.type(await screen.findByLabelText('Your name'), 'Ada')
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.click(screen.getByRole('radio', { name: /Post/ }))
    await user.type(screen.getByLabelText('Recipient'), 'Ada Lovelace')
    await user.type(screen.getByLabelText('Street address'), '1 Analytical Way')
    await user.type(screen.getByLabelText('Town or city'), 'London')
    await user.click(screen.getByRole('button', { name: 'Send my order' }))
    expect(await screen.findByText('We have your order. We sent a link to ada@example.com.')).toBeInTheDocument()
    expect(sent!.key).toMatch(/^[0-9a-f-]{36}$/)
    expect(sent!.body).toEqual({
      draft_id: 'd1',
      name: 'Ada',
      email: 'ada@example.com',
      shipping_option_id: 'post',
      address: { recipient: 'Ada Lovelace', lines: ['1 Analytical Way'], locality: 'London' },
    })
    expect(screen.queryByText('Name keychain')).toBeNull()
  })

  it('sends no address for an option that does not ship', async () => {
    let body: unknown = null
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)),
      http.post('/api/store/v1/draft/submit', async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ id: 'd1', number: 'ORD-2026-7', email: 'ada@example.com' })
      }),
    )
    const { user } = renderPage(<DraftPage />)
    await user.type(await screen.findByLabelText('Your name'), 'Ada')
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.click(screen.getByRole('radio', { name: /Pick up/ }))
    await user.click(screen.getByRole('button', { name: 'Send my order' }))
    await screen.findByText('We have your order. We sent a link to ada@example.com.')
    expect(body).toEqual({ draft_id: 'd1', name: 'Ada', email: 'ada@example.com', shipping_option_id: 'pickup' })
  })

  it('shows a refusal and keeps the form', async () => {
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)),
      http.post('/api/store/v1/draft/submit', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/email_invalid', status: 422, detail: 'Give one email address, such as ada@example.com.' },
          { status: 422 },
        ),
      ),
    )
    const { user } = renderPage(<DraftPage />)
    await user.type(await screen.findByLabelText('Your name'), 'Ada')
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.click(screen.getByRole('radio', { name: /Pick up/ }))
    await user.click(screen.getByRole('button', { name: 'Send my order' }))
    expect(await screen.findByText('Give one email address, such as ada@example.com.')).toBeInTheDocument()
    expect(screen.getByLabelText('Your name')).toHaveValue('Ada')
  })

  it('loads the cart again when it was sent from another tab', async () => {
    let loads = 0
    server.use(
      http.get('/api/store/v1/draft', () => {
        loads += 1
        return loads === 1 ? HttpResponse.json(DRAFT) : HttpResponse.json(null)
      }),
      http.post('/api/store/v1/draft/submit', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/order_submitted', status: 409, detail: 'This order is sent already.' },
          { status: 409 },
        ),
      ),
    )
    const { user } = renderPage(<DraftPage />)
    await user.type(await screen.findByLabelText('Your name'), 'Ada')
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.click(screen.getByRole('radio', { name: /Pick up/ }))
    await user.click(screen.getByRole('button', { name: 'Send my order' }))
    expect(await screen.findByText('Your order is empty.')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('This order is sent already.')
    expect(screen.queryByLabelText('Your name')).toBeNull()
    expect(loads).toBe(2)
  })

  it('sends the bot check token in X-Turnstile-Token', async () => {
    let token: string | null = null
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json({ ...DRAFT, shipping_options: [] })),
      http.get('/api/store/v1/turnstile', () => HttpResponse.json({ site_key: '0x4AAA-site' })),
      http.post('/api/store/v1/draft/submit', ({ request }) => {
        token = request.headers.get('X-Turnstile-Token')
        return HttpResponse.json({ id: 'd1', number: 'ORD-2026-7', email: 'ada@example.com' })
      }),
    )
    const { user } = renderPage(<DraftPage />)
    await screen.findByTestId('turnstile')
    await waitFor(() => expect(scripts()).toHaveLength(1))
    const render = vi.fn((_el: HTMLElement, options: { action: string; callback: (t: string) => void }) => {
      options.callback('token-1')
      return 'widget-1'
    })
    window.turnstile = { render, remove: vi.fn() }
    for (const script of scripts()) script.onload?.(new Event('load'))
    await waitFor(() => expect(render).toHaveBeenCalled())
    expect(render.mock.calls[0]![1]).toMatchObject({ action: 'submit' })
    await user.type(screen.getByLabelText('Your name'), 'Ada')
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.click(screen.getByRole('button', { name: 'Send my order' }))
    await screen.findByText('We have your order. We sent a link to ada@example.com.')
    expect(token).toBe('token-1')
  })
})
