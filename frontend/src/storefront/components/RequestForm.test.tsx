import { screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { RequestForm } from './RequestForm'

describe('RequestForm', () => {
  it('sends the same key again when a request is retried', async () => {
    const keys: (string | null)[] = []
    let answer = 0
    server.use(
      http.post('/api/store/v1/draft/requests', ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        answer += 1
        if (answer === 1) {
          return HttpResponse.json(
            { type: 'https://scadbuddy.dev/problems/storefront/uploads_busy', status: 503, detail: 'The shop is busy with other uploads. Try again in a minute.' },
            { status: 503 },
          )
        }
        return HttpResponse.json({ id: 'd1', currency: 'USD', minor_exponent: 2, items: [] })
      }),
    )
    const { user } = renderPage(<RequestForm />)
    await user.type(await screen.findByLabelText('What should we make?'), 'A hook for my bike')
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByText('The shop is busy with other uploads. Try again in a minute.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Send request' }))
    expect(await screen.findByText('Your request is in your order.')).toBeInTheDocument()
    expect(keys).toHaveLength(2)
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/)
    expect(keys[1]).toBe(keys[0])
  })

  it('refuses more files than the shop takes before sending', async () => {
    const { user } = renderPage(<RequestForm />)
    const files = Array.from({ length: 11 }, (_, i) => new File(['x'], `p${i}.png`, { type: 'image/png' }))
    await user.upload(await screen.findByLabelText('Photos, videos or PDFs'), files)
    expect(await screen.findByText('Add at most 10 files.')).toBeInTheDocument()
  })
})
