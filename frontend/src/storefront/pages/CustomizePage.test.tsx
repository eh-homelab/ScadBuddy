import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
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

function show(id = 'l1', timing: PollTiming = FAST, debounceMs = 10) {
  return renderPage(<CustomizePage id={id} timing={timing} debounceMs={debounceMs} />)
}

/** Every render the page asked for, by its body. */
function renders(): unknown[] {
  const sent: unknown[] = []
  server.events.on('request:start', async ({ request }) => {
    if (request.method === 'POST' && new URL(request.url).pathname === '/api/store/v1/renders') sent.push(await request.clone().json())
  })
  return sent
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
  beforeEach(() => {
    window.localStorage.clear()
    return () => server.events.removeAllListeners()
  })

  it('renders on load, and shows the rough price as an estimate', async () => {
    const sent = renders()
    show()
    expect(await screen.findByText('about $12.40, confirmed after we check it')).toBeInTheDocument()
    expect(screen.getByTestId('preview')).toHaveAttribute('data-status', 'done')
    expect(sent).toEqual([{ listing_id: 'l1', listing_updated_at: '2026-11-07T12:00:00Z', values: {}, colours: {} }])
    expect(screen.queryByRole('button', { name: 'Render' })).toBeNull()
  })

  it('tells the visitor when to try again', async () => {
    server.use(http.post('/api/store/v1/renders', () => HttpResponse.json(LIMITED, { status: 429, headers: { 'Retry-After': '42' } })))
    show()
    expect(await screen.findByText('You have made a lot of previews. Try again in 42 seconds.')).toBeInTheDocument()
  })

  it('renders once after a burst of changes, sending only what the visitor changed, with an Idempotency-Key', async () => {
    const sent: { body: unknown; key: string | null }[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname === '/api/store/v1/renders') {
        sent.push({ body: await request.clone().json(), key: request.headers.get('Idempotency-Key') })
      }
    })
    const { user } = show('l1', FAST, 300)
    await screen.findByText('about $12.40, confirmed after we check it')
    await user.type(screen.getByLabelText('Name'), 'Ada')
    await waitFor(() => expect(sent).toHaveLength(2))
    await screen.findByText('about $12.40, confirmed after we check it')
    expect(sent).toHaveLength(2)
    expect(sent[1]!.body).toEqual({
      listing_id: 'l1',
      listing_updated_at: '2026-11-07T12:00:00Z',
      values: { name: 'Ada' },
      colours: {},
    })
    expect(sent[1]!.key).toMatch(/^[0-9a-f-]{36}$/)
    expect(sent[1]!.key).not.toBe(sent[0]!.key)
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
    void show()
    expect(await screen.findByText('Previews are paused. Try again later.')).toBeInTheDocument()
  })

  it('holds a number outside the listing back', async () => {
    const { user } = show()
    const size = await screen.findByLabelText('Length (mm)')
    await user.type(size, '200')
    expect(await screen.findByText('Choose from 40 to 80, in steps of 5.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add to order' })).toBeDisabled())
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
    void show()
    expect(await screen.findByText('about $12.40, confirmed after we check it')).toBeInTheDocument()
    expect(polls).toBe(5)
    expect(screen.queryByText(/a lot of/)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('backs off between polls, and stops with a plain message after the time limit', async () => {
    // The page's clock moves only when it waits, so a slow host cannot change what it
    // asks for (#5902): a poll that took longer than the limit once left no gaps to measure.
    let clock = 0
    const asked: number[] = []
    const wait = async (ms: number) => {
      asked.push(ms)
      clock += ms
    }
    let polls = 0
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(RUNNING, { status: 202 })),
      http.get('/api/store/v1/renders/:id', () => {
        polls += 1
        return HttpResponse.json(RUNNING)
      }),
    )
    void renderPage(
      <CustomizePage id="l1" timing={{ firstMs: 20, maxMs: 80, limitMs: 600 }} wait={wait} now={() => clock} />,
    )
    expect(await screen.findByText('This preview is taking longer than usual. Change something, or come back in a few minutes.')).toBeInTheDocument()
    // Doubling from 20 ms to at most 80 ms, then only the 60 ms left before the limit.
    expect(asked).toEqual([20, 40, 80, 80, 80, 80, 80, 80, 60])
    expect(polls).toBe(8)
  })

  it('stops at the time limit even when a poll is told to wait far longer', async () => {
    let polls = 0
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(RUNNING, { status: 202 })),
      http.get('/api/store/v1/renders/:id', () => {
        polls += 1
        return HttpResponse.json({ type: `${PROBLEMS}rate_limited`, status: 429, detail: 'Slow down.' }, { status: 429, headers: { 'Retry-After': '99999' } })
      }),
    )
    const started = performance.now()
    void show('l1', { firstMs: 20, maxMs: 40, limitMs: 400 })
    expect(await screen.findByText('This preview is taking longer than usual. Change something, or come back in a few minutes.')).toBeInTheDocument()
    expect(performance.now() - started).toBeLessThan(2_500)
    expect(polls).toBe(1)
  })

  it('reads no more once the wait reaches the time limit, even when the timer fires before the clock does', async () => {
    // A timer can fire a millisecond or two before Date.now() shows its delay has passed
    // (Node: 5 of 200 timers, measured for #5875). Only the page's own waits fire 5 ms
    // early here; testing-library, msw and user-event keep the real timers (#5902).
    const early = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, Math.max(0, ms - 5)))
    let polls = 0
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(RUNNING, { status: 202 })),
      http.get('/api/store/v1/renders/:id', () => {
        polls += 1
        return HttpResponse.json({ type: `${PROBLEMS}rate_limited`, status: 429, detail: 'Slow down.' }, { status: 429, headers: { 'Retry-After': '99999' } })
      }),
    )
    void renderPage(<CustomizePage id="l1" timing={{ firstMs: 20, maxMs: 40, limitMs: 400 }} wait={early} />)
    expect(await screen.findByText('This preview is taking longer than usual. Change something, or come back in a few minutes.')).toBeInTheDocument()
    expect(polls).toBe(1)
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
    const { unmount } = show()
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
    show()
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
    expect(await screen.findByText('This listing has changed. Load it again.')).toBeInTheDocument()
    await user.type(screen.getByLabelText('Name'), 'Ada')
    await user.type(screen.getByLabelText('Length (mm)'), '50')
    expect(screen.getByRole('button', { name: 'Add to order' })).toBeDisabled()
    republished = true
    await user.click(screen.getByRole('button', { name: 'Load the latest version' }))
    await waitFor(() => expect(screen.queryByLabelText('Length (mm)')).toBeNull())
    expect(screen.getByLabelText('Name')).toHaveValue('Ada')
    expect(screen.queryByText('This listing has changed. Load it again.')).toBeNull()
    // The latest version renders on its own.
    await waitFor(() => expect(screen.getByTestId('preview')).toHaveAttribute('data-status', 'running'))
  })

  it('keeps the earlier preview on screen and hides its price while a change renders', async () => {
    let posts = 0
    server.use(
      http.post('/api/store/v1/renders', () => {
        posts += 1
        return posts === 1 ? HttpResponse.json(DONE) : HttpResponse.json(RUNNING, { status: 202 })
      }),
      http.get('/api/store/v1/renders/:id', () => HttpResponse.json(RUNNING)),
      http.get('/api/store/v1/renders/:id/estimate', () => HttpResponse.json(ESTIMATE)),
    )
    const { user } = show('l1', { firstMs: 5, maxMs: 20, limitMs: 60_000 })
    await screen.findByText('about $12.40, confirmed after we check it')
    await user.type(screen.getByLabelText('Name'), 'Bo')
    expect(screen.queryByText('about $12.40, confirmed after we check it')).toBeNull()
    expect(await screen.findByText('Updating your preview and price…')).toBeInTheDocument()
    expect(screen.getByTestId('preview')).toHaveAttribute('data-status', 'done')
  })

  it('reads the price again after the proxy times out', async () => {
    let reads = 0
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(DONE)),
      http.get('/api/store/v1/renders/:id/estimate', () => {
        reads += 1
        return reads < 3 ? new HttpResponse('Gateway Timeout', { status: 504 }) : HttpResponse.json(ESTIMATE)
      }),
    )
    show()
    expect(await screen.findByText('about $12.40, confirmed after we check it')).toBeInTheDocument()
    expect(reads).toBe(3)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('tries a limited render once more after Retry-After', async () => {
    let posts = 0
    server.use(
      http.post('/api/store/v1/renders', () => {
        posts += 1
        return posts === 1 ? HttpResponse.json(LIMITED, { status: 429, headers: { 'Retry-After': '0' } }) : HttpResponse.json(DONE)
      }),
      http.get('/api/store/v1/renders/:id/estimate', () => HttpResponse.json(ESTIMATE)),
    )
    show()
    expect(await screen.findByText('about $12.40, confirmed after we check it')).toBeInTheDocument()
    expect(posts).toBe(2)
  })

  it('says no preview is coming after a refused first render, and lets the visitor try again', async () => {
    let posts = 0
    server.use(
      http.post('/api/store/v1/renders', () => {
        posts += 1
        return posts === 1
          ? HttpResponse.json({ type: `${PROBLEMS}render_unavailable`, status: 503, detail: 'Previews are paused. Try again later.' }, { status: 503 })
          : HttpResponse.json(DONE)
      }),
      http.get('/api/store/v1/renders/:id/estimate', () => HttpResponse.json(ESTIMATE)),
    )
    const { user } = show()
    expect(await screen.findByText('Previews are paused. Try again later.')).toBeInTheDocument()
    expect(screen.getByText('No preview yet.')).toBeInTheDocument()
    expect(screen.queryByText('Making your preview…')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('about $12.40, confirmed after we check it')).toBeInTheDocument()
    expect(posts).toBe(2)
  })

  it('drops the delayed retry once the visitor has changed the choices to ones the page refuses', async () => {
    let posts = 0
    server.use(
      http.post('/api/store/v1/renders', () => {
        posts += 1
        return HttpResponse.json(LIMITED, { status: 429, headers: { 'Retry-After': '1' } })
      }),
    )
    const { user } = show()
    expect(await screen.findByText('You have made a lot of previews. Try again in 1 second.')).toBeInTheDocument()
    await user.type(screen.getByLabelText('Length (mm)'), '200')
    await new Promise((resolve) => setTimeout(resolve, 1_300))
    expect(posts).toBe(1)
  })

  it('keeps the visitor’s choices when they leave and come back', async () => {
    const first = show()
    await first.user.type(await screen.findByLabelText('Name'), 'Ada')
    await first.user.click(screen.getByRole('button', { name: 'One more' }))
    first.unmount()
    const sent = renders()
    show()
    expect(await screen.findByLabelText('Name')).toHaveValue('Ada')
    expect(screen.getByText('2')).toBeInTheDocument()
    await waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0]).toMatchObject({ values: { name: 'Ada' } })
  })

  it('drops a kept value the listing no longer allows', async () => {
    window.localStorage.setItem('scadbuddy.storefront.customize.l1', JSON.stringify({ v: 1, values: { name: 'Ada', size: 999, gone: 1 }, quantity: 3 }))
    const sent = renders()
    show()
    expect(await screen.findByLabelText('Name')).toHaveValue('Ada')
    expect(screen.getByLabelText('Length (mm)')).toHaveValue('')
    await waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0]).toMatchObject({ values: { name: 'Ada' } })
  })

  it('forgets the choices once the design is in the order', async () => {
    const { user } = show()
    await user.type(await screen.findByLabelText('Name'), 'Ada')
    await user.click(screen.getByRole('button', { name: 'Add to order' }))
    await screen.findByText('Added to your order.')
    expect(window.localStorage.getItem('scadbuddy.storefront.customize.l1')).toBeNull()
  })

  it('reads a hex number as no number', async () => {
    const { user } = show()
    await user.type(await screen.findByLabelText('Length (mm)'), '0x10')
    expect(await screen.findByText('Choose from 40 to 80, in steps of 5.')).toBeInTheDocument()
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
