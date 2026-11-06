import { randomUUID } from 'node:crypto'
import postgres from 'postgres'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { API_AI_GRANTS, grantBackendRoles, PROJECTS_AI_GRANTS } from '../src/db/grants.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase, type Throwaway } from './support/postgres.js'

// #5210: the agent owns its ai_* tables and grants the backend's roles what they use of them.

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

  it('grants the API its list and the projects role its own, and nothing more', async () => {
    const reserved = await t.db.sql.reserve()
    try {
      await reserved.unsafe(`SET ROLE "${roles.agent}"`)
      expect(await grantBackendRoles(reserved, roles.projects)).toEqual([roles.api, roles.projects])
    } finally {
      await reserved.unsafe('RESET ROLE')
      reserved.release()
    }
    for (const [table, privileges] of Object.entries(API_AI_GRANTS)) {
      for (const p of privileges) expect(await can(roles.api, table, p), `${roles.api} ${p} ${table}`).toBe(true)
    }
    for (const [table, privileges] of Object.entries(PROJECTS_AI_GRANTS)) {
      for (const p of privileges) expect(await can(roles.projects, table, p), `${roles.projects} ${p} ${table}`).toBe(true)
    }
    // ScadBuddy#5298: a run's payload keys come through the API, sealed.
    for (const p of ['SELECT', 'INSERT', 'DELETE']) {
      expect(await can(roles.projects, 'ai_payload_keys', p), `${roles.projects} ${p} ai_payload_keys`).toBe(false)
    }
    expect(await can(roles.projects, 'ai_forgotten_subjects', 'SELECT')).toBe(false)
    expect(await can(roles.projects, 'ai_operations', 'SELECT')).toBe(false)
    // ScadBuddy#5298: a session's tiers and the approval timeout come through the API.
    for (const table of ['ai_sessions', 'ai_mcp_tokens', 'ai_settings']) {
      expect(await can(roles.api, table, 'SELECT'), `${roles.api} SELECT ${table}`).toBe(true)
      expect(await can(roles.projects, table, 'SELECT'), `${roles.projects} SELECT ${table}`).toBe(false)
    }
  })

  it('grants nobody on one shared role', async () => {
    const reserved = await t.db.sql.reserve()
    try {
      await reserved.unsafe(`SET ROLE "${roles.api}"`)
      expect(await grantBackendRoles(reserved, `absent_${randomUUID().replaceAll('-', '').slice(0, 8)}`)).toEqual([])
    } finally {
      await reserved.unsafe('RESET ROLE')
      reserved.release()
    }
  })
})
