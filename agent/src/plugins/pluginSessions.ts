import os from 'node:os'
import type { Client } from '@temporalio/client'
import { TASK_QUEUE } from '../temporal/names.js'
import { type QueueDescriber, queuePollers } from '../temporal/pollers.js'
import { deleteUpstreamSession, type SessionLedger } from './forwarder.js'
import { loadEnabledPlugins, type PluginStoreLike } from './registry.js'
import type { Resolver } from '../http/egress.js'
import { type CrudTarget, crudOf } from '../api/crudStores.js'

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

type Row = { plugin_id: string; upstream_session_id: string; old: boolean }

/**
 * The rows of `ai_plugin_sessions`: the forwarders' ledger (`seen`, `ended`) and the sweep's
 * reads, through the API's CRUD routes (`CrudPluginSessions`, area `plugin-sessions`).
 */
export type PluginSessionRows = SessionLedger & {
  /** The database's clock now, as Postgres's text, to the microsecond (#3691). */
  mark(): Promise<string>
  /** The rows of replicas not in `live`, older than `graceMs`; `old` past SWEEP_GIVE_UP_MS. */
  dead(live: ReadonlySet<string>, graceMs: number): Promise<Row[]>
  /** `self`'s rows recorded before `mark`. */
  own(self: string, mark: string): Promise<Row[]>
}

/** The rows through the API's CRUD routes (tools/go/core/internal/access). */
export class CrudPluginSessions implements PluginSessionRows {
  readonly #call: ReturnType<typeof crudOf>
  readonly #replica: string

  constructor(target: CrudTarget, replica: string = replicaIdentity()) {
    this.#call = crudOf(target)
    this.#replica = replica
  }

  async seen(plugin: string, id: string): Promise<void> {
    await this.#call('an upstream MCP session seen', '/plugin-sessions/seen', { body: { plugin, id, replica: this.#replica } })
  }

  async ended(plugin: string, id: string): Promise<void> {
    await this.#call('an upstream MCP session ended', '/plugin-sessions/ended', { body: { plugin, id } })
  }

  async mark(): Promise<string> {
    return (await this.#call<{ mark: string }>("the database's clock", '/plugin-sessions/mark')).mark
  }

  async dead(live: ReadonlySet<string>, graceMs: number): Promise<Row[]> {
    const body = { live: [...live], grace_ms: graceMs, give_up_ms: SWEEP_GIVE_UP_MS }
    return (await this.#call<{ rows: Row[] }>("the sessions of replicas that do not poll", '/plugin-sessions/dead', { body })).rows
  }

  async own(self: string, mark: string): Promise<Row[]> {
    return (await this.#call<{ rows: Row[] }>("this replica's earlier sessions", '/plugin-sessions/own', { body: { replica: self, mark } })).rows
  }
}

export type SweepDeps = {
  plugins: Pick<PluginStoreLike, 'enabled'>
  resolve?: Resolver
  /** Rows younger than this are kept (default SWEEP_GRACE_MS). */
  graceMs?: number
  log?: (message: string) => void
}

/** A row whose DELETE keeps failing is dropped after this, however it failed. */
export const SWEEP_GIVE_UP_MS = 24 * 3_600_000

/**
 * Sends each row's session its DELETE, as the plugin is now (the registry's URL and header, the
 * address the egress check passes), and forgets it. A session of a plugin that is gone, disabled
 * or refused by the egress check is forgotten without a DELETE (one log line names each such
 * plugin); so is a package's http server's (`packageLedgerId`), which is never looked up in the
 * registry (the packages are not resolved here). A failed DELETE keeps its row for the next sweep unless it is `old` or
 * `forgetFailed`.
 */
async function endSessions(ledger: PluginSessionRows, rows: Row[], deps: SweepDeps, forgetFailed: boolean): Promise<void> {
  if (rows.length === 0) return
  const log = deps.log ?? (() => {})
  const loaded = await loadEnabledPlugins(deps.plugins, deps.resolve)
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
      await ledger.ended(row.plugin_id, row.upstream_session_id)
    }),
  )
}

/**
 * Ends every recorded session whose replica is not in `liveReplicas`, then forgets it (see
 * `endSessions`). Returns how many rows it looked at.
 */
export async function sweepDeadReplicas(ledger: PluginSessionRows, liveReplicas: Set<string>, deps: SweepDeps): Promise<number> {
  const rows = await ledger.dead(liveReplicas, deps.graceMs ?? SWEEP_GRACE_MS)
  await endSessions(ledger, rows, deps, false)
  return rows.length
}

/**
 * At start, before the first sweep: ends the sessions this replica's previous incarnation left
 * (the hostname is reused across a restart, so the sweep would count them live): its rows
 * recorded before `mark` (`PluginSessionRows.mark`), never this incarnation's. Failed DELETEs are forgotten
 * too: nothing would retry them.
 */
export async function endOwnSessions(ledger: PluginSessionRows, self: string, deps: SweepDeps, mark: string): Promise<number> {
  const rows = await ledger.own(self, mark)
  await endSessions(ledger, rows, deps, true)
  return rows.length
}

/** The replicas whose agent-tools worker polls, from DescribeTaskQueue on every partition (temporal/pollers.ts); a poller's identity is `<pid>@<host>`. */
export async function pollingReplicas(client: QueueDescriber): Promise<Set<string>> {
  const pollers = await queuePollers(client, TASK_QUEUE, WORKFLOW_TASK_QUEUE, Date.now() + DESCRIBE_DEADLINE_MS)
  return new Set(pollers.flatMap((p) => (p.identity ? [p.identity.replace(/^\d+@/, '')] : [])))
}

export type SweepRunDeps = SweepDeps & {
  ledger: PluginSessionRows
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
  const removed = await sweepDeadReplicas(deps.ledger, live, deps)
  if (removed > 0) log(`plugin session sweep: ended ${removed} session(s) of replicas that no longer poll`)
}
