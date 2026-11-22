import type { Sql, TransactionSql } from 'postgres'
import { CrudToolListings, type StoredTool } from '../../../src/access/listings.js'
import { packageServerSource, remoteSource } from '../../../src/access/model.js'
import type { HeldLease } from '../../../src/api/crudStores.js'

// Test-only: `ai_tool_listings` over SQL, with no lease (a test runs one connection test at a
// time), for the tests that run on a throwaway database. The agent itself goes through the
// API (src/access/listings.ts CrudToolListings, its lease on the API).

type Listings = Awaited<ReturnType<CrudToolListings['read']>>

export class SqlToolListings extends CrudToolListings {
  readonly #sql: Sql

  constructor(sql: Sql) {
    super({ baseUrl: 'http://no-api.invalid' })
    this.#sql = sql
  }

  override async read(sources: readonly string[]): Promise<Listings> {
    const rows = await this.#sql<{ source: string; tools: unknown; listed_at: Date; truncated: boolean }[]>`
      SELECT source, tools, listed_at, truncated FROM ai_tool_listings WHERE source = ANY(${[...sources]}::text[])`
    // The wire shape CrudToolListings parses: through its own `read` on a fake fetch.
    const answer = { listings: rows.map((r) => ({ ...r, listed_at: r.listed_at.toISOString() })) }
    const parse = new CrudToolListings({
      baseUrl: 'http://fake',
      fetch: async () => new Response(JSON.stringify(answer), { headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } }),
    })
    return parse.read(sources)
  }

  override async leased<T>(_key: string, fn: (lease: HeldLease) => Promise<T>): Promise<{ value: T } | 'busy' | 'full' | 'unreachable'> {
    return { value: await fn({ token: 'sql' } as HeldLease) }
  }

  override async putPlugin(_lease: HeldLease, name: string, url: string, tools: readonly StoredTool[], truncated: boolean): Promise<boolean> {
    return this.#sql.begin(async (tx) => {
      const [plugin] = await tx`SELECT 1 FROM ai_plugins WHERE name = ${name} AND url = ${url} FOR UPDATE`
      if (!plugin) return false
      await this.#upsert(tx, remoteSource(name), tools, truncated)
      return true
    })
  }

  override async putPackageServer(
    _lease: HeldLease,
    pkg: string,
    server: string,
    contentHash: string,
    tools: readonly StoredTool[],
    truncated: boolean,
  ): Promise<boolean> {
    return this.#sql.begin(async (tx) => {
      const [row] = await tx`SELECT 1 FROM ai_plugin_packages WHERE name = ${pkg} AND content_hash = ${contentHash} FOR UPDATE`
      if (!row) return false
      await this.#upsert(tx, packageServerSource(pkg, server), tools, truncated)
      return true
    })
  }

  async #upsert(tx: TransactionSql, source: string, tools: readonly StoredTool[], truncated: boolean): Promise<void> {
    await tx`
      INSERT INTO ai_tool_listings (source, tools, truncated)
      VALUES (${source}, ${tx.json(tools as never)}, ${truncated})
      ON CONFLICT (source) DO UPDATE SET tools = EXCLUDED.tools, truncated = EXCLUDED.truncated, listed_at = now()`
  }
}
