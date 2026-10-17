import { describe, expect, it, vi } from 'vitest'
import { resending } from './resend'

const refused = () => Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNREFUSED' } })

describe('resending (mswjs/interceptors#862)', () => {
  it('sends a refused request again, logging the first resend only', async () => {
    const ok = new Response('ok')
    const intercepted = vi.fn().mockRejectedValueOnce(refused()).mockRejectedValueOnce(refused()).mockResolvedValue(ok)
    const log = vi.fn()
    await expect(resending(intercepted, log, 3)('http://test/a')).resolves.toBe(ok)
    expect(intercepted).toHaveBeenCalledTimes(3)
    expect(log.mock.calls).toEqual([[expect.stringContaining('resending http://test/a')]])
  })

  it('gives up after its tries, saying so', async () => {
    const intercepted = vi.fn().mockRejectedValue(refused())
    const log = vi.fn()
    await expect(resending(intercepted, log, 3)('http://test/b')).rejects.toThrow('fetch failed')
    expect(intercepted).toHaveBeenCalledTimes(3)
    expect(log.mock.calls).toEqual([[expect.stringContaining('resending')], [expect.stringContaining('gave up')]])
  })

  it("never resends an aborted request, whether init's signal or the Request's own", async () => {
    const aborted = AbortSignal.abort()
    for (const call of [
      (f: typeof fetch) => f('http://test/c', { signal: aborted }),
      (f: typeof fetch) => f(new Request('http://test/c', { signal: aborted })),
    ]) {
      const intercepted = vi.fn().mockRejectedValue(refused())
      await expect(call(resending(intercepted, vi.fn(), 3))).rejects.toThrow('fetch failed')
      expect(intercepted).toHaveBeenCalledOnce()
    }
  })

  it("treats init's explicit null signal as none, as fetch does", async () => {
    const request = new Request('http://test/e', { signal: AbortSignal.abort() })
    const intercepted = vi.fn().mockRejectedValueOnce(refused()).mockResolvedValue(new Response('ok'))
    await expect(resending(intercepted, vi.fn(), 3)(request, { signal: null })).resolves.toBeInstanceOf(Response)
    expect(intercepted).toHaveBeenCalledTimes(2)
  })

  it("keeps the Request's own signal when init's is undefined, as fetch does", async () => {
    const request = new Request('http://test/f', { signal: AbortSignal.abort() })
    const intercepted = vi.fn().mockRejectedValue(refused())
    await expect(resending(intercepted, vi.fn(), 3)(request, { signal: undefined })).rejects.toThrow('fetch failed')
    expect(intercepted).toHaveBeenCalledOnce()
  })

  it('passes any other failure straight through', async () => {
    const intercepted = vi.fn().mockRejectedValue(new TypeError('bad url'))
    await expect(resending(intercepted, vi.fn(), 3)('http://test/d')).rejects.toThrow('bad url')
    expect(intercepted).toHaveBeenCalledOnce()
  })
})
