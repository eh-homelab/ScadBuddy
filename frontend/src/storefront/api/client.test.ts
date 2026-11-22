import { afterEach, describe, expect, it, vi } from 'vitest'
import { ActionKey, PROBLEMS, errorText, store } from './client'

const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

describe('ActionKey', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('keeps one key per action until it is done', () => {
    const keys = new ActionKey()
    const first = keys.for('a')
    expect(first).toMatch(UUID4)
    expect(keys.for('a')).toBe(first)
    expect(keys.for('b')).not.toBe(first)
    const b = keys.for('b')
    keys.done()
    expect(keys.for('b')).not.toBe(b)
  })

  it('makes a UUID v4 from random bytes where randomUUID is missing (an insecure context)', () => {
    const real = globalThis.crypto
    vi.stubGlobal('crypto', { getRandomValues: (bytes: Uint8Array<ArrayBuffer>) => real.getRandomValues(bytes) })
    const keys = new ActionKey()
    const one = keys.for('a')
    keys.done()
    const two = keys.for('a')
    expect(one).toMatch(UUID4)
    expect(two).toMatch(UUID4)
    expect(one).not.toBe(two)
  })
})

describe('a refusal', () => {
  afterEach(() => vi.unstubAllGlobals())

  function answer(status: number, body: unknown): void {
    vi.stubGlobal('fetch', () => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })))
  }

  async function shown(): Promise<string> {
    try {
      await store.listings()
    } catch (error) {
      return errorText(error)
    }
    throw new Error('no refusal')
  }

  it("never shows a proxy's own problem text (Cloudflare's 504, #6918)", async () => {
    answer(504, {
      type: 'https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-504/',
      status: 504,
      detail: 'The origin web server did not respond to Cloudflare within the allowed time.',
      cloudflare_error: true,
    })
    expect(await shown()).toBe('The shop could not answer. Try again in a moment.')
  })

  it("shows the shop's own problems and the edge's plain ones", async () => {
    answer(503, { type: `${PROBLEMS}render_unavailable`, status: 503, detail: 'Previews are paused. Try again later.' })
    expect(await shown()).toBe('Previews are paused. Try again later.')
    answer(503, { type: 'about:blank', status: 503, detail: 'The shop is unavailable. Try again later.' })
    expect(await shown()).toBe('The shop is unavailable. Try again later.')
  })
})
