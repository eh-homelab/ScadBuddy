import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { PreviewJob } from '../../components/Preview'
import { TAG_ID } from '../../mocks/features/storefrontPublic'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { CustomizePage } from './CustomizePage'

// WebGL does not exist in jsdom: the viewer is a stand-in. The e2e run draws the real one.
vi.mock('../../components/Preview', () => ({
  Preview: ({ job }: { job?: PreviewJob }) => <div data-testid="preview" data-status={job?.status ?? 'none'} />,
}))

const LIMITED = {
  type: 'https://scadbuddy.dev/problems/storefront/render_limited',
  title: 'Too Many Requests',
  status: 429,
  detail: 'Too many renders.',
}

function show(id = 'l1') {
  return renderPage(<CustomizePage id={id} />)
}

describe('CustomizePage', () => {
  it('shows the rough price as an estimate', async () => {
    const { user } = show()
    await user.click(await screen.findByRole('button', { name: 'Render' }))
    expect(await screen.findByText('about $12.40, confirmed after we check it')).toBeInTheDocument()
  })

  it('tells the visitor when to try again', async () => {
    server.use(http.post('/api/store/v1/renders', () => HttpResponse.json(LIMITED, { status: 429, headers: { 'Retry-After': '42' } })))
    const { user } = show()
    await user.click(await screen.findByRole('button', { name: 'Render' }))
    expect(await screen.findByText('You have made a lot of previews. Try again in 42 seconds.')).toBeInTheDocument()
  })

  it('sends only what the visitor changed, with an Idempotency-Key', async () => {
    const sent: { body: unknown; key: string | null }[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname === '/api/store/v1/renders') {
        sent.push({ body: await request.clone().json(), key: request.headers.get('Idempotency-Key') })
      }
    })
    const { user } = show()
    await user.type(await screen.findByLabelText('Name'), 'Ada')
    await user.click(screen.getByRole('button', { name: 'Render' }))
    await screen.findByText('about $12.40, confirmed after we check it')
    server.events.removeAllListeners()
    expect(sent).toHaveLength(1)
    expect(sent[0]!.body).toEqual({
      listing_id: 'l1',
      listing_updated_at: '2026-11-07T12:00:00Z',
      values: { name: 'Ada' },
      colours: {},
    })
    expect(sent[0]!.key).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('sends a picked colour, and nothing once it is picked again', async () => {
    const sent: unknown[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname === '/api/store/v1/draft/items') {
        sent.push(((await request.clone().json()) as { values: unknown }).values)
      }
    })
    const { user } = show()
    const orange = await screen.findByRole('radio', { name: 'Signal orange PLA' })
    await user.click(orange)
    await user.click(screen.getByRole('button', { name: 'Add to order' }))
    await screen.findByText('Added to your order.')
    await user.click(orange)
    await user.click(screen.getByRole('button', { name: 'One more' }))
    await user.click(screen.getByRole('button', { name: 'Add to order' }))
    await waitFor(() => expect(sent).toHaveLength(2))
    server.events.removeAllListeners()
    expect(sent).toEqual([{ base_colour: '#E8532F' }, {}])
  })

  it('adds the design to the order with the render colours', async () => {
    const added: unknown[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname === '/api/store/v1/draft/items') {
        added.push(await request.clone().json())
      }
    })
    const { user } = show()
    await user.click(await screen.findByRole('button', { name: 'Render' }))
    await screen.findByText('about $12.40, confirmed after we check it')
    await user.click(screen.getByRole('button', { name: 'Add to order' }))
    expect(await screen.findByText('Added to your order.')).toBeInTheDocument()
    server.events.removeAllListeners()
    expect(added).toEqual([
      {
        listing_id: 'l1',
        listing_updated_at: '2026-11-07T12:00:00Z',
        values: {},
        colours: { '0': 'f-blue', '1': 'f-orange' },
        quantity: 1,
      },
    ])
  })

  it('refuses an order of a colour no filament is left for', async () => {
    show(TAG_ID)
    expect(await screen.findByText('No colour is available for this right now.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add to order' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Render' })).toBeDisabled()
  })

  it('says plainly why the shop refused', async () => {
    server.use(
      http.post('/api/store/v1/renders', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/storefront/render_unavailable', status: 503, detail: 'Previews are paused. Try again later.' },
          { status: 503 },
        ),
      ),
    )
    const { user } = show()
    await user.click(await screen.findByRole('button', { name: 'Render' }))
    expect(await screen.findByText('Previews are paused. Try again later.')).toBeInTheDocument()
  })

  it('holds a number outside the listing back', async () => {
    const { user } = show()
    const size = await screen.findByLabelText('Length (mm)')
    await user.type(size, '200')
    expect(await screen.findByText('Choose from 40 to 80, in steps of 5.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Render' })).toBeDisabled())
  })
})
