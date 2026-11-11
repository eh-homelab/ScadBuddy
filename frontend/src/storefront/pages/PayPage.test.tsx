import { act, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

const navigate = vi.hoisted(() => vi.fn())
vi.mock('react-router', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useNavigate: () => navigate,
}))
import type { CustomerOrder, PayCheckout } from '../api/client'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { OrderPage } from './OrderPage'
import { PayPage } from './PayPage'
import userEvent from '@testing-library/user-event'
import { Link } from 'react-router'

const TOKEN = 'T'.repeat(43)

const CHECKOUT: PayCheckout = {
  amount_minor: 1250,
  currency: 'USD',
  order_number: '2026-7',
  client_id: 'AYSq3RDGsmBLJE-otTkBtM',
  paypal_order_id: '5O190127TN364715T',
  minor_exponent: 2,
}

interface ButtonOptions {
  createOrder: () => Promise<string>
  onApprove: (data: { orderID: string }) => Promise<void>
  onError: (error: unknown) => void
}

function fakePayPal() {
  const rendered: ButtonOptions[] = []
  const close = vi.fn()
  const render = vi.fn(async () => undefined)
  ;(window as unknown as { paypal: unknown }).paypal = {
    Buttons: (options: ButtonOptions) => {
      rendered.push(options)
      return { render, close, isEligible: () => true }
    },
  }
  return { rendered, render, close }
}

function serve(answer: () => Response): { bodies: unknown[]; urls: string[] } {
  const seen = { bodies: [] as unknown[], urls: [] as string[] }
  server.use(
    http.post('/api/store/v1/pay/checkout', async ({ request }) => {
      seen.urls.push(request.url)
      seen.bodies.push(await request.json())
      return answer()
    }),
  )
  return seen
}

function serveOrder(): void {
  const order: CustomerOrder = {
    number: '2026-7',
    status: 'We are reviewing your order',
    currency: 'USD',
    minor_exponent: 2,
    can_cancel: false,
    items: [],
    payments: [
      { id: 'p1', kind: 'balance', amount_minor: 1250, currency: 'USD', status: 'open', pay_url: `/pay#${TOKEN}`, manual: false },
    ],
    payment_instructions: null,
    shipments: [],
  }
  server.use(http.post('/api/store/v1/order/view', () => HttpResponse.json(order)))
}

async function sdk(): Promise<HTMLScriptElement> {
  return waitFor(() => {
    const script = document.querySelector<HTMLScriptElement>('script[src*="paypal.com/sdk/js"]')
    expect(script).not.toBeNull()
    return script!
  })
}

function history(length: number) {
  Object.defineProperty(window.history, 'length', { value: length, configurable: true })
}

afterEach(() => {
  history(1)
  sessionStorage.clear()
  navigate.mockReset()
  delete (window as unknown as { paypal?: unknown }).paypal
  document.querySelectorAll('script[src*="paypal.com"]').forEach((node) => node.remove())
})

describe('PayPage', () => {
  it('reads the token from the fragment, sends it in the body only, and shows what it is for', async () => {
    const seen = serve(() => HttpResponse.json(CHECKOUT))
    renderPage(<PayPage />, { route: `/pay#${TOKEN}` })
    expect(await screen.findByText('$12.50')).toBeInTheDocument()
    expect(screen.getByText(/order 2026-7/)).toBeInTheDocument()
    expect(seen.bodies).toEqual([{ token: TOKEN }])
    expect(seen.urls.join(' ')).not.toContain(TOKEN)
  })

  it('loads the SDK with the checkout\'s client id, its currency and Venmo funding', async () => {
    serve(() => HttpResponse.json(CHECKOUT))
    renderPage(<PayPage />, { route: `/pay#${TOKEN}` })
    const script = await sdk()
    const url = new URL(script.src)
    expect(url.origin + url.pathname).toBe('https://www.paypal.com/sdk/js')
    expect(url.searchParams.get('client-id')).toBe(CHECKOUT.client_id)
    expect(url.searchParams.get('currency')).toBe('USD')
    expect(url.searchParams.get('enable-funding')).toBe('venmo')
    expect(script.src).not.toContain(TOKEN)
  })

  async function approve(): Promise<void> {
    serve(() => HttpResponse.json(CHECKOUT))
    const paypal = fakePayPal()
    renderPage(<PayPage />, { route: `/pay#${TOKEN}` })
    const script = await sdk()
    act(() => {
      script.dispatchEvent(new Event('load'))
    })
    await waitFor(() => expect(paypal.render).toHaveBeenCalledTimes(1))
    expect(await paypal.rendered[0]!.createOrder()).toBe(CHECKOUT.paypal_order_id)
    expect(screen.queryByText(/Thanks/)).not.toBeInTheDocument()
    await act(async () => paypal.rendered[0]!.onApprove({ orderID: CHECKOUT.paypal_order_id }))
    expect(await screen.findByText('Thanks. We will confirm your payment shortly.')).toBeInTheDocument()
  }

  it('renders the buttons for the request\'s PayPal order and thanks the payer on approval', async () => {
    await approve()
    expect(screen.getByText(/Open your order from the link in your mail/)).toBeInTheDocument()
    expect(screen.getAllByRole('link').map((a) => a.getAttribute('href'))).toEqual(['/account'])
    expect(screen.queryByRole('button', { name: 'Back to your order' })).not.toBeInTheDocument()
  })

  it('goes back to the order when the payer clicked the order page\'s pay link', async () => {
    serveOrder()
    const { unmount } = renderPage(<OrderPage />, { route: `/o#${TOKEN}` })
    const link = await screen.findByRole('link', { name: 'Pay $12.50' })
    expect(link).toHaveAttribute('rel', 'noreferrer')
    // jsdom cannot navigate; the click is all the page sees.
    link.addEventListener('click', (event) => event.preventDefault())
    await userEvent.setup().click(link)
    unmount()
    history(2)
    await approve()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Back to your order' }))
    expect(navigate).toHaveBeenCalledWith(-1)
    expect(screen.queryByText(/Open your order from the link in your mail/)).not.toBeInTheDocument()
    expect(sessionStorage.getItem('sb_pay_from_order')).toBeNull()
  })

  it('does not mark the order page\'s pay link when a modifier key or another button is used', async () => {
    serveOrder()
    renderPage(<OrderPage />, { route: `/o#${TOKEN}` })
    const link = await screen.findByRole('link', { name: 'Pay $12.50' })
    link.addEventListener('click', (event) => event.preventDefault())
    const user = userEvent.setup()
    for (const key of ['Control', 'Meta', 'Shift', 'Alt']) {
      await user.keyboard(`{${key}>}`)
      await user.click(link)
      await user.keyboard(`{/${key}}`)
      expect(sessionStorage.getItem('sb_pay_from_order')).toBeNull()
    }
    await user.pointer({ keys: '[MouseMiddle]', target: link })
    expect(sessionStorage.getItem('sb_pay_from_order')).toBeNull()
    await user.click(link)
    expect(sessionStorage.getItem('sb_pay_from_order')).toBe('1')
  })

  it('starts over when a second pay link changes only the fragment', async () => {
    const OTHER = 'O'.repeat(43)
    server.use(
      http.post('/api/store/v1/pay/checkout', async ({ request }) => {
        const { token } = (await request.json()) as { token: string }
        return token === TOKEN ? HttpResponse.json(CHECKOUT) : new HttpResponse(null, { status: 404 })
      }),
    )
    const paypal = fakePayPal()
    const { user } = renderPage(
      <>
        <PayPage />
        <Link to={`/pay#${OTHER}`}>other link</Link>
      </>,
      { route: `/pay#${TOKEN}` },
    )
    const script = await sdk()
    act(() => {
      script.dispatchEvent(new Event('load'))
    })
    await waitFor(() => expect(paypal.render).toHaveBeenCalledTimes(1))
    await act(async () => paypal.rendered[0]!.onApprove({ orderID: CHECKOUT.paypal_order_id }))
    expect(await screen.findByText(/Thanks/)).toBeInTheDocument()
    await user.click(screen.getByRole('link', { name: 'other link' }))
    expect(await screen.findByText(/This link does not open a payment/)).toBeInTheDocument()
    expect(screen.queryByText(/Thanks/)).not.toBeInTheDocument()
  })

  it('offers no way back when the pay link opened in a tab of its own', async () => {
    sessionStorage.setItem('sb_pay_from_order', '1')
    history(1)
    await approve()
    expect(screen.queryByRole('button', { name: 'Back to your order' })).not.toBeInTheDocument()
    expect(screen.getByText(/Open your order from the link in your mail/)).toBeInTheDocument()
  })

  it('offers no way back when the storage refuses', async () => {
    history(2)
    const refuse = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    await approve()
    refuse.mockRestore()
    expect(screen.queryByRole('button', { name: 'Back to your order' })).not.toBeInTheDocument()
    expect(screen.getByText(/Open your order from the link in your mail/)).toBeInTheDocument()
  })

  it.each([
    ['JPY', 0, '¥1,250'],
    ['USD', 2, '$12.50'],
    ['KWD', 3, 'KWD 1.250'],
  ])('shows %s with %i decimals from the checkout\'s own exponent', async (currency, exponent, shown) => {
    serve(() => HttpResponse.json({ ...CHECKOUT, currency, minor_exponent: exponent }))
    renderPage(<PayPage />, { route: `/pay#${TOKEN}` })
    expect(await screen.findByText((text) => text.replace(/\s/g, ' ').includes(shown.replace(/\s/g, ' ')))).toBeInTheDocument()
  })

  it('says so when PayPal cannot be reached', async () => {
    serve(() => HttpResponse.json(CHECKOUT))
    renderPage(<PayPage />, { route: `/pay#${TOKEN}` })
    const script = await sdk()
    act(() => {
      script.dispatchEvent(new Event('error'))
    })
    expect(await screen.findByRole('alert')).toHaveTextContent(/PayPal/)
  })

  it('says a link that opens nothing opens nothing, and loads no SDK', async () => {
    serve(() => new HttpResponse(null, { status: 404 }))
    renderPage(<PayPage />, { route: `/pay#${TOKEN}` })
    expect(await screen.findByText(/This link does not open a payment/)).toBeInTheDocument()
    expect(document.querySelector('script[src*="paypal.com"]')).toBeNull()
  })

  it('never reads the token from the path', async () => {
    const seen = serve(() => HttpResponse.json(CHECKOUT))
    renderPage(<PayPage />, { route: `/pay/${TOKEN}` })
    expect(await screen.findByText(/This link does not open a payment/)).toBeInTheDocument()
    expect(seen.bodies).toEqual([])
  })
})
