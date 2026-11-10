import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { PreviewJob } from '../../components/Preview'
import type { CustomerOrder } from '../api/client'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { OrderPage } from './OrderPage'

vi.mock('../../components/Preview', () => ({
  Preview: ({ job }: { job?: PreviewJob }) => <div data-testid="preview" data-url={job?.preview_url ?? ''} />,
}))

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
          pictures: [
            { url: '/mock-files/render.png', kind: 'image', caption: 'Design 1, picture 1' },
            { url: null, kind: 'image', caption: 'The test print' },
          ],
          colours: [{ slot: 0, colour: '#1b6ca8', name: 'Ocean blue' }],
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

  it('reads a signed-in order from ?order= and sends its id in the body only', async () => {
    const sent = serve(ORDER)
    renderPage(<OrderPage />, { route: '/o?order=o-1' })
    expect(await screen.findByRole('heading', { name: 'Order 2026-7' })).toBeInTheDocument()
    expect(sent[0]!.body).toEqual({ order_id: 'o-1' })
  })

  it('takes the fragment token over ?order=', async () => {
    const sent = serve(ORDER)
    renderPage(<OrderPage />, { route: `/o?order=o-1#${TOKEN}` })
    expect(await screen.findByRole('heading', { name: 'Order 2026-7' })).toBeInTheDocument()
    expect(sent[0]!.body).toEqual({ token: TOKEN })
  })

  it("shows a design's 3D preview in the shared viewer", async () => {
    const revision = ORDER.items[0]!.revisions[0]!
    const model = { url: 'https://store.example/customer-file-1', kind: 'model', caption: 'Design 1, picture 3' } as const
    serve({
      ...ORDER,
      items: [{ ...ORDER.items[0]!, revisions: [{ ...revision, pictures: [...revision.pictures, model] }] }],
    })
    renderPage(<OrderPage />, { route: `/o#${TOKEN}` })
    expect(await screen.findByTestId('preview')).toHaveAttribute('data-url', model.url)
    expect(screen.getByRole('figure', { name: 'Design 1, picture 3' })).toBeInTheDocument()
  })

  it('renders what the shop typed as text', async () => {
    serve(ORDER)
    renderPage(<OrderPage />, { route: `/o#${TOKEN}` })
    expect(await screen.findByText(HOSTILE)).toBeInTheDocument()
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(screen.getByRole('img', { name: 'Design 1, picture 1' })).toHaveAttribute('src', '/mock-files/render.png')
  })

  it('shows a picture it cannot show now as its caption, and the colours', async () => {
    serve(ORDER)
    renderPage(<OrderPage />, { route: `/o#${TOKEN}` })
    expect(await screen.findByText('The test print: this picture cannot be shown now.')).toBeInTheDocument()
    expect(screen.getAllByRole('img')).toHaveLength(1)
    expect(screen.getByText('Colour 1: Ocean blue')).toBeInTheDocument()
  })

  it('cancels with the reason and a key, and shows a refusal', async () => {
    const sent: Sent[] = []
    let calls = 0
    server.use(
      http.post('/api/store/v1/order/view', () => HttpResponse.json(ORDER)),
      http.post('/api/store/v1/order/cancel', async ({ request }) => {
        sent.push({ path: '', key: request.headers.get('Idempotency-Key'), body: (await request.json()) as Record<string, unknown> })
        calls += 1
        if (calls === 1) {
          return HttpResponse.json(
            {
              type: 'https://scadbuddy.dev/problems/storefront/order_approved',
              title: 'Conflict',
              status: 409,
              detail: 'Part of your order is approved already. Ask the shop to cancel it.',
            },
            { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }
        return HttpResponse.json({ ...ORDER, status: 'Cancelled', can_cancel: false })
      }),
    )
    const { user } = renderPage(<OrderPage />, { route: `/o#${TOKEN}` })
    await user.click(await screen.findByRole('button', { name: 'Cancel order' }))
    await user.type(screen.getByLabelText('Why are you cancelling? (optional)'), 'Changed my mind')
    await user.click(screen.getByRole('button', { name: 'Cancel my order' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Part of your order is approved already. Ask the shop to cancel it.')
    await user.click(screen.getByRole('button', { name: 'Cancel my order' }))
    expect(await screen.findByText('Cancelled')).toBeInTheDocument()
    expect(sent.map((s) => s.body)).toEqual([
      { token: TOKEN, reason: 'Changed my mind' },
      { token: TOKEN, reason: 'Changed my mind' },
    ])
    // The same action sent again after a refusal keeps its key.
    expect(sent[0]!.key).toMatch(/^[0-9a-f-]{36}$/)
    expect(sent[1]!.key).toBe(sent[0]!.key)
  })

  it('asks for the mail\'s link when it has no token and no order id', async () => {
    const sent = serve(ORDER)
    renderPage(<OrderPage />, { route: '/o' })
    expect(await screen.findByText('Open your order from the link in your mail.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Your orders' })).toHaveAttribute('href', '/account')
    expect(sent).toEqual([])
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
