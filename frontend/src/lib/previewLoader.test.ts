import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PreviewLoader, RETRY_DELAYS_MS, stopRetries } from './previewLoader'

const URL_ = 'http://shop.test/api/store/v1/renders/a/preview.glb'
const mesh = () => new Response(JSON.stringify({ asset: { version: '2.0' } }))
const refusal = (status: number) => () => new Response('', { status })

/** A fetch that answers each call in turn, its last answer from then on. */
function answers(...each: (() => Response)[]) {
  const fetched = vi.fn(async () => (each.length > 1 ? each.shift()! : each[0]!)())
  vi.stubGlobal('fetch', fetched)
  return fetched
}

function load() {
  const loaded = vi.fn()
  const failed = vi.fn()
  new PreviewLoader().load(URL_, loaded, undefined, failed)
  return { loaded, failed }
}

describe('PreviewLoader (#6968)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it.each([502, 503, 504])('asks again for a preview that answered %i', async (status) => {
    const fetched = answers(refusal(status), refusal(status), mesh)
    const { loaded, failed } = load()
    await vi.advanceTimersByTimeAsync(0)
    expect(fetched).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(RETRY_DELAYS_MS[0]!)
    expect(fetched).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(RETRY_DELAYS_MS[1]!)
    expect(fetched).toHaveBeenCalledTimes(3)
    await vi.waitFor(() => expect(loaded).toHaveBeenCalledTimes(1))
    expect(failed).not.toHaveBeenCalled()
  })

  it('fails once every try was refused', async () => {
    const fetched = answers(refusal(504))
    const { loaded, failed } = load()
    await vi.advanceTimersByTimeAsync(RETRY_DELAYS_MS.reduce((sum, delay) => sum + delay, 0) + 1)
    await vi.waitFor(() => expect(failed).toHaveBeenCalledTimes(1))
    expect(fetched).toHaveBeenCalledTimes(RETRY_DELAYS_MS.length + 1)
    expect(loaded).not.toHaveBeenCalled()
  })

  it.each([404, 422, 500])('does not ask again after a %i', async (status) => {
    const fetched = answers(refusal(status))
    const { failed } = load()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(failed).toHaveBeenCalledTimes(1)
    expect(fetched).toHaveBeenCalledTimes(1)
  })

  it('stops a waiting retry at once', async () => {
    const fetched = answers(refusal(503))
    const { loaded, failed } = load()
    await vi.advanceTimersByTimeAsync(0)
    await vi.waitFor(() => expect(fetched).toHaveBeenCalledTimes(1))
    expect(stopRetries(URL_)).toBe(true)
    expect(failed).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(fetched).toHaveBeenCalledTimes(1)
    expect(loaded).not.toHaveBeenCalled()
    expect(stopRetries(URL_)).toBe(false)
  })
})
