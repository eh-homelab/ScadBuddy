import { randomUUID } from 'node:crypto'
import postgres from 'postgres'
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { API_AI_GRANTS, GRANT_LOCK, grantBackendRoles, revokeRole } from '../src/db/grants.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// #5210: the agent owns its ai_* tables and grants the backend's roles what they use of them.

it('takes the same advisory lock as the backend grants', () => {
  const source = readFileSync(new URL('../../backend/scadbuddy/auth/caller_grants.py', import.meta.url), 'utf8')
  const found = /^GRANT_LOCK = (0x[0-9A-Fa-f_]+)$/m.exec(source)
  expect(found, 'GRANT_LOCK in caller_grants.py').not.toBeNull()
  expect(BigInt(found![1]!.replaceAll('_', ''))).toBe(GRANT_LOCK)
})

const skip = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

describe.skipIf(skip !== '')(`the backend roles' ai_* grants${skip}`, () => {
  let t: Throwaway
  let roles: { agent: string; api: string; projects: string }
  const admin = () => postgres(TEST_DATABASE_URL!, { max: 1, onnotice: () => {} })

  beforeEach(async () => {
    const id = randomUUID().replaceAll('-', '').slice(0, 10)
    roles = { agent: `agent_${id}`, api: `api_${id}`, projects: `proj_${id}` }
    const a = admin()
    for (const role of Object.values(roles)) await a.unsafe(`CREATE ROLE "${role}" NOLOGIN`)
    await a.end()
    t = await throwawayDatabase()
    // The API's ledger, owned by the API's role; the agent's tables, owned by the agent's.
    await t.db.sql.unsafe(`CREATE TABLE IF NOT EXISTS scadbuddy_migrations (id text)`)
    await t.db.sql.unsafe(`ALTER TABLE scadbuddy_migrations OWNER TO "${roles.api}"`)
    for (const table of Object.keys(API_AI_GRANTS)) await t.db.sql.unsafe(`ALTER TABLE "${table}" OWNER TO "${roles.agent}"`)
  })
  afterEach(async () => {
    await t.drop()
    const a = admin()
    for (const role of Object.values(roles)) await a.unsafe(`DROP ROLE IF EXISTS "${role}"`)
    await a.end()
  })

  const can = async (role: string, table: string, privilege: string) => {
    const [row] = await t.db.sql<{ ok: boolean }[]>`SELECT has_table_privilege(${role}, ${table}, ${privilege}) AS ok`
    return row!.ok
  }

  // A client of its own whose session runs as `role` (a reserved connection has no
  // `begin`, which `revokeRole` uses).
  const asRole = async <T>(role: string, run: (sql: postgres.Sql) => Promise<T>): Promise<T> => {
    const sql = postgres(t.url, { max: 1, onnotice: () => {}, connection: { role } })
    try {
      return await run(sql)
    } finally {
      await sql.end({ timeout: 5 })
    }
  }
  const asAgent = <T>(run: (sql: postgres.Sql) => Promise<T>) => asRole(roles.agent, run)

  // What the agent granted `scadbuddy_projects` before ScadBuddy#5298.
  const OLD_PROJECTS_AI_GRANTS = ['ai_payload_keys', 'ai_forgotten_subjects', 'ai_sessions', 'ai_mcp_tokens', 'ai_settings']

  it('grants the API its list and the projects role nothing', async () => {
    expect(await asAgent(sql => grantBackendRoles(sql, roles.projects))).toEqual([roles.api])
    for (const [table, privileges] of Object.entries(API_AI_GRANTS)) {
      for (const p of privileges) expect(await can(roles.api, table, p), `${roles.api} ${p} ${table}`).toBe(true)
    }
    for (const table of Object.keys(API_AI_GRANTS)) {
      for (const p of ['SELECT', 'INSERT', 'UPDATE', 'DELETE']) {
        expect(await can(roles.projects, table, p), `${roles.projects} ${p} ${table}`).toBe(false)
      }
    }
  })

  it('takes back what it granted the projects role, once, and leaves the API\'s grants', async () => {
    await asAgent(async sql => {
      for (const table of OLD_PROJECTS_AI_GRANTS) await sql.unsafe(`GRANT SELECT, INSERT ON "${table}" TO "${roles.projects}"`)
    })
    // A backend table the API's role owns and granted (a superuser's grant is recorded as
    // the owner's): the API takes that back itself.
    await t.db.sql.unsafe(`CREATE TABLE settings_like (id int)`)
    await t.db.sql.unsafe(`ALTER TABLE settings_like OWNER TO "${roles.api}"`)
    await t.db.sql.unsafe(`GRANT SELECT ON settings_like TO "${roles.projects}"`)
    for (const table of OLD_PROJECTS_AI_GRANTS) expect(await can(roles.projects, table, 'SELECT')).toBe(true)

    expect(await asAgent(sql => grantBackendRoles(sql, roles.projects))).toEqual([roles.api])

    for (const table of OLD_PROJECTS_AI_GRANTS) {
      for (const p of ['SELECT', 'INSERT']) expect(await can(roles.projects, table, p), `${roles.projects} ${p} ${table}`).toBe(false)
    }
    expect(await can(roles.projects, 'settings_like', 'SELECT')).toBe(true)
    expect(await asAgent(sql => revokeRole(sql, roles.projects))).toEqual([])
  })

  it('revokes nothing from a missing role or its own', async () => {
    expect(await asAgent(sql => revokeRole(sql, `absent_${randomUUID().replaceAll('-', '').slice(0, 8)}`))).toEqual([])
    expect(await asAgent(sql => revokeRole(sql, roles.agent))).toEqual([])
  })

  it('grants nobody on one shared role', async () => {
    const absent = `absent_${randomUUID().replaceAll('-', '').slice(0, 8)}`
    expect(await asRole(roles.api, sql => grantBackendRoles(sql, absent))).toEqual([])
  })
})
