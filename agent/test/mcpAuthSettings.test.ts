import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  DEFAULT_MCP_AUTH,
  mcpAuthSettings,
  SETTING_MCP_ANONYMOUS_CAP,
  SETTING_MCP_AUTH_MODE,
} from '../src/auth/authenticate.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { appFetch, MCP_URL, testApp } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// The MCP auth mode and anonymous cap as ai_settings keys (spec §8.3, §9;
// auth/authenticate.ts `mcpAuthSettings`).

function reader(values: Record<string, unknown>) {
  return { get: <T>(key: string) => Promise.resolve(values[key] as T | undefined) }
}

describe('mcpAuthSettings', () => {
  it('is bearer with full anonymous access when nothing is set, or there is no store', async () => {
    const warned: string[] = []
    expect(await mcpAuthSettings(reader({}), (m) => warned.push(m))()).toEqual(DEFAULT_MCP_AUTH)
    expect(await mcpAuthSettings(undefined, (m) => warned.push(m))()).toEqual(DEFAULT_MCP_AUTH)
    expect(DEFAULT_MCP_AUTH).toEqual({ mode: 'bearer', anonymousCap: 'outward' })
    expect(warned).toEqual([])
  })

  it('reads both keys', async () => {
    const values: Record<string, unknown> = { [SETTING_MCP_AUTH_MODE]: 'disabled', [SETTING_MCP_ANONYMOUS_CAP]: 'write' }
    expect(await mcpAuthSettings(reader(values), () => {})()).toEqual({ mode: 'disabled', anonymousCap: 'write' })
  })

  it('fails closed on a value it does not know: bearer, and a read cap', async () => {
    const warned: string[] = []
    const settings = mcpAuthSettings(reader({ [SETTING_MCP_AUTH_MODE]: 'open', [SETTING_MCP_ANONYMOUS_CAP]: 7 }), (m) =>
      warned.push(m),
    )
    expect(await settings()).toEqual({ mode: 'bearer', anonymousCap: 'read' })
    expect(warned).toEqual([
      expect.stringContaining(`${SETTING_MCP_AUTH_MODE} is "open"`),
      expect.stringContaining(`${SETTING_MCP_ANONYMOUS_CAP} is 7`),
    ])
  })

  it('warns that auth is disabled once, not per request, and again after a change', async () => {
    const values: Record<string, unknown> = { [SETTING_MCP_AUTH_MODE]: 'disabled' }
    const warned: string[] = []
    const settings = mcpAuthSettings(reader(values), (m) => warned.push(m))
    await settings()
    await settings()
    expect(warned).toEqual([expect.stringMatching(/MCP auth is DISABLED.*"outward" tier/)])
    values[SETTING_MCP_ANONYMOUS_CAP] = 'read'
    await settings()
    expect(warned).toHaveLength(2)
    expect(warned[1]).toMatch(/"read" tier/)
    values[SETTING_MCP_AUTH_MODE] = 'bearer'
    await settings()
    values[SETTING_MCP_AUTH_MODE] = 'disabled'
    await settings()
    expect(warned).toHaveLength(3)
  })

  it('leaves a failed read to throw, so /mcp fails closed', async () => {
    const broken = { get: () => Promise.reject(new Error('connection refused')) }
    await expect(mcpAuthSettings(broken, () => {})()).rejects.toThrow('connection refused')
  })
})

describe.skipIf(!TEST_DATABASE_URL)(`the MCP auth mode in ai_settings${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  it('applies to the next /mcp request: 401 in bearer mode, served anonymously once disabled', async () => {
    const store = new SettingsStore(db.sql)
    const { app } = testApp({ authSettings: mcpAuthSettings(store, () => {}) })
    const initialize = () =>
      appFetch(app)(MCP_URL, {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'initialize',
          params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 't', version: '0' } },
        }),
      })
    expect((await initialize()).status).toBe(401)
    await store.set(SETTING_MCP_AUTH_MODE, 'disabled')
    expect((await initialize()).status).toBe(200)
    await store.set(SETTING_MCP_AUTH_MODE, 'bearer')
    expect((await initialize()).status).toBe(401)
  })
})
