import { describe, expect, it } from 'vitest'
import { readPluginSecrets } from '../src/plugins/secrets.js'
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

  it('uses the same minimum as redact and the upload search', () => {
    const short = 'x'.repeat(SECRET_MIN_CHARS - 1)
    const enough = 'y'.repeat(SECRET_MIN_CHARS)
    expect(redact(`${short} ${enough}`, [short, enough])).toBe(`${short} [redacted]`)
    expect(asSecretBytes([short, enough]).map((b) => b.toString('utf8'))).toEqual([enough])
  })
})
