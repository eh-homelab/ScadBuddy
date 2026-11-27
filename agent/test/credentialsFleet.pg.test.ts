import { randomBytes } from 'node:crypto'
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { CredentialError } from '../src/credentials.js'
import { kekFromBase64 } from '../src/secrets.js'
import type { Database } from './support/database.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { FLEET_ROW_MESSAGE, SqlCredentials } from './support/sql/credentials.js'

// #7983: the SQL fake of the API's credential routes refuses the agent's caller every change to
// a fleet row, as backend agent_data/credentials.py and core's secrets/credentials.go do.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const KEY = 'sk-ant-api03-fleet-fake-aaaa'
const LOGIN = JSON.stringify({
  accessToken: 'sk-ant-oat01-fleet-fake-llll',
  refreshToken: 'sk-ant-ort01-fleet-fake-rrrr',
  expiresAt: 1795000000000,
  accountUuid: '8d2c3a4e-0000-4000-8000-00000000000a',
  subscriptionType: 'max',
})

async function refused(p: Promise<unknown>): Promise<[number, string]> {
  const err = await p.then(
    () => undefined,
    (e: unknown) => e,
  )
  expect(err).toBeInstanceOf(CredentialError)
  return [(err as CredentialError).status, (err as Error).message]
}

describe.skipIf(!TEST_DATABASE_URL)(`the SQL credentials fake and the fleet's rows${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let repo: SqlCredentials
  beforeAll(async () => {
    ;({ db, drop } = await throwawayDatabase())
    repo = new SqlCredentials(db.sql, kek)
  })
  afterAll(async () => {
    await drop()
  })
  beforeEach(async () => {
    await db.sql`DELETE FROM ai_credentials`
  })

  it('refuses a claude_login or a fleet row on create and save', async () => {
    for (const update of [
      { kind: 'claude_login' as const, secret: LOGIN },
      { kind: 'anthropic_api_key' as const, secret: KEY, scope: 'fleet' as const },
    ]) {
      expect(await refused(repo.create(update))).toEqual([409, FLEET_ROW_MESSAGE])
    }
    const fleet = await repo.seedFleet({ kind: 'anthropic_api_key', secret: KEY + 'f', scope: 'fleet' })
    expect(await refused(repo.put({ kind: 'anthropic_api_key', secret: KEY }, fleet.id))).toEqual([409, FLEET_ROW_MESSAGE])
  })

  it('keeps a claude_login’s account and email in clear', async () => {
    const login = await repo.seedFleet({ kind: 'claude_login', secret: LOGIN, email: 'ops@example.com' })
    expect([login.kind, login.scope, login.account_uuid, login.subscription_type, login.email]).toEqual([
      'claude_login',
      'fleet',
      '8d2c3a4e-0000-4000-8000-00000000000a',
      'max',
      'ops@example.com',
    ])
  })

  it('refuses the agent’s caller a delete, rename, reset, event or reorder of a fleet row', async () => {
    const login = await repo.seedFleet({ kind: 'claude_login', secret: LOGIN })
    const own = await repo.create({ kind: 'anthropic_api_key', secret: KEY })
    const fleet = await repo.seedFleet({ kind: 'anthropic_api_key', secret: KEY + 'f', scope: 'fleet' })
    const both = await repo.create({ kind: 'anthropic_api_key', secret: KEY + 'b', scope: 'both' })
    for (const row of [login, fleet]) {
      expect(await refused(repo.delete(row.id))).toEqual([409, FLEET_ROW_MESSAGE])
      expect(await refused(repo.reset(row.id))).toEqual([409, FLEET_ROW_MESSAGE])
      expect(await refused(repo.rename(row.id, 'Mine'))).toEqual([409, FLEET_ROW_MESSAGE])
      expect(await refused(repo.record(row.id, 0, { kind: 'disabled', reason: 'HTTP 401' }))).toEqual([409, FLEET_ROW_MESSAGE])
    }
    expect(await refused(repo.reorder([login.id, both.id, own.id]))).toEqual([409, FLEET_ROW_MESSAGE])
    const order = await repo.reorder([both.id, own.id])
    expect(order.map((c) => c.id)).toEqual([login.id, both.id, fleet.id, own.id])
    // With no id: the first row the agent uses, never the fleet's login before it.
    expect((await repo.get())?.id).toBe(both.id)
    expect((await repo.put({ kind: 'anthropic_api_key', secret: KEY + 'n' })).id).toBe(both.id)
    expect(await repo.delete()).toBe(true)
    const left = await repo.list()
    expect(left.map((c) => [c.id, c.priority, c.status, c.epoch, c.name])).toEqual([
      [login.id, 0, 'active', 0, null],
      [fleet.id, 1, 'active', 0, null],
      [own.id, 2, 'active', 0, null],
    ])
  })
})
