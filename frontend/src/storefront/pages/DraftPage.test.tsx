import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { Draft } from '../api/client'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { DraftPage } from './DraftPage'

const HOSTILE = '<img src=x onerror=alert(1)>'

const DRAFT: Draft = {
  id: 'd1',
  currency: 'USD',
  minor_exponent: 2,
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
