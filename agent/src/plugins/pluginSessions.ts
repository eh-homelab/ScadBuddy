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

/**
 * Ends every recorded session whose replica is not in `liveReplicas`, then forgets it. A
 * session of a plugin that is gone, disabled or no longer passes the egress check is forgotten
 * without a DELETE. Returns how many rows it removed.
 */
export async function sweepDeadReplicas(sql: Sql, liveReplicas: Set<string>, deps: SweepDeps): Promise<number> {
  const log = deps.log ?? (() => {})
  const dead = [...liveReplicas]
  const rows = await sql<{ plugin_id: string; upstream_session_id: string }[]>`
    SELECT plugin_id, upstream_session_id FROM ai_plugin_sessions
    WHERE replica <> ALL(${dead}) AND created_at < now() - ${(deps.graceMs ?? SWEEP_GRACE_MS) / 1000} * interval '1 second'`
  if (rows.length === 0) return 0
  const loaded = await loadEnabledPlugins(deps.plugins, deps.kek, deps.resolve)
  const byName = new Map(loaded.plugins.map((checked) => [checked.plugin.name, checked]))
  await Promise.all(
    rows.map(async (row) => {
      const checked = byName.get(row.plugin_id)
      if (checked) await deleteUpstreamSession(checked.plugin, checked.address, row.upstream_session_id, log)
      await sql`
        DELETE FROM ai_plugin_sessions
        WHERE plugin_id = ${row.plugin_id} AND upstream_session_id = ${row.upstream_session_id}`
    }),
  )
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
