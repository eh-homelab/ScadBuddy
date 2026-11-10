import { act, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

const navigate = vi.hoisted(() => vi.fn())
vi.mock('react-router', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useNavigate: () => navigate,
}))
import type { PayCheckout } from '../api/client'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { PayPage } from './PayPage'

const TOKEN = 'T'.repeat(43)

const CHECKOUT: PayCheckout = {
  amount_minor: 1250,
  currency: 'USD',
  order_number: '2026-7',
  client_id: 'AYSq3RDGsmBLJE-otTkBtM',
  environment: 'sandbox',
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

function referrer(value: string) {
  Object.defineProperty(document, 'referrer', { value, configurable: true })
}

afterEach(() => {
  referrer('')
  history(1)
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

  it('goes back to the order when the payer came from this site\'s order page', async () => {
    referrer(`${window.location.origin}/o`)
    history(2)
    await approve()
    const { default: userEvent } = await import('@testing-library/user-event')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Back to your order' }))
    expect(navigate).toHaveBeenCalledWith(-1)
    expect(screen.queryByText(/Open your order from the link in your mail/)).not.toBeInTheDocument()
  })

  it('offers no way back when the pay link opened in a tab of its own', async () => {
    referrer(`${window.location.origin}/o`)
    history(1)
    await approve()
    expect(screen.queryByRole('button', { name: 'Back to your order' })).not.toBeInTheDocument()
    expect(screen.getByText(/Open your order from the link in your mail/)).toBeInTheDocument()
  })

  it.each(['https://elsewhere.example/o', `${window.location.origin}/draft`, `${window.location.origin}/order`])(
    'offers no way back from %s',
    async (from) => {
      referrer(from)
      await approve()
      expect(screen.queryByRole('button', { name: 'Back to your order' })).not.toBeInTheDocument()
      expect(screen.getByText(/Open your order from the link in your mail/)).toBeInTheDocument()
    },
  )

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
