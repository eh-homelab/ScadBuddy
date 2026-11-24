import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PreviewJob } from '../../components/Preview'
import { CHARM_ID, ESTIMATE, FILE_ID, KEYCHAIN, NO_PRICE, PENDING_PRICE, TAG_ID } from '../../mocks/features/storefrontPublic'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import type { PollTiming } from '../poll'
import { CustomizePage } from './CustomizePage'

// WebGL does not exist in jsdom: the viewer is a stand-in. The e2e run draws the real one.
vi.mock('../../components/Preview', () => ({
  Preview: ({ job, slotColours, leading }: { job?: PreviewJob; slotColours?: Record<number, string>; leading?: React.ReactNode }) => (
    <div
      data-testid="preview"
      data-status={job?.status ?? 'none'}
      data-url={job?.preview_url ?? ''}
      data-slots={slotColours ? JSON.stringify(slotColours) : ''}
    >
      {leading}
    </div>
  ),
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

  it('renders on load, and shows the slice price as firm (#6153)', async () => {
    const sent = renders()
    show()
    expect(await screen.findByText('$12.40 for one')).toBeInTheDocument()
    // The first test of the file loads the lazy viewer's chunk: under load it can come after the price.
    expect(await screen.findByTestId('preview')).toHaveAttribute('data-status', 'done')
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
    await screen.findByText('$12.40 for one')
    await user.type(screen.getByLabelText('Name'), 'Ada')
    await waitFor(() => expect(sent).toHaveLength(2))
    await screen.findByText('$12.40 for one')
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
    const picker = await screen.findByRole('button', { name: /Base colour/ })
    await user.click(picker)
    await user.click(await screen.findByRole('radio', { name: 'Signal orange' }))
    // A colour set waits for its preview: the shop fills the render's slots from it.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add to order' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Add to order' }))
    await screen.findByText('Added to your order.')
    await user.click(picker)
    await user.click(await screen.findByRole('button', { name: 'Use the shop’s choice' }))
    await user.click(screen.getByRole('button', { name: 'One more' }))
    await user.click(screen.getByRole('button', { name: 'Add to order' }))
    await waitFor(() => expect(sent).toHaveLength(2))
    server.events.removeAllListeners()
    expect(sent).toEqual([{ base_colour: '#E8532F' }, {}])
  })

  it('shows no Material control, and sends only the colours the visitor sets (#6258)', async () => {
    const sent = renders()
    const { user } = show(CHARM_ID)
    await waitFor(() => expect(sent).toHaveLength(1))
    expect(screen.queryByRole('radiogroup', { name: 'Material' })).toBeNull()
    expect(sent[0]).toEqual(expect.objectContaining({ values: {} }))
    await user.click(screen.getByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Rust' }))
    await waitFor(() => expect(sent.at(-1)).toEqual(expect.objectContaining({ values: { body_colour: '#C0502A' } })))
    server.events.removeAllListeners()
    expect(sent.every((body) => !('picks' in (body as object)))).toBe(true)
  })

  it('moves an untouched part at once, then shows the colour the shop settled for it (#6258)', async () => {
    // The rim's default blue is PLA only. The visitor picks a rust that is PETG only, so the
    // print is PETG. The page predicts the rim's closest PETG colour, teal, at once. The
    // shop's reply settles it to rust here, and the reply wins.
    let hold: (() => void) | undefined
    server.use(
      http.post('/api/store/v1/renders', async ({ request }) => {
        const body = (await request.json()) as { values: Record<string, string> }
        if (body.values.body_colour === undefined) return HttpResponse.json({ ...RUNNING, values: { body_colour: '#1B6CA8', rim_colour: '#1B6CA8' } }, { status: 202 })
        await new Promise<void>((resolve) => { hold = resolve })
        return HttpResponse.json({ ...RUNNING, values: { body_colour: body.values.body_colour, rim_colour: '#C0502A' } }, { status: 202 })
      }),
      http.get('/api/store/v1/renders/:id', () => HttpResponse.json(RUNNING)),
    )
    const { user } = show(CHARM_ID)
    expect(await screen.findByRole('button', { name: /Rim colour Ocean blue/ })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Rust' }))
    expect(screen.getByRole('button', { name: /Rim colour Teal/ })).toBeInTheDocument()
    await waitFor(() => expect(hold).toBeDefined())
    hold?.()
    expect(await screen.findByRole('button', { name: /Rim colour Rust/ })).toBeInTheDocument()
  })

  it('shows its own prediction, not the last reply, while the start of changed choices has not answered (#6302 review)', async () => {
    // The first pick's reply settles the rim to rust. The next pick's start does not answer
    // yet, so the rim shows the page's prediction (teal), not the last reply's rust.
    let hold: (() => void) | undefined
    server.use(
      http.post('/api/store/v1/renders', async ({ request }) => {
        const body = (await request.json()) as { values: Record<string, string> }
        if (body.values.body_colour === '#C0502A') return HttpResponse.json({ ...RUNNING, values: { body_colour: '#C0502A', rim_colour: '#C0502A' } }, { status: 202 })
        if (body.values.body_colour === undefined) return HttpResponse.json({ ...RUNNING, values: { body_colour: '#1B6CA8', rim_colour: '#1B6CA8' } }, { status: 202 })
        await new Promise<void>((resolve) => { hold = resolve })
        return HttpResponse.json({ ...RUNNING, values: { body_colour: body.values.body_colour, rim_colour: '#C0502A' } }, { status: 202 })
      }),
      http.get('/api/store/v1/renders/:id', () => HttpResponse.json(RUNNING)),
    )
    const { user } = show(CHARM_ID)
    await user.click(await screen.findByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Rust' }))
    expect(await screen.findByRole('button', { name: /Rim colour Rust/ })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Teal' }))
    expect(screen.getByRole('button', { name: /Rim colour Teal/ })).toBeInTheDocument()
    await waitFor(() => expect(hold).toBeDefined())
    expect(screen.getByRole('button', { name: /Rim colour Teal/ })).toBeInTheDocument()
    hold?.()
    expect(await screen.findByRole('button', { name: /Rim colour Rust/ })).toBeInTheDocument()
  })

  it('shows its own prediction again once a render start is refused (#6302 review)', async () => {
    server.use(
      http.post('/api/store/v1/renders', async ({ request }) => {
        const body = (await request.json()) as { values: Record<string, string> }
        if (body.values.body_colour === '#C0502A') return HttpResponse.json({ ...RUNNING, values: { body_colour: '#C0502A', rim_colour: '#C0502A' } }, { status: 202 })
        if (body.values.body_colour === undefined) return HttpResponse.json({ ...RUNNING, values: { body_colour: '#1B6CA8', rim_colour: '#1B6CA8' } }, { status: 202 })
        return HttpResponse.json(
          { type: PROBLEMS + 'listing_colours_clash', title: 'Unprocessable Content', status: 422, detail: 'No.', parts: ['body_colour'] },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
      http.get('/api/store/v1/renders/:id', () => HttpResponse.json(RUNNING)),
    )
    const { user } = show(CHARM_ID)
    await user.click(await screen.findByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Rust' }))
    expect(await screen.findByRole('button', { name: /Rim colour Rust/ })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Teal' }))
    await screen.findByText(/can't go/)
    expect(screen.getByRole('button', { name: /Rim colour Teal/ })).toBeInTheDocument()
  })

  it('says plainly which picks clash when they need two materials, and moves neither (#6258)', async () => {
    const { user } = show(CHARM_ID)
    await user.click(await screen.findByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Signal orange' }))
    await user.click(screen.getByRole('button', { name: /Rim colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Teal' }))
    expect(
      await screen.findByText(
        "The body colour and the rim colour can't go together. Each print is made from one kind of plastic, and a few colours only come in one kind. Change one of them.",
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Body colour/ })).toHaveAccessibleName('Body colour Signal orange')
    expect(screen.getByRole('button', { name: /Rim colour/ })).toHaveAccessibleName('Rim colour Teal')
  })

  it('never starts the same choices again when the page re-runs its render effect (clash flake)', async () => {
    // The effect also runs when the preview on screen changes. An older render that showed
    // its preview after the newer one started ran it again: the second start cleared the
    // refusal on screen, then showed it again.
    const sent = renders()
    const { user, rerender } = show(CHARM_ID)
    await user.click(await screen.findByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Signal orange' }))
    await user.click(screen.getByRole('button', { name: /Rim colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Teal' }))
    const refusal = await screen.findByRole('alert')
    const starts = () => sent.filter((body) => Object.keys((body as { values: object }).values).length === 2).length
    expect(starts()).toBe(1)
    rerender(<CustomizePage id={CHARM_ID} timing={FAST} debounceMs={11} />)
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(starts()).toBe(1)
    expect(refusal).toBeInTheDocument()
  })

  it('keeps a running render of choices the visitor changed away from and back to (#7007 review)', async () => {
    server.use(http.get('/api/store/v1/renders/:id', () => HttpResponse.json(RUNNING)))
    const sent = renders()
    // A long time limit: a render past it ends as too slow and would start again.
    const { user } = show(CHARM_ID, { ...FAST, limitMs: 60_000 }, 300)
    await user.click(await screen.findByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Rust' }))
    await waitFor(() => expect(sent).toHaveLength(2))
    // Away and back before the debounce ends, while the render of these choices still runs.
    await user.click(screen.getByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Teal' }))
    await user.click(screen.getByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Rust' }))
    await new Promise((resolve) => setTimeout(resolve, 400))
    expect(sent).toHaveLength(2)
  })

  it('renders choices again after a change and back once their render ended as too slow (#7010 review)', async () => {
    server.use(http.get('/api/store/v1/renders/:id', () => HttpResponse.json(RUNNING)))
    const sent = renders()
    // The two one-second debounces take about 2.5 s; the 15 s timeout leaves room on a loaded machine.
    const { user } = show(CHARM_ID, { ...FAST, limitMs: 100 }, 1_000)
    await screen.findByRole('alert')
    await user.click(await screen.findByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Rust' }))
    await waitFor(() => expect(sent).toHaveLength(2), { timeout: 2_000 })
    // Away while the render of Rust runs; it ends as too slow before the visitor comes back.
    await user.click(screen.getByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Teal' }))
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument(), { timeout: 2_000 })
    await user.click(screen.getByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Rust' }))
    await waitFor(() => expect(sent).toHaveLength(3), { timeout: 2_000 })
    expect(sent[2]).toEqual(sent[1])
  }, 15_000)

  it('renders refused choices again after a change and back inside the debounce (#7000 review)', async () => {
    const sent = renders()
    const { user } = show(CHARM_ID, FAST, 300)
    await user.click(await screen.findByRole('button', { name: /Body colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Signal orange' }))
    await user.click(screen.getByRole('button', { name: /Rim colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Teal' }))
    await screen.findByRole('alert')
    const rims = (colour: string) => sent.filter((body) => (body as { values: Record<string, string> }).values.rim_colour === colour).length
    const teal = sent.find((body) => Object.keys((body as { values: object }).values).length === 2) as { values: { rim_colour: string } }
    expect(rims(teal.values.rim_colour)).toBe(1)
    // Away and back before the debounce ends: the change away never renders.
    await user.click(screen.getByRole('button', { name: /Rim colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Rust' }))
    await user.click(screen.getByRole('button', { name: /Rim colour/ }))
    await user.click(await screen.findByRole('radio', { name: 'Teal' }))
    await waitFor(() => expect(rims(teal.values.rim_colour)).toBe(2))
    expect(sent).toHaveLength(3)
  })

  it('says the preview must show before an add, with a retry, when the preview fails (#6152 review)', async () => {
    // A colour the visitor set (saved), so the first render is the one that fails.
    window.localStorage.setItem('scadbuddy.storefront.customize.l1', JSON.stringify({ v: 1, values: { base_colour: '#E8532F' }, quantity: 1 }))
    server.use(
      http.post('/api/store/v1/renders', () =>
        HttpResponse.json({ type: `${PROBLEMS}render_queue_full`, status: 503, detail: 'busy' }, { status: 503, headers: { 'Retry-After': '120' } }),
      ),
    )
    show()
    expect(await screen.findByText('You can add it once its preview shows. Its preview could not be made: try again.')).toBeInTheDocument()
    expect(screen.queryByText('You can add it once its preview shows.')).toBeNull()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add to order' })).toBeDisabled()
  })

  it('holds “Add to order” back while the preview of the colours set is made (#6152)', async () => {
    window.localStorage.setItem('scadbuddy.storefront.customize.l1', JSON.stringify({ v: 1, values: { base_colour: '#E8532F' }, quantity: 1 }))
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(RUNNING, { status: 202 })),
      http.get('/api/store/v1/renders/:id', () => HttpResponse.json(RUNNING)),
    )
    show('l1', { firstMs: 5, maxMs: 20, limitMs: 60_000 })
    expect(await screen.findByText('You can add it once its preview shows.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add to order' })).toBeDisabled()
  })

  it('reads past the picks and material of choices saved before #6258', async () => {
    const sent = renders()
    window.localStorage.setItem(
      'scadbuddy.storefront.customize.l1',
      JSON.stringify({ v: 1, values: { base_colour: '#E8532F' }, quantity: 2, picks: { base_colour: 'f-orange' }, material: 'PLA' }),
    )
    show()
    await screen.findByText('$12.40 for one')
    server.events.removeAllListeners()
    expect(sent[0]).toEqual({ listing_id: 'l1', listing_updated_at: '2026-11-07T12:00:00Z', values: { base_colour: '#E8532F' }, colours: {} })
    expect(screen.getByText('2')).toBeInTheDocument()
  })

  it('adds the design to the order with its values, never a guess of the slots', async () => {
    const added: unknown[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname === '/api/store/v1/draft/items') {
        added.push(await request.clone().json())
      }
    })
    const { user } = show()
    await screen.findByText('$12.40 for one')
    await user.click(screen.getByRole('button', { name: 'Add to order' }))
    expect(await screen.findByText('Added to your order.')).toBeInTheDocument()
    server.events.removeAllListeners()
    expect(added).toEqual([
      {
        listing_id: 'l1',
        listing_updated_at: '2026-11-07T12:00:00Z',
        values: {},
        colours: {},
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
    expect(await screen.findByText('$12.40 for one')).toBeInTheDocument()
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

  it('keeps the earlier preview and its price on screen, marked as updating, while a change renders (#6921)', async () => {
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
    await screen.findByText('$12.40 for one')
    await user.type(screen.getByLabelText('Name'), 'Bo')
    expect(await screen.findByText('Updating your preview and price…')).toBeInTheDocument()
    // The owner's rule: always show a price. The old one stays, marked as updating.
    expect(screen.getByText('$12.40 for one')).toBeInTheDocument()
    expect(screen.getByText('Updating this price…')).toBeInTheDocument()
    expect(screen.getByTestId('preview')).toHaveAttribute('data-status', 'done')
  })

  it('drops the earlier price when the render of a change is refused, and says nothing is updating (#6921)', async () => {
    let posts = 0
    server.use(
      http.post('/api/store/v1/renders', () => {
        posts += 1
        // The load renders; the change is refused.
        return posts === 1 ? undefined : HttpResponse.json(LIMITED, { status: 429, headers: { 'Retry-After': '42' } })
      }),
    )
    const { user } = show()
    await screen.findByText('$12.40 for one')
    await user.type(screen.getByLabelText('Name'), 'Bo')
    expect(await screen.findByText('You have made a lot of previews. Try again in 42 seconds.')).toBeInTheDocument()
    expect(screen.queryByText('Updating this price…')).toBeNull()
    expect(screen.queryByText('$12.40 for one')).toBeNull()
  })

  it('drops the earlier price when the render of a change fails (#6921)', async () => {
    let posts = 0
    server.use(
      http.post('/api/store/v1/renders', () => {
        posts += 1
        return posts === 1 ? undefined : HttpResponse.json({ ...DONE, status: 'failed', error: 'It broke.' })
      }),
    )
    const { user } = show()
    await screen.findByText('$12.40 for one')
    await user.type(screen.getByLabelText('Name'), 'Bo')
    expect(await screen.findByText('It broke.')).toBeInTheDocument()
    expect(screen.queryByText('Updating this price…')).toBeNull()
    expect(screen.queryByText('$12.40 for one')).toBeNull()
  })

  it('shows a cached preview and its firm price before any render, and renders nothing (#6921)', async () => {
    const sent = renders()
    server.use(
      http.post('/api/store/v1/listings/:id/variant', () =>
        HttpResponse.json({
          price: ESTIMATE,
          preview: { url: '/api/store/v1/variants/k/preview.glb', colors: ['#1B6CA8'], bbox_mm: DONE.bbox_mm, plates: [] },
          render: null,
          values: null,
        }),
      ),
    )
    show()
    expect(await screen.findByText('$12.40 for one')).toBeInTheDocument()
    expect(screen.getByTestId('preview')).toHaveAttribute('data-status', 'done')
    expect(screen.queryByText('Updating this price…')).toBeNull()
    expect(screen.queryByText('Making your preview and price…')).toBeNull()
    // The add needs no render of the visitor's own: the shop takes the variant it priced.
    expect(screen.getByRole('button', { name: 'Add to order' })).toBeEnabled()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(sent).toEqual([])
  })

  it('shows the last price marked as updating, never as firm, until the render is priced (#6921)', async () => {
    let release = () => {}
    const sliced = new Promise<void>((resolve) => {
      release = resolve
    })
    const sent = renders()
    server.use(
      http.post('/api/store/v1/listings/:id/variant', () =>
        HttpResponse.json({ price: { ...ESTIMATE, source: 'last', price_minor: 990 }, preview: null, render: null, values: null }),
      ),
      http.post('/api/store/v1/renders', () => HttpResponse.json(DONE)),
      http.get('/api/store/v1/renders/:id/estimate', async () => {
        await sliced
        return HttpResponse.json(ESTIMATE)
      }),
    )
    show()
    // Before the render answers, the last price shows, marked.
    expect(await screen.findByText('$9.90 for one')).toBeInTheDocument()
    expect(screen.getByText('Updating this price…')).toBeInTheDocument()
    await waitFor(() => expect(sent).toHaveLength(1))
    release()
    expect(await screen.findByText('$12.40 for one')).toBeInTheDocument()
    expect(screen.queryByText('Updating this price…')).toBeNull()
    expect(screen.queryByText('$9.90 for one')).toBeNull()
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
    expect(await screen.findByText('$12.40 for one')).toBeInTheDocument()
    expect(reads).toBe(3)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('waits for a done render to name its preview, and for a price that is not ready', async () => {
    let polls = 0
    let reads = 0
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json({ ...DONE, preview_url: null })),
      http.get('/api/store/v1/renders/:id', () => {
        polls += 1
        return HttpResponse.json(polls < 2 ? { ...DONE, preview_url: null } : DONE)
      }),
      http.get('/api/store/v1/renders/:id/estimate', () => {
        reads += 1
        return HttpResponse.json(reads < 2 ? { ...PENDING_PRICE, retry_after: 0 } : ESTIMATE)
      }),
    )
    show()
    expect(await screen.findByText('$12.40 for one')).toBeInTheDocument()
    expect(polls).toBe(2)
    expect(reads).toBe(2)
    expect(screen.getByTestId('preview')).toHaveAttribute('data-status', 'done')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says the price is being worked out while the slice runs, then shows it (#6153)', async () => {
    let reads = 0
    let release = () => {}
    const sliced = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(DONE)),
      http.get('/api/store/v1/renders/:id/estimate', async () => {
        reads += 1
        if (reads > 1) await sliced
        return HttpResponse.json(reads < 3 ? { ...PENDING_PRICE, retry_after: 0 } : ESTIMATE)
      }),
    )
    show()
    expect(await screen.findByText('Working out the price…')).toBeInTheDocument()
    expect(screen.queryByText(/\$/)).toBeNull()
    release()
    expect(await screen.findByText('$12.40 for one')).toBeInTheDocument()
    expect(screen.queryByText('Working out the price…')).toBeNull()
  })

  it('stops saying the price is being worked out when the read of it fails (#6921 review)', async () => {
    let reads = 0
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(DONE)),
      http.get('/api/store/v1/renders/:id/estimate', () => {
        reads += 1
        return reads < 2
          ? HttpResponse.json({ ...PENDING_PRICE, retry_after: 0 })
          : HttpResponse.json({ type: `${PROBLEMS}render_not_found`, status: 404, detail: 'gone' }, { status: 404, headers: { 'Content-Type': 'application/problem+json' } })
      }),
    )
    show()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(reads).toBe(2)
    expect(screen.queryByText('Working out the price…')).toBeNull()
  })

  it('shows no number when there is no firm price (#6153)', async () => {
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(DONE)),
      http.get('/api/store/v1/renders/:id/estimate', () => HttpResponse.json(NO_PRICE)),
    )
    show()
    expect(await screen.findByText('We price this design by hand')).toBeInTheDocument()
    expect(screen.queryByText(/\$/)).toBeNull()
  })

  it('shows no number for a price still pending at the time limit', async () => {
    server.use(
      http.post('/api/store/v1/renders', () => HttpResponse.json(DONE)),
      http.get('/api/store/v1/renders/:id/estimate', () => HttpResponse.json({ ...PENDING_PRICE, retry_after: 0 })),
    )
    show('l1', { firstMs: 5, maxMs: 10, limitMs: 60 })
    expect(await screen.findByText('We price this design by hand')).toBeInTheDocument()
    expect(screen.queryByText(/\$/)).toBeNull()
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
    expect(await screen.findByText('$12.40 for one')).toBeInTheDocument()
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
    expect(await screen.findByText('$12.40 for one')).toBeInTheDocument()
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
    // A slow slice, as on the loaded CI runner of #6927: the load's price shows, marked as
    // updating, while the price of the change is still read. The handler falls through to the
    // mock's own.
    server.use(http.get('/api/store/v1/renders/:id/estimate', () => new Promise<undefined>((resolve) => setTimeout(resolve, 200))))
    const first = show()
    // The load's render is priced before the visitor types (#6927 CI: a slow page did so).
    expect(await screen.findByText('$12.40 for one')).toBeInTheDocument()
    await first.user.type(screen.getByLabelText('Name'), 'Ada')
    await first.user.click(screen.getByRole('button', { name: 'One more' }))
    // The first page's render of these values was priced before it left: the price is firm,
    // not the load's price still marked as updating.
    await waitFor(
      () => {
        expect(screen.queryByText('Updating this price…')).toBeNull()
        expect(screen.getByText('$12.40 for one')).toBeInTheDocument()
      },
      { timeout: 5_000 },
    )
    first.unmount()
    const sent = renders()
    const read: unknown[] = []
    server.events.on('request:start', async ({ request }) => {
      if (new URL(request.url).pathname === '/api/store/v1/listings/l1/variant') read.push(await request.clone().json())
    })
    show()
    expect(await screen.findByLabelText('Name')).toHaveValue('Ada')
    expect(screen.getByText('2')).toBeInTheDocument()
    await waitFor(() => expect(read).toHaveLength(1))
    expect(read[0]).toMatchObject({ values: { name: 'Ada' } })
    // #6921: the shop held its preview and firm price, so nothing is rendered again.
    expect(await screen.findByText('$12.40 for one')).toBeInTheDocument()
    expect(sent).toEqual([])
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

/** A file listing's price of other colours, while the shop works out the chosen ones'. */
const LAST = { source: 'last', price_minor: 650, currency: 'USD', minor_exponent: 2, retry_after: 0 }
const HELD_NOTE = 'This was our last price. We confirm the price before you pay.'

describe('CustomizePage of a file listing (#6710)', () => {
  beforeEach(() => {
    window.localStorage.clear()
    return () => server.events.removeAllListeners()
  })

  /** Every request the page sent, as `METHOD path` and its JSON body. */
  function requests(): { what: string; body: unknown }[] {
    const sent: { what: string; body: unknown }[] = []
    server.events.on('request:start', async ({ request }) => {
      const body: unknown = request.method === 'POST' ? await request.clone().json() : null
      sent.push({ what: `${request.method} ${new URL(request.url).pathname}`, body })
    })
    return sent
  }

  it('shows one picker per part and recolours the held preview with no render', async () => {
    const sent = requests()
    const { user } = show(FILE_ID)
    expect(await screen.findByRole('button', { name: /Part 1/ })).toHaveTextContent('Ocean blue')
    expect(screen.getByRole('button', { name: /Part 2/ })).toHaveTextContent('Signal orange')
    const preview = await screen.findByTestId('preview')
    expect(preview).toHaveAttribute('data-url', '/api/store/v1/listings/l4/preview/1.glb')
    expect(JSON.parse(preview.dataset.slots!)).toEqual({ 0: '#1B6CA8FF', 1: '#E8532FFF' })

    await user.click(screen.getByRole('button', { name: /Part 2/ }))
    await user.click(await screen.findByRole('radio', { name: 'Ocean blue' }))
    await waitFor(() => expect(JSON.parse(screen.getByTestId('preview').dataset.slots!)).toEqual({ 0: '#1B6CA8FF', 1: '#1B6CA8FF' }))
    // The page never shows a material, the file's own colours or its name.
    expect(screen.queryByText(/PLA|PETG|3mf/)).toBeNull()
    await screen.findByText('$7.00 for one')
    expect(sent.filter(({ what }) => /\/renders|\/variant/.test(what))).toEqual([])
  })

  it('chooses a plate when the file has more than one', async () => {
    const { user } = show(FILE_ID)
    await user.click(await screen.findByRole('radio', { name: 'Plate 2' }))
    expect(screen.getByTestId('preview')).toHaveAttribute('data-url', '/api/store/v1/listings/l4/preview/2.glb')
  })

  it('reads the price route with the colours, shows it pending, then firm', async () => {
    const sent = requests()
    show(FILE_ID)
    expect(await screen.findByText('Working out the price…')).toBeInTheDocument()
    expect(await screen.findByText('$7.00 for one')).toBeInTheDocument()
    const prices = sent.filter(({ what }) => what === 'POST /api/store/v1/listings/l4/price')
    expect(prices.length).toBeGreaterThanOrEqual(2)
    expect(prices[0]!.body).toEqual({ listing_updated_at: '2026-11-07T12:00:00Z', colours: { 0: 'f-blue', 1: 'f-orange' } })
  })

  it('keeps the last price on screen, marked as updating, until the new colours are priced', async () => {
    const { user } = show(FILE_ID)
    await screen.findByText('$7.00 for one')
    await user.click(screen.getByRole('button', { name: /Part 1/ }))
    await user.click(await screen.findByRole('radio', { name: 'Signal orange' }))
    expect(screen.getByText('$7.00 for one')).toBeInTheDocument()
    expect(screen.queryByText('Working out the price…')).toBeNull()
  })

  it("shows the listing's last price, marked as updating, for colours never priced (#6710 §7.5)", async () => {
    let reads = 0
    server.use(
      http.post('/api/store/v1/listings/l4/price', () => {
        reads += 1
        return HttpResponse.json(reads < 3 ? LAST : { ...LAST, source: 'slice', price_minor: 700, retry_after: null })
      }),
    )
    show(FILE_ID)
    expect(await screen.findByText('$6.50 for one')).toBeInTheDocument()
    expect(screen.getByText('Updating this price…')).toBeInTheDocument()
    expect(screen.queryByText('Working out the price…')).toBeNull()
    expect(await screen.findByText('$7.00 for one')).toBeInTheDocument()
    expect(screen.queryByText('Updating this price…')).toBeNull()
    expect(reads).toBe(3)
  })

  it('keeps the last price on screen, not as updating, when a read is refused', async () => {
    const { user } = show(FILE_ID)
    await screen.findByText('$7.00 for one')
    server.use(http.post('/api/store/v1/listings/l4/price', () => HttpResponse.json(LIMITED, { status: 429, headers: { 'Retry-After': '42' } })))
    await user.click(screen.getByRole('button', { name: /Part 1/ }))
    await user.click(await screen.findByRole('radio', { name: 'Signal orange' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('$7.00 for one')).toBeInTheDocument()
    expect(screen.queryByText('Updating this price…')).toBeNull()
    // Another colours' price is never shown as firm.
    expect(screen.getByText(HELD_NOTE)).toBeInTheDocument()
  })

  it('keeps a last price, never as firm, when a read again is refused or fails', async () => {
    for (const refusal of [
      () => HttpResponse.json(LIMITED, { status: 429, headers: { 'Retry-After': '42' } }),
      () => HttpResponse.json({ type: 'about:blank', title: 'Bad Request', status: 400, detail: 'No.' }, { status: 400 }),
    ]) {
      let reads = 0
      server.use(http.post('/api/store/v1/listings/l4/price', () => (++reads === 1 ? HttpResponse.json(LAST) : refusal())))
      const { unmount } = show(FILE_ID)
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      expect(screen.getByText('$6.50 for one')).toBeInTheDocument()
      expect(screen.getByText(HELD_NOTE)).toBeInTheDocument()
      expect(screen.queryByText('Updating this price…')).toBeNull()
      unmount()
    }
  })

  it('keeps a last price, never as firm, past the time limit', async () => {
    server.use(http.post('/api/store/v1/listings/l4/price', () => HttpResponse.json(LAST)))
    show(FILE_ID, { ...FAST, limitMs: 60 })
    expect(await screen.findByText(HELD_NOTE)).toBeInTheDocument()
    expect(screen.getByText('$6.50 for one')).toBeInTheDocument()
    expect(screen.queryByText('We price this design by hand')).toBeNull()
  })

  it('marks the price as updating again once the colours change after a refusal', async () => {
    const { user } = show(FILE_ID, FAST, 300)
    await screen.findByText('$7.00 for one')
    server.use(http.post('/api/store/v1/listings/l4/price', () => HttpResponse.json(LIMITED, { status: 429, headers: { 'Retry-After': '42' } })))
    const pick = async (colour: string) => {
      await user.click(screen.getByRole('button', { name: /Part 1/ }))
      await user.click(await screen.findByRole('radio', { name: colour }))
    }
    await pick('Signal orange')
    await screen.findByText(HELD_NOTE)
    await pick('Ocean blue')
    await pick('Signal orange')
    // Through the debounce: the refusal was of an earlier read, and a new one is due.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText(HELD_NOTE)).toBeNull()
    expect(screen.getByText('Updating this price…')).toBeInTheDocument()
  })

  it('reads no price again when the colour on screen is picked again', async () => {
    const sent = requests()
    const { user } = show(FILE_ID)
    await screen.findByText('$7.00 for one')
    const before = sent.filter(({ what }) => what === 'POST /api/store/v1/listings/l4/price').length
    await user.click(screen.getByRole('button', { name: /Part 1/ }))
    await user.click(await screen.findByRole('radio', { name: 'Ocean blue' }))
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(sent.filter(({ what }) => what === 'POST /api/store/v1/listings/l4/price')).toHaveLength(before)
    expect(screen.queryByText('Updating this price…')).toBeNull()
  })

  it('says the price is being worked out from the change on, when no price is known', async () => {
    server.use(http.post('/api/store/v1/listings/l4/price', () => HttpResponse.json(NO_PRICE)))
    const { user } = show(FILE_ID, FAST, 10_000)
    await screen.findByText('We price this design by hand')
    await user.click(screen.getByRole('button', { name: /Part 1/ }))
    await user.click(await screen.findByRole('radio', { name: 'Signal orange' }))
    // The read waits out the debounce; the page says it is working the price out meanwhile.
    expect(await screen.findByText('Working out the price…')).toBeInTheDocument()
    expect(screen.queryByText('We price this design by hand')).toBeNull()
  })

  it('adds the colours to the order, with no values', async () => {
    const sent = requests()
    const { user } = show(FILE_ID)
    await screen.findByRole('button', { name: /Part 1/ })
    await user.click(screen.getByRole('button', { name: 'Add to order' }))
    await screen.findByText('Added to your order.')
    const added = sent.find(({ what }) => what === 'POST /api/store/v1/draft/items')
    expect(added?.body).toEqual({
      listing_id: 'l4',
      listing_updated_at: '2026-11-07T12:00:00Z',
      values: {},
      colours: { 0: 'f-blue', 1: 'f-orange' },
      quantity: 1,
    })
  })

  it('names the parts whose colours cannot go together', async () => {
    const { user } = show(FILE_ID)
    await user.click(await screen.findByRole('button', { name: /Part 1/ }))
    await user.click(await screen.findByRole('radio', { name: 'Rust' }))
    await user.click(screen.getByRole('button', { name: /Part 2/ }))
    await user.click(await screen.findByRole('radio', { name: 'Signal orange' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("The part 2 can't go with the other colours.")
  })

  it('says when the shop changed the design, and loads it again', async () => {
    server.use(http.post('/api/store/v1/listings/l4/price', () => HttpResponse.json({ type: `${PROBLEMS}listing_changed`, title: 'Conflict', status: 409, detail: 'This listing has changed. Load it again.' }, { status: 409 })))
    const { user } = show(FILE_ID)
    await user.click(await screen.findByRole('button', { name: 'Load the latest version' }))
    expect(await screen.findByRole('button', { name: /Part 1/ })).toBeInTheDocument()
  })
})
