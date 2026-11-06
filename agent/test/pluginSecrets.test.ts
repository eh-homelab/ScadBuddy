import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PackagePin } from '../src/plugins/packages/store.js'
import { PLUGIN_SECRETS_CACHE_MS, readPluginSecrets, toolSecrets } from '../src/plugins/secrets.js'
import type { RemotePlugin } from '../src/plugins/registry.js'
import { redact, SECRET_MIN_CHARS } from '../src/secrets.js'
import { asSecretBytes } from '../src/tools/workspaceUploads.js'

// #4703: one reader of the plugin secrets for a turn's ToolCall tools and the UI's pick,
// and one minimum length for every search, so a short value never matches ordinary text.

const plugin = (value: string) => ({ header: { name: 'Authorization', value } }) as unknown as RemotePlugin

describe('readPluginSecrets', () => {
  it('gathers header values and OAuth secrets, once each, and leaves out a value under the minimum', async () => {
    const got = await readPluginSecrets({
      kek: undefined,
      plugins: { enabled: () => Promise.resolve({ plugins: [plugin('Bearer header-token-1'), plugin('abc')], problems: [] }) },
      oauth: { storedSecrets: () => Promise.resolve(['oauth-access-1', 'Bearer oauth-access-1', 'xy', 'oauth-access-1']) },
    })
    expect(got.sort()).toEqual(['Bearer header-token-1', 'Bearer oauth-access-1', 'header-token-1', 'oauth-access-1'])
  })

  it("reads every enabled package's header secrets from the installer, not from a turn's load (#5485)", async () => {
    const pins = [{ name: 'a' }, { name: 'b' }] as unknown as PackagePin[]
    const got = await readPluginSecrets({
      kek: undefined,
      packages: {
        store: { enabledPins: () => Promise.resolve(pins) },
        installer: { headerSecrets: (pin) => Promise.resolve(pin.name === 'a' ? ['Bearer pkg-a-token', 'pkg-a-token'] : ['xy']) },
      },
    })
    expect(got.sort()).toEqual(['Bearer pkg-a-token', 'pkg-a-token'])
    // One package whose files cannot be had fails the read: the list fails closed.
    await expect(
      readPluginSecrets({
        kek: undefined,
        packages: { store: { enabledPins: () => Promise.resolve(pins) }, installer: { headerSecrets: () => Promise.reject(new Error('fetch failed')) } },
      }),
    ).rejects.toThrow('fetch failed')
  })

  it('uses the same minimum as redact and the upload search', () => {
    const short = 'x'.repeat(SECRET_MIN_CHARS - 1)
    const enough = 'y'.repeat(SECRET_MIN_CHARS)
    expect(redact(`${short} ${enough}`, [short, enough])).toBe(`${short} [redacted]`)
    expect(asSecretBytes([short, enough]).map((b) => b.toString('utf8'))).toEqual([enough])
  })
})

// #5486: toolSecrets' cache of the plugin secrets, on a fake clock.
describe('toolSecrets', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-06T00:00:00Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  /** A plugin store that counts its reads and answers each from `answers` in turn. */
  function counted(...answers: (string | Error)[]) {
    let reads = 0
    const plugins = {
      enabled: () => {
        const answer = answers[Math.min(reads, answers.length - 1)]!
        reads++
        return answer instanceof Error ? Promise.reject(answer) : Promise.resolve({ plugins: [plugin(answer)], problems: [] })
      },
    }
    return { plugins, reads: () => reads }
  }

  it(`reuses one read for ${PLUGIN_SECRETS_CACHE_MS} ms, then reads again`, async () => {
    const { plugins, reads } = counted('Bearer first-token', 'Bearer second-token')
    const { secrets } = toolSecrets({ kek: undefined, plugins })
    expect(await secrets()).toContain('Bearer first-token')
    vi.advanceTimersByTime(PLUGIN_SECRETS_CACHE_MS)
    expect(await secrets()).toContain('Bearer first-token')
    expect(reads()).toBe(1)
    vi.advanceTimersByTime(1)
    expect(await secrets()).toContain('Bearer second-token')
    expect(reads()).toBe(2)
  })

  it('drops a read that failed, so the next call reads again', async () => {
    const { plugins, reads } = counted(new Error('store down'), 'Bearer after-token')
    const { secrets } = toolSecrets({ kek: undefined, plugins })
    await expect(secrets()).rejects.toThrow('store down')
    expect(await secrets()).toContain('Bearer after-token')
    expect(reads()).toBe(2)
  })

  it('reads fresh for a pick, past the cache and without filling it', async () => {
    const { plugins, reads } = counted('Bearer one-token', 'Bearer two-token', 'Bearer three-token')
    const { secrets, fresh } = toolSecrets({ kek: undefined, plugins })
    expect(await secrets()).toContain('Bearer one-token')
    expect(await fresh()).toContain('Bearer two-token')
    expect(await fresh()).toContain('Bearer three-token')
    expect(reads()).toBe(3)
    // The cached read is still the first one.
    expect(await secrets()).toContain('Bearer one-token')
    expect(reads()).toBe(3)
  })
})
