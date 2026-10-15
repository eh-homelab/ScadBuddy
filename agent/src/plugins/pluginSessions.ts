import os from 'node:os'
import type { Client } from '@temporalio/client'
import type { Sql } from 'postgres'
import type { Kek } from '../secrets.js'
import { TASK_QUEUE } from '../temporal/names.js'
import { deleteUpstreamSession, type SessionLedger } from './forwarder.js'
import { loadEnabledPlugins, type PluginStore } from './registry.js'
import type { Resolver } from '../http/egress.js'

// The upstream MCP sessions this service's forwarders have open (migration `plugin_sessions`).
//
// Claude Code sends no session DELETE when its process ends (unified runtime spec
// 2026-10-13 §5.3 E7), so a warm process's recycle ends them itself (warm.ts `drop`,
// forwarder.ts `Registration.terminate`). A replica that dies leaves its sessions open and
// its memory gone: each is recorded here with the replica that holds it, and every replica
// sweeps the rows of replicas that no longer poll. Rows hold no secret: the sweep reads the
// plugin's URL and header from the registry by name, and goes through the same egress
// check as a turn (`loadEnabledPlugins`).

/** This replica's identity: the host behind its pinned `agent-tools.<host>` queue (main.ts HOST_QUEUE). */
export function replicaIdentity(): string {
  return os.hostname()
}

/** A row younger than this is left alone: its replica's worker may not be polling yet. */
export const SWEEP_GRACE_MS = 5 * 60_000
/** How long DescribeTaskQueue may take. */
const DESCRIBE_DEADLINE_MS = 10_000
const WORKFLOW_TASK_QUEUE = 1

/** What a package's http server's sessions are recorded under, apart from the registry's names (#3599). */
const PACKAGE_PREFIX = 'pkg:'
export const packageLedgerId = (server: string): string => `${PACKAGE_PREFIX}${server}`

export class PluginSessions implements SessionLedger {
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
}

export type SweepDeps = {
  plugins: Pick<PluginStore, 'enabled'>
  kek: Kek | undefined
  resolve?: Resolver
  /** Rows younger than this are kept (default SWEEP_GRACE_MS). */
  graceMs?: number
  log?: (message: string) => void
}

/** A row whose DELETE keeps failing is dropped after this, however it failed. */
export const SWEEP_GIVE_UP_MS = 24 * 3_600_000

type Row = { plugin_id: string; upstream_session_id: string; old: boolean }

/**
 * Sends each row's session its DELETE, as the plugin is now (the registry's URL and header, the
 * address the egress check passes), and forgets it. A session of a plugin that is gone, disabled
 * or refused by the egress check is forgotten without a DELETE (one log line names each such
 * plugin); so is a package's http server's (`packageLedgerId`), which is never looked up in the
 * registry (the packages are not resolved here). A failed DELETE keeps its row for the next sweep unless it is `old` or
 * `forgetFailed`.
 */
async function endSessions(sql: Sql, rows: Row[], deps: SweepDeps, forgetFailed: boolean): Promise<void> {
  if (rows.length === 0) return
  const log = deps.log ?? (() => {})
  const loaded = await loadEnabledPlugins(deps.plugins, deps.kek, deps.resolve)
  const byName = new Map(loaded.plugins.map((checked) => [checked.plugin.name, checked]))
  // A package's server is never looked up in the registry, whatever its name.
  const registered = (id: string) => (id.startsWith(PACKAGE_PREFIX) ? undefined : byName.get(id))
  for (const id of new Set(rows.filter((r) => !registered(r.plugin_id)).map((r) => r.plugin_id))) {
    log(
      id.startsWith(PACKAGE_PREFIX)
        ? `plugin session sweep: ${id.slice(PACKAGE_PREFIX.length)} is a package's server; its sessions are dropped without a DELETE`
        : `plugin session sweep: ${id} is not an enabled plugin that passes the egress check; its sessions are dropped without a DELETE`,
    )
  }
  await Promise.all(
    rows.map(async (row) => {
      const checked = registered(row.plugin_id)
      const ended = checked ? await deleteUpstreamSession(checked.plugin, checked.address, row.upstream_session_id, log) : true
      if (!ended && !row.old && !forgetFailed) return
      await sql`
        DELETE FROM ai_plugin_sessions
        WHERE plugin_id = ${row.plugin_id} AND upstream_session_id = ${row.upstream_session_id}`
    }),
  )
}

/**
 * Ends every recorded session whose replica is not in `liveReplicas`, then forgets it (see
 * `endSessions`). Returns how many rows it looked at.
 */
export async function sweepDeadReplicas(sql: Sql, liveReplicas: Set<string>, deps: SweepDeps): Promise<number> {
  const rows = await sql<Row[]>`
    SELECT plugin_id, upstream_session_id, created_at < now() - ${SWEEP_GIVE_UP_MS / 1000} * interval '1 second' AS old
    FROM ai_plugin_sessions
    WHERE replica <> ALL(${[...liveReplicas]}) AND created_at < now() - ${(deps.graceMs ?? SWEEP_GRACE_MS) / 1000} * interval '1 second'`
  await endSessions(sql, rows, deps, false)
  return rows.length
}

/** The database's clock, read once at start before anything can record a session: the line between this incarnation's rows and the last one's. */
export async function startMark(sql: Sql): Promise<Date> {
  const [row] = await sql<{ t: Date }[]>`SELECT now() AS t`
  return row!.t
}

/**
 * At start, before the first sweep: ends the sessions this replica's previous incarnation left
 * (the hostname is reused across a restart, so the sweep would count them live): its rows
 * recorded before `mark` (`startMark`), never this incarnation's. Failed DELETEs are forgotten
 * too: nothing would retry them.
 */
export async function endOwnSessions(sql: Sql, self: string, deps: SweepDeps, mark: Date): Promise<number> {
  const rows = await sql<Row[]>`
    SELECT plugin_id, upstream_session_id, false AS old FROM ai_plugin_sessions
    WHERE replica = ${self} AND created_at < ${mark}`
  await endSessions(sql, rows, deps, true)
  return rows.length
}

/** The replicas whose agent-tools worker polls, from DescribeTaskQueue; a poller's identity is `<pid>@<host>`. */
export async function pollingReplicas(client: Pick<Client, 'workflowService' | 'options' | 'withDeadline'>): Promise<Set<string>> {
  const { pollers } = await client.withDeadline(Date.now() + DESCRIBE_DEADLINE_MS, () =>
    client.workflowService.describeTaskQueue({
      namespace: client.options.namespace,
      taskQueue: { name: TASK_QUEUE },
      taskQueueType: WORKFLOW_TASK_QUEUE,
    }),
  )
  return new Set((pollers ?? []).flatMap((p) => (p.identity ? [p.identity.replace(/^\d+@/, '')] : [])))
}

export type SweepRunDeps = SweepDeps & {
  sql: Sql
  /** Undefined when Temporal is not configured. */
  client: Pick<Client, 'workflowService' | 'options' | 'withDeadline'> | undefined
  self?: string
}

/**
 * One sweep. Without Temporal, or when it cannot say who polls, or when nobody polls (this
 * replica's own worker should), the run is skipped and logged: a dead replica is only ever
 * one that is known not to poll.
 */
export async function runPluginSessionSweep(deps: SweepRunDeps): Promise<void> {
  const log = deps.log ?? (() => {})
  // Known limit: a replica whose agent-tools worker has been unable to poll for more than
  // SWEEP_GRACE_MS (Temporal unreachable from it) while it still serves warm sessions is not in
  // `live`, so the other replicas end its sessions. Its next call to one gets a 404 for the
  // session, and Claude Code opens a new one.
  if (!deps.client) return log('plugin session sweep skipped: Temporal is not configured')
  let live: Set<string>
  try {
    live = await pollingReplicas(deps.client)
  } catch (err) {
    return log(`plugin session sweep skipped: DescribeTaskQueue failed: ${err instanceof Error ? err.message : String(err)}`)
  }
  if (live.size === 0) return log('plugin session sweep skipped: no agent-tools worker polls')
  live.add(deps.self ?? replicaIdentity())
  const removed = await sweepDeadReplicas(deps.sql, live, deps)
  if (removed > 0) log(`plugin session sweep: ended ${removed} session(s) of replicas that no longer poll`)
}
