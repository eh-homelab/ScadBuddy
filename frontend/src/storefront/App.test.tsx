import { screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { App } from './App'

describe('the storefront App', () => {
  it('names the cart link with its count', async () => {
    const item = { kind: 'request', title: 'Design request', quantity: 1, inputs: null, labels: {}, notes: 'x', attachments: [] }
    server.use(
      http.get('/api/store/v1/draft', () =>
        HttpResponse.json({ id: 'd', currency: 'USD', minor_exponent: 2, items: [1, 2, 3].map((n) => ({ ...item, id: `i${n}` })) }),
      ),
    )
    renderPage(<App />)
    expect(await screen.findByRole('link', { name: 'Your order, 3 items' })).toHaveAttribute('href', '/draft')
  })

  it('names an empty cart plainly', async () => {
    renderPage(<App />)
    expect(await screen.findByRole('link', { name: 'Your order' })).toBeInTheDocument()
  })
})
