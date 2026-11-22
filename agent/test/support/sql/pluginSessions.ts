import type { Sql } from 'postgres'
import { type PluginSessionRows, replicaIdentity, SWEEP_GIVE_UP_MS } from '../../../src/plugins/pluginSessions.js'

// Test-only: `ai_plugin_sessions` over SQL. The agent itself goes through the API
// (src/plugins/pluginSessions.ts CrudPluginSessions).

type Row = { plugin_id: string; upstream_session_id: string; old: boolean }

/**
 * The database's clock, read once at start before anything can record a session: the line between this incarnation's rows and the last one's.
 * Kept as Postgres's text, to the microsecond: a JS Date keeps milliseconds, and a row from earlier in the mark's millisecond fell after it
 * (#3691). It goes back as `::text::timestamptz`: a parameter postgres.js sees as a timestamptz it sends through a Date, which cuts it again.
 */
export async function startMark(sql: Sql): Promise<string> {
  const [row] = await sql<{ t: string }[]>`SELECT now()::text AS t`
  return row!.t
}

export class PluginSessions implements PluginSessionRows {
  private readonly sql: Sql
  private readonly replica: string

  constructor(sql: Sql, replica: string = replicaIdentity()) {
    this.sql = sql
    this.replica = replica
  }

  async seen(plugin: string, id: string): Promise<void> {
    await this.sql`
      INSERT INTO ai_plugin_sessions (plugin_id, upstream_session_id, replica)
      VALUES (${plugin}, ${id}, ${this.replica})
      ON CONFLICT DO NOTHING`
  }

  async ended(plugin: string, id: string): Promise<void> {
    await this.sql`DELETE FROM ai_plugin_sessions WHERE plugin_id = ${plugin} AND upstream_session_id = ${id}`
  }

  mark(): Promise<string> {
    return startMark(this.sql)
  }

  async dead(live: ReadonlySet<string>, graceMs: number): Promise<Row[]> {
    return this.sql<Row[]>`
      SELECT plugin_id, upstream_session_id, created_at < now() - ${SWEEP_GIVE_UP_MS / 1000} * interval '1 second' AS old
      FROM ai_plugin_sessions
      WHERE replica <> ALL(${[...live]}) AND created_at < now() - ${graceMs / 1000} * interval '1 second'`
  }

  async own(self: string, mark: string): Promise<Row[]> {
    return this.sql<Row[]>`
      SELECT plugin_id, upstream_session_id, false AS old FROM ai_plugin_sessions
      WHERE replica = ${self} AND created_at < ${mark}::text::timestamptz`
  }
}
