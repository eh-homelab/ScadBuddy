import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { activityKindsOf, upsertActivityKinds } from '../src/activity/kinds.js'
import type { Database } from '../src/db.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// ai_activity_kinds: the agent's activity kinds as data (spec 2026-10-09 §4).

describe('activityKindsOf', () => {
  it('prefixes the name and labels it from the kind or the spaced name', () => {
    expect(activityKindsOf([{ name: 'plugin_package_install', label: 'Install plugin package' }, { name: 'a_b' }])).toEqual([
      { key: 'agent.plugin_package_install', label: 'Install plugin package', facets: [] },
      { key: 'agent.a_b', label: 'a b', facets: [] },
    ])
  })
})

describe.skipIf(!TEST_DATABASE_URL)(`ai_activity_kinds${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  it('upserts declared kinds and retires the rest', async () => {
    await upsertActivityKinds(db.sql, [
      { key: 'agent.a', label: 'A', facets: [] },
      { key: 'agent.b', label: 'B', facets: [] },
    ])
    await upsertActivityKinds(db.sql, [{ key: 'agent.a', label: 'A2', facets: [] }])
    const rows = await db.sql`SELECT key, label, retired_at IS NOT NULL AS retired FROM ai_activity_kinds ORDER BY key`
    expect(rows).toEqual([
      { key: 'agent.a', label: 'A2', retired: false },
      { key: 'agent.b', label: 'B', retired: true },
    ])
    await upsertActivityKinds(db.sql, [{ key: 'agent.b', label: 'B', facets: [] }])
    const [b] = await db.sql`SELECT retired_at IS NOT NULL AS retired FROM ai_activity_kinds WHERE key = 'agent.b'`
    expect(b).toEqual({ retired: false })
  })
})
