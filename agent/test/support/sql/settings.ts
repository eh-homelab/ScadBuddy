import type { Sql, TransactionSql } from 'postgres'
import { SettingsStore, type SettingsRows } from '../../../src/credentials.js'
import type { AuditSink } from '../../../src/audit/log.js'
import { isRuntimeKey } from '../../../src/sessions/runtimeSettings.js'
import { evictsWarmOnWrite } from '../../../src/sessions/warmEvict.js'

// `ai_settings` over SQL on the test database: what the API's `settings` routes run (backend
// tools/go/core/internal/secrets), as the agent once ran them itself. Test-only.

type Expected = { key: string; present: boolean; value?: unknown }

/** The NOTIFYs a write of `keys` sends on commit, as the API sends them. */
async function notify(tx: TransactionSql, keys: readonly string[]): Promise<void> {
  if (keys.some(evictsWarmOnWrite)) await tx`SELECT pg_notify('scadbuddy_warm_evict', '*')`
  if (keys.some(isRuntimeKey)) await tx`SELECT pg_notify('scadbuddy_settings', 'session-runtime')`
}

export class SqlSettingsRows implements SettingsRows {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async get<T>(key: string): Promise<T | undefined> {
    const [row] = await this.#sql<{ value: T }[]>`SELECT value FROM ai_settings WHERE key = ${key}`
    return row?.value
  }

  async write(values: Record<string, unknown>, expect: Expected[] | null): Promise<boolean> {
    return await this.#sql.begin(async (tx) => {
      if (expect !== null) {
        await tx`LOCK TABLE ai_settings IN SHARE ROW EXCLUSIVE MODE`
        for (const e of expect) {
          const [row] = await tx<{ value: unknown }[]>`SELECT value FROM ai_settings WHERE key = ${e.key}`
          if ((row !== undefined) !== e.present) return false
          if (row && JSON.stringify(row.value) !== JSON.stringify(e.value)) return false
        }
      }
      for (const [key, value] of Object.entries(values)) {
        await tx`
          INSERT INTO ai_settings (key, value) VALUES (${key}, ${tx.json(value as never)})
          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()`
      }
      await notify(tx, Object.keys(values))
      return true
    })
  }

  /** The check and the write in one transaction, under the table's lock. */
  async compareAndSet(
    values: Record<string, unknown>,
    check: (current: { get<T>(key: string): Promise<T | undefined> }) => Promise<boolean>,
  ): Promise<boolean> {
    return await this.#sql.begin(async (tx) => {
      await tx`LOCK TABLE ai_settings IN SHARE ROW EXCLUSIVE MODE`
      const current = {
        get: async <T>(key: string): Promise<T | undefined> => {
          const [row] = await tx<{ value: T }[]>`SELECT value FROM ai_settings WHERE key = ${key}`
          return row?.value
        },
      }
      if (!(await check(current))) return false
      for (const [key, value] of Object.entries(values)) {
        await tx`
          INSERT INTO ai_settings (key, value) VALUES (${key}, ${tx.json(value as never)})
          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()`
      }
      await notify(tx, Object.keys(values))
      return true
    })
  }
}

/** A `SettingsStore` over `sql`. */
export function sqlSettings(sql: Sql, audit?: AuditSink): SettingsStore {
  return new SettingsStore(new SqlSettingsRows(sql), audit)
}
