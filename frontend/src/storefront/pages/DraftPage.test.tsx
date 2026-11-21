import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Draft, DraftItem } from '../api/client'
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
    { id: 'i1', kind: 'request', title: 'Design request', quantity: 1, inputs: null, notes: HOSTILE, attachments: [], sources: [], own_model: false, price: 'by_hand' },
    {
      id: 'i2',
      kind: 'model',
      title: 'Name keychain',
      quantity: 2,
      inputs: { name: 'Ada', base_colour: '#E8532F', ring: false },
      labels: { name: 'Name', base_colour: 'Base colour', ring: 'Keyring hole' },
      notes: null,
      attachments: [],
      sources: [],
      own_model: false,
      price: 'firm',
      unit_price_minor: 650,
      total_minor: 1300,
    },
    { id: 'i3', kind: 'model', title: 'Luggage tag', quantity: 1, inputs: {}, labels: {}, notes: null, attachments: [], sources: [], own_model: false, price: 'by_hand' },
  ],
  pay_now: false,
}

/** Every line firm (#6153): the order goes to payment once it is sent. */
const FIRM: Draft = {
  ...DRAFT,
  items: [DRAFT.items[1]!, { ...DRAFT.items[2]!, price: 'firm', unit_price_minor: 400, total_minor: 400 }],
  pay_now: true,
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

  it('shows a switch as Yes or No (#6141)', async () => {
    server.use(http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)))
    renderPage(<DraftPage />)
    const item = (await screen.findByText('Name keychain')).closest('li')!
    const ring = within(item).getByText('Keyring hole').closest('div')!
    expect(within(ring).getByText('No')).toBeInTheDocument()
    expect(within(item).queryByText('false')).toBeNull()
  })

  it('shows each firm price, and no number for a line the shop prices by hand (#6153)', async () => {
    server.use(http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)))
    renderPage(<DraftPage />)
    const keychain = (await screen.findByText('Name keychain')).closest('li')!
    expect(within(keychain).getByText('$13.00')).toBeInTheDocument()
    expect(within(keychain).getByText(/\$6\.50 each/)).toBeInTheDocument()
    const tag = screen.getByText('Luggage tag').closest('li')!
    expect(within(tag).getByText('The shop prices this by hand')).toBeInTheDocument()
    expect(within(tag).queryByText(/\$/)).toBeNull()
    expect(screen.getByRole('note')).toHaveTextContent('The prices shown come to $13.00. The shop prices the other designs by hand')
  })

  it('shows the total to pay when every line is firm (#6153)', async () => {
    server.use(http.get('/api/store/v1/draft', () => HttpResponse.json(FIRM)))
    renderPage(<DraftPage />)
    expect(await screen.findByText('Total $17.00')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent('you pay from the link we mail you')
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

  it('lists no shipping option when the shop has none', async () => {
    server.use(http.get('/api/store/v1/draft', () => HttpResponse.json({ ...DRAFT, shipping_options: [] })))
    renderPage(<DraftPage />)
    expect(await screen.findByLabelText('Your name')).toBeInTheDocument()
    expect(screen.queryByRole('radio')).toBeNull()
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

  it('says the order is priced when it went straight to payment (#6153)', async () => {
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json({ ...FIRM, shipping_options: [] })),
      http.post('/api/store/v1/draft/submit', () =>
        HttpResponse.json({ id: 'd1', number: 'ORD-2026-8', email: 'ada@example.com', pay_now: true }),
      ),
    )
    const { user } = renderPage(<DraftPage />)
    await user.type(await screen.findByLabelText('Your name'), 'Ada')
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.click(screen.getByRole('button', { name: 'Send my order' }))
    expect(await screen.findByText(/Follow the link in the mail to pay for your order/)).toBeInTheDocument()
  })

  it('loads the cart again when a price changed before the send (#6153)', async () => {
    let reads = 0
    const changed: Draft = { ...FIRM, shipping_options: [], items: [{ ...FIRM.items[0]!, unit_price_minor: 700, total_minor: 1400 }, FIRM.items[1]!] }
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json(reads++ === 0 ? { ...FIRM, shipping_options: [] } : changed)),
      http.post('/api/store/v1/draft/submit', () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/storefront/prices_changed',
            title: 'Conflict',
            status: 409,
            detail: 'Some prices changed. Check your order, then send it again.',
            items: ['i2'],
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderPage(<DraftPage />)
    await user.type(await screen.findByLabelText('Your name'), 'Ada')
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.click(screen.getByRole('button', { name: 'Send my order' }))
    expect(await screen.findByText('Some prices changed. Check your order, then send it again.')).toBeInTheDocument()
    expect(await screen.findByText('$14.00')).toBeInTheDocument()
    expect(screen.queryByText(/We have your order/)).toBeNull()
  })

  it('shows that the shop prices a line whose price went before the send (#6153)', async () => {
    let reads = 0
    const gone: Draft = {
      ...FIRM,
      shipping_options: [],
      items: [{ ...FIRM.items[0]!, price: 'by_hand', unit_price_minor: null, total_minor: null }, FIRM.items[1]!],
      pay_now: false,
    }
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json(reads++ === 0 ? { ...FIRM, shipping_options: [] } : gone)),
      http.post('/api/store/v1/draft/submit', () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/storefront/prices_changed',
            title: 'Conflict',
            status: 409,
            detail: 'Some prices changed. Check your order, then send it again.',
            items: [{ id: 'i2', price: 'by_hand' }],
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderPage(<DraftPage />)
    await user.type(await screen.findByLabelText('Your name'), 'Ada')
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.click(screen.getByRole('button', { name: 'Send my order' }))
    expect(await screen.findByText('Some prices changed. Check your order, then send it again.')).toBeInTheDocument()
    const keychain = (await screen.findByText('Name keychain')).closest('li')!
    expect(await within(keychain).findByText('The shop prices this by hand')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent('The shop prices the other designs by hand')
  })

  it('sends a country typed in lower case as the two capital letters the server takes', async () => {
    let body: { address?: { country?: string } } | null = null
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)),
      http.post('/api/store/v1/draft/submit', async ({ request }) => {
        body = (await request.json()) as typeof body
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
    await user.type(screen.getByLabelText(/Country code/), 'gb')
    await user.click(screen.getByRole('button', { name: 'Send my order' }))
    expect(await screen.findByText(/We have your order/)).toBeInTheDocument()
    expect(body!.address!.country).toBe('GB')
  })

  it('refuses a country that is not two letters before it sends anything', async () => {
    let sent = false
    server.use(
      http.get('/api/store/v1/draft', () => HttpResponse.json(DRAFT)),
      http.post('/api/store/v1/draft/submit', () => {
        sent = true
        return HttpResponse.json({})
      }),
    )
    const { user } = renderPage(<DraftPage />)
    await user.type(await screen.findByLabelText('Your name'), 'Ada')
    await user.type(screen.getByLabelText('Email'), 'ada@example.com')
    await user.click(screen.getByRole('radio', { name: /Post/ }))
    await user.type(screen.getByLabelText('Recipient'), 'Ada Lovelace')
    await user.type(screen.getByLabelText('Street address'), '1 Analytical Way')
    await user.type(screen.getByLabelText('Town or city'), 'London')
    await user.type(screen.getByLabelText(/Country code/), '1a')
    await user.click(screen.getByRole('button', { name: 'Send my order' }))
    expect(await screen.findByText('Give the country as two letters, such as US.')).toBeInTheDocument()
    expect(sent).toBe(false)
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

  describe('a customer\'s own models (#6133)', () => {
    const file = (id: string, extra: Partial<NonNullable<DraftItem['sources']>[number]>) => ({
      id,
      kind: 'file' as const,
      purpose: 'print' as const,
      filename: `${id}.stl`,
      size: 10,
      url: null,
      note: '',
      check_status: 'checking' as const,
      check_reason: null,
      status: 'open' as const,
      preview_url: null,
      ...extra,
    })
    const link = (id: string, url: string, purpose: 'print' | 'purchase', note = '') => ({
      id,
      kind: 'link' as const,
      purpose,
      filename: null,
      size: null,
      url,
      note,
      check_status: null,
      check_reason: null,
      status: (purpose === 'purchase' ? 'pending' : 'manual') as 'pending' | 'manual',
      preview_url: null,
    })
    const MODELS: Draft = {
      ...DRAFT,
      items: [
        {
          id: 'm1',
          kind: 'request',
          title: 'Your models',
          quantity: 2,
          inputs: null,
          notes: '',
          attachments: [],
          own_model: true,
          price: 'by_hand',
          sources: [
            file('checking', {}),
            file('ready', { check_status: 'ready', preview_url: '/api/store/v1/draft/sources/ready/preview' }),
            file('broken', { check_status: 'refused', check_reason: 'This file is not a 3MF: it is not a readable zip.' }),
            file('old', { check_status: 'refused', check_reason: 'This file expired before the order was sent; upload it again.' }),
            link('l1', 'javascript:alert(1)', 'print'),
            link('l2', 'https://parts.example.com/m', 'purchase', HOSTILE),
          ],
        },
      ],
    }

    it('shows one line per file and link, each in plain words', async () => {
      server.use(http.get('/api/store/v1/draft', () => HttpResponse.json(MODELS)))
      renderPage(<DraftPage />)
      const item = (await screen.findByText('Your models', { selector: 'h2' })).closest('li')!
      const lines = within(item).getAllByRole('listitem')
      expect(lines).toHaveLength(6)
      expect(within(lines[0]!).getByText('Checking')).toBeInTheDocument()
      expect(within(lines[1]!).getByText('Ready')).toBeInTheDocument()
      expect(within(lines[1]!).getByRole('img')).toHaveAttribute('src', '/api/store/v1/draft/sources/ready/preview')
      expect(within(lines[2]!).getByText('This file is not a 3MF: it is not a readable zip.')).toBeInTheDocument()
      expect(within(lines[3]!).getByText(/This file expired;/)).toBeInTheDocument()
      expect(within(lines[3]!).getByRole('link', { name: 'upload it again' })).toHaveAttribute('href', '/print-my-model')
    })

    it('shows a link and a thing to buy as text, never as an anchor', async () => {
      server.use(http.get('/api/store/v1/draft', () => HttpResponse.json(MODELS)))
      renderPage(<DraftPage />)
      expect(await screen.findByText('javascript:alert(1)')).toBeInTheDocument()
      expect(screen.getByText('https://parts.example.com/m')).toBeInTheDocument()
      expect(screen.getByText(HOSTILE)).toBeInTheDocument()
      expect(document.querySelector('a[href^="javascript"]')).toBeNull()
      expect(document.querySelector('a[href^="https://parts"]')).toBeNull()
      expect(document.querySelector('img[src="x"]')).toBeNull()
    })

    it('says a refused file blocks the order until it is removed', async () => {
      server.use(http.get('/api/store/v1/draft', () => HttpResponse.json(MODELS)))
      renderPage(<DraftPage />)
      expect(
        await screen.findByText('A file cannot be printed. Remove “Your models”, then add the files again without it.'),
      ).toBeInTheDocument()
    })

    it('reads the cart again while a file is being checked', async () => {
      let reads = 0
      server.use(
        http.get('/api/store/v1/draft', () => {
          reads += 1
          const first = MODELS.items[0]!
          const sources = reads === 1 ? [file('a', {})] : [file('a', { check_status: 'ready', preview_url: '/p' })]
          return HttpResponse.json({ ...MODELS, items: [{ ...first, sources }] })
        }),
      )
      renderPage(<DraftPage />)
      expect(await screen.findByText('Checking')).toBeInTheDocument()
      expect(await screen.findByText('Ready', {}, { timeout: 4000 })).toBeInTheDocument()
    })
  })
})

