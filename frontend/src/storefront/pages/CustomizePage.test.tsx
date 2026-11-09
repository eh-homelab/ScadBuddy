import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { PreviewJob } from '../../components/Preview'
import { ESTIMATE, KEYCHAIN, TAG_ID } from '../../mocks/features/storefrontPublic'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import type { PollTiming } from '../poll'
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

/** Fast polls, so a test of the backoff takes milliseconds. */
const FAST: PollTiming = { firstMs: 5, maxMs: 20, limitMs: 2_000 }

function show(id = 'l1', timing: PollTiming = FAST) {
  return renderPage(<CustomizePage id={id} timing={timing} />)
}

const PROBLEMS = 'https://scadbuddy.dev/problems/storefront/'
const RUNNING = {
  id: 'a'.repeat(32),
  status: 'running',
  stage: null,
  plates: [],
  bbox_mm: null,
  colors: null,
  preview_url: null,
  error: null,
}
const DONE = {
  ...RUNNING,
  status: 'done',
  colors: ['#1B6CA8'],
  bbox_mm: { min: [0, 0, 0], max: [1, 1, 1], size: [1, 1, 1] },
  preview_url: '/api/store/v1/renders/x/preview.glb',
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

  it('keeps following a render through a limited or failed poll', async () => {
    let polls = 0
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(RUNNING, { status: 202 })),
      http.get('/api/store/v1/renders/:id', () => {
        polls += 1
        if (polls === 1) {
          return HttpResponse.json({ type: `${PROBLEMS}rate_limited`, status: 429, detail: 'Slow down.' }, { status: 429, headers: { 'Retry-After': '0' } })
        }
        if (polls === 2) return HttpResponse.error()
        if (polls === 3) return HttpResponse.json({ status: 503, detail: 'Busy.' }, { status: 503 })
        return HttpResponse.json(polls < 5 ? RUNNING : DONE)
      }),
      http.get('/api/store/v1/renders/:id/estimate', () => HttpResponse.json(ESTIMATE)),
    )
    const { user } = show()
    await user.click(await screen.findByRole('button', { name: 'Render' }))
    expect(await screen.findByText('about $12.40, confirmed after we check it')).toBeInTheDocument()
    expect(polls).toBe(5)
    expect(screen.queryByText(/a lot of/)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('backs off between polls, and stops with a plain message after the time limit', async () => {
    const at: number[] = []
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(RUNNING, { status: 202 })),
      http.get('/api/store/v1/renders/:id', () => {
        at.push(performance.now())
        return HttpResponse.json(RUNNING)
      }),
    )
    const { user } = show('l1', { firstMs: 20, maxMs: 80, limitMs: 600 })
    await user.click(await screen.findByRole('button', { name: 'Render' }))
    expect(await screen.findByText('This preview is taking longer than usual. Try Render again in a few minutes.')).toBeInTheDocument()
    const gaps = at.slice(1).map((t, i) => t - at[i]!)
    // Doubling from 20 ms to at most 80 ms: far fewer reads than one every 20 ms.
    expect(at.length).toBeLessThan(15)
    expect(Math.max(...gaps)).toBeGreaterThanOrEqual(70)
    expect(screen.getByRole('button', { name: 'Render' })).toBeEnabled()
  })

  it('stops reading a render once the page is gone', async () => {
    let polls = 0
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(RUNNING, { status: 202 })),
      http.get('/api/store/v1/renders/:id', () => {
        polls += 1
        return HttpResponse.json(RUNNING)
      }),
    )
    const { user, unmount } = show()
    await user.click(await screen.findByRole('button', { name: 'Render' }))
    await waitFor(() => expect(polls).toBeGreaterThan(0))
    unmount()
    const seen = polls
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(polls).toBeLessThanOrEqual(seen + 1)
  })

  it.each([
    ['render_queue_full', 'The shop is busy making other previews. Try again in 7 seconds.'],
    ['rate_limited', 'Your connection has sent a lot of requests. Try again in 7 seconds.'],
  ])('gives a %s its own words', async (slug, text) => {
    server.use(
      http.post('/api/store/v1/renders', () =>
        HttpResponse.json({ type: PROBLEMS + slug, status: 429, detail: 'x' }, { status: 429, headers: { 'Retry-After': '7' } }),
      ),
    )
    const { user } = show()
    await user.click(await screen.findByRole('button', { name: 'Render' }))
    expect(await screen.findByText(text)).toBeInTheDocument()
    expect(screen.queryByText(/a lot of previews/)).toBeNull()
  })

  it('loads the latest version of a changed listing and keeps the values it still offers', async () => {
    let republished = false
    server.use(
      http.post('/api/store/v1/renders', () =>
        republished
          ? HttpResponse.json(RUNNING, { status: 202 })
          : HttpResponse.json({ type: `${PROBLEMS}listing_changed`, status: 409, detail: 'This listing has changed. Load it again.' }, { status: 409 }),
      ),
      http.get('/api/store/v1/listings/l1', () => {
        if (!republished) return HttpResponse.json(KEYCHAIN)
        const { size: _size, ...params } = KEYCHAIN.params
        return HttpResponse.json({ ...KEYCHAIN, params, updated_at: '2026-11-08T09:00:00Z' })
      }),
    )
    const { user } = show()
    await user.type(await screen.findByLabelText('Name'), 'Ada')
    await user.type(screen.getByLabelText('Length (mm)'), '50')
    await user.click(screen.getByRole('button', { name: 'Render' }))
    expect(await screen.findByText('This listing has changed. Load it again.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add to order' })).toBeDisabled()
    republished = true
    await user.click(screen.getByRole('button', { name: 'Load the latest version' }))
    await waitFor(() => expect(screen.queryByLabelText('Length (mm)')).toBeNull())
    expect(screen.getByLabelText('Name')).toHaveValue('Ada')
    expect(screen.getByRole('button', { name: 'Render' })).toBeEnabled()
    expect(screen.queryByText('This listing has changed. Load it again.')).toBeNull()
  })

  it('marks a preview of earlier choices', async () => {
    const { user } = show()
    await user.click(await screen.findByRole('button', { name: 'Render' }))
    await screen.findByText('about $12.40, confirmed after we check it')
    expect(screen.queryByText('Preview shows earlier choices. Render again to see your changes.')).toBeNull()
    await user.type(screen.getByLabelText('Name'), 'Bo')
    expect(screen.getByText('Preview shows earlier choices. Render again to see your changes.')).toBeInTheDocument()
    expect(screen.queryByText('about $12.40, confirmed after we check it')).toBeNull()
  })

  it('reads a hex number as no number', async () => {
    const { user } = show()
    await user.type(await screen.findByLabelText('Length (mm)'), '0x10')
    expect(await screen.findByText('Choose from 40 to 80, in steps of 5.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Render' })).toBeDisabled()
  })

  it('offers the text keyboard where a number may be negative', async () => {
    server.use(
      http.get('/api/store/v1/listings/l1', () =>
        HttpResponse.json({ ...KEYCHAIN, params: { tilt: { kind: 'number', label: 'Tilt', group: '', colour: false, min: -10, max: 10, step: null } } }),
      ),
    )
    show()
    expect(await screen.findByLabelText('Tilt')).toHaveAttribute('inputmode', 'text')
  })
})
