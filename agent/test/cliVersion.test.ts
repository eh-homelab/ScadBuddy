import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  bundledCliVersion,
  parseCliVersion,
  sdkDeclaredCliVersion,
} from '../src/harness/cliVersion.js'

describe('parseCliVersion', () => {
  it('reads the format the bundled binary prints', () => {
    expect(parseCliVersion('2.1.283 (Claude Code)\n')).toBe('2.1.283')
  })

  it('refuses anything else rather than guessing', () => {
    expect(() => parseCliVersion('')).toThrow(/unrecognised/)
    expect(() => parseCliVersion('claude 2.1.283')).toThrow(/unrecognised/)
  })
})

// Runs the real bundled binary (`--version` only: no network, no credentials),
// so a lockfile whose SDK and binary disagree fails here as well as in the image.
describe('bundled Claude Code binary', () => {
  it('reports the version the SDK declares', async () => {
    const dir = await mkdtemp(path.join(tmpdir(), 'claude-version-test-'))
    try {
      expect(await bundledCliVersion(dir)).toBe(await sdkDeclaredCliVersion())
    } finally {
      await rm(dir, { recursive: true, force: true })
    }
  }, 30_000)
})
