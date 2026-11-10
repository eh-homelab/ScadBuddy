import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { CustomerOrder } from '../api/client'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { OrderPage } from './OrderPage'

const HOSTILE = '<img src=x onerror=alert(1)>'
const TOKEN = 'T'.repeat(43)

const ORDER: CustomerOrder = {
  number: '2026-7',
  status: 'We are reviewing your order',
  currency: 'USD',
  minor_exponent: 2,
  can_cancel: true,
  items: [
    {
      id: 'i1',
      title: 'Name keychain',
      quantity: 1,
      status: 'Price ready',
      revisions: [
        {
          id: 'r1',
          number: 1,
          note: HOSTILE,
          sent_at: '2026-11-09T10:00:00Z',
          answer: null,
          answer_note: null,
          can_answer: true,
          pictures: [{ url: '/mock-files/render.png', kind: 'image', filename: 'render.png' }],
        },
      ],
      quote: { id: 'q1', unit_price_minor: 1250, quantity: 1, total_minor: 1250, accepted: false, can_accept: false },
    },
  ],
  payments: [],
  shipments: [],
}

const OPEN = { id: 'p1', kind: 'balance', amount_minor: 1250, currency: 'USD', status: 'open', pay_url: 'https://pay.example/1' } as const

interface Sent {
  path: string
  key: string | null
  body: Record<string, unknown>
}

function serve(...answers: CustomerOrder[]): Sent[] {
  const sent: Sent[] = []
  let reads = 0
  const record = async (request: Request) => {
    sent.push({
      path: new URL(request.url).pathname,
      key: request.headers.get('Idempotency-Key'),
      body: (await request.json()) as Record<string, unknown>,
    })
  }
  server.use(
    http.post('/api/store/v1/order/view', async ({ request }) => {
      await record(request)
      return HttpResponse.json(answers[Math.min(reads++, answers.length - 1)])
    }),
    http.post('/api/store/v1/order/revisions/:id/answer', async ({ request }) => {
      await record(request)
      return HttpResponse.json(answers[answers.length - 1])
    }),
  )
  return sent
}

describe('OrderPage', () => {
  it('reads the token from the fragment and sends it in the body only', async () => {
    const sent = serve(ORDER)
    renderPage(<OrderPage />, { route: `/o#${TOKEN}` })
    expect(await screen.findByRole('heading', { name: 'Order 2026-7' })).toBeInTheDocument()
    expect(sent).toHaveLength(1)
    expect(sent[0]!.path).toBe('/api/store/v1/order/view')
    expect(sent[0]!.body).toEqual({ token: TOKEN })
    expect(screen.getByText('Price: $12.50')).toBeInTheDocument()
  })

  it('renders what the shop typed as text', async () => {
    serve(ORDER)
    renderPage(<OrderPage />, { route: `/o#${TOKEN}` })
    expect(await screen.findByText(HOSTILE)).toBeInTheDocument()
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(screen.getByRole('img', { name: 'render.png' })).toHaveAttribute('src', '/mock-files/render.png')
  })

  it('says a link that opens nothing opens nothing', async () => {
    server.use(http.post('/api/store/v1/order/view', () => new HttpResponse(null, { status: 404 })))
    renderPage(<OrderPage />, { route: `/o#${TOKEN}` })
    expect(await screen.findByText(/This link does not open an order/)).toBeInTheDocument()
  })

  it('asks for changes only with a note', async () => {
    const answered = {
      ...ORDER,
      items: [
        {
          ...ORDER.items[0]!,
          revisions: [{ ...ORDER.items[0]!.revisions[0]!, can_answer: false, answer: 'changes_requested', answer_note: 'Bigger, please' }],
        },
      ],
    } satisfies CustomerOrder
    const sent = serve(ORDER, answered)
    const { user } = renderPage(<OrderPage />, { route: `/o#${TOKEN}` })
    await user.click(await screen.findByRole('button', { name: 'Ask for changes' }))
    const send = screen.getByRole('button', { name: 'Send' })
    expect(send).toBeDisabled()
    await user.type(screen.getByLabelText('What should change?'), '   ')
    expect(send).toBeDisabled()
    await user.clear(screen.getByLabelText('What should change?'))
    await user.type(screen.getByLabelText('What should change?'), 'Bigger, please')
    await user.click(send)
    expect(await screen.findByText('Bigger, please')).toBeInTheDocument()
    const answer = sent.find((s) => s.path.endsWith('/answer'))!
    expect(answer.path).toBe('/api/store/v1/order/revisions/r1/answer')
    expect(answer.body).toEqual({ token: TOKEN, answer: 'changes_requested', note: 'Bigger, please' })
    expect(answer.key).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('accepts a design with an Idempotency-Key', async () => {
    const sent = serve(ORDER)
    const { user } = renderPage(<OrderPage />, { route: `/o#${TOKEN}` })
    await user.click(await screen.findByRole('button', { name: 'Accept' }))
    await waitFor(() => expect(sent.some((s) => s.path.endsWith('/answer'))).toBe(true))
    const answer = sent.find((s) => s.path.endsWith('/answer'))!
    expect(answer.body).toEqual({ token: TOKEN, answer: 'accepted', note: '' })
    expect(answer.key).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('shows Paid only after a fresh read, never from the redirect', async () => {
    const open = { ...ORDER, payments: [OPEN] } satisfies CustomerOrder
    const paid = { ...ORDER, payments: [{ ...OPEN, status: 'paid', pay_url: null }] } satisfies CustomerOrder
    serve(open, paid)
    // The provider sent the browser back with its own "it worked" in the URL.
    const { user } = renderPage(<OrderPage />, { route: `/o?redirect_status=succeeded&paid=1#${TOKEN}` })
    expect(await screen.findByRole('link', { name: 'Pay $12.50' })).toHaveAttribute('href', 'https://pay.example/1')
    expect(screen.queryByText('Paid')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Check for updates' }))
    expect(await screen.findByText('Paid')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Pay $12.50' })).toBeNull()
  })
})
