import type { RiskTier, TierResolver } from '../harness/permissions.js'
import { strictest } from './items.js'
import { type AccessRow, BUILTIN_SOURCES, type ToolAccess } from './model.js'

// Claude Code's own built-ins in every session (plan 2026-10-13 Step 9.6, decision Q3): the
// source `builtin:claude-code`, one item per tool, each at its default access until an admin
// sets it. They run in the turn's Claude Code process, in the session's working directory
// (workspace/directory.ts), and the seam confines their paths to it (harness/confine.ts).
//
// Bash and PowerShell are `off` (decision Q4): Claude Code's sandbox cannot run in the worker
// pod. Measured on 2026-11-01 in `scadbuddy-agent-worker` (read-only): `unshare --user
// --map-root-user --pid --fork` works, but with `--mount` it fails with "cannot change root
// filesystem propagation: Permission denied" (AppArmor `cri-containerd.apparmor.d`, enforce),
// and the image has no bwrap. Without a sandbox, Bash reads what the worker can (the KEK mount,
// every session's directory). An `allow_refused` package that names one still switches it on
// (`turnBuiltins`), at `outward`. Where the deployment sets up sandbox pods (#5864,
// config.ts `sandbox`), the turn never gets the native ones: the same two items govern the
// session's own Bash and PowerShell, which run in its sandbox pod (builtins/sandboxShell.ts),
// `outward` by default (SANDBOXED_ACCESS).
//
// WebFetch and WebSearch stay ScadBuddy's egress-checked ones (decision Q2, builtins/web.ts),
// and CronCreate, CronDelete, CronList, ScheduleWakeup, Monitor, Workflow and TaskStop the
// session's own (src/background/): SESSION_BUILTINS, served by the turn's `builtin` server
// (builtins/server.ts), items of this source too. Every session turn offers each one not
// `off`, the background ones only where the session keeps background tasks (not a session a
// flow run started, nor a Workflow agent's chat, nor any on an agent without Temporal or a database).

export type NativeBuiltin = { name: string; access: ToolAccess; label: string; description: string }

/** The shell built-in a Monitor's `command` source needs on (`monitorAccess`, live.ts). */
export const BASH = 'Bash'
/** The session built-in whose access its source raises (`monitorAccess`, `sessionTierOf`). */
export const MONITOR = 'Monitor'

export const NATIVE_BUILTINS: readonly NativeBuiltin[] = [
  { name: 'Read', access: 'read', label: 'Read a file', description: "Reads a file of the session's working directory." },
  { name: 'Glob', access: 'read', label: 'Find files', description: "Lists the session directory's files that match a glob pattern." },
  { name: 'Grep', access: 'read', label: 'Search files', description: "Searches the session directory's files with a regular expression." },
  // The task tools write, but only the session's own task list (no file, nothing outward), so `read`.
  { name: 'TaskCreate', access: 'read', label: 'Add a task', description: "Adds a task to the session's task list." },
  { name: 'TaskGet', access: 'read', label: 'Read a task', description: "Reads one task of the session's task list." },
  { name: 'TaskList', access: 'read', label: 'List the tasks', description: "Lists the session's task list." },
  { name: 'TaskUpdate', access: 'read', label: 'Update a task', description: "Changes a task of the session's task list." },
  { name: 'Write', access: 'write', label: 'Write a file', description: "Writes a file in the session's working directory." },
  { name: 'Edit', access: 'write', label: 'Edit a file', description: "Replaces exact text in a file of the session's working directory." },
  { name: 'NotebookEdit', access: 'write', label: 'Edit a notebook', description: "Edits a notebook cell in the session's working directory." },
  {
    name: BASH,
    access: 'off',
    label: 'Run a shell command',
    description:
      "Runs a shell command in the session's sandbox pod, where one is set up (#5864); there, outward by default. Off where none is: in the worker pod it could read every session and the key.",
  },
  {
    name: 'PowerShell',
    access: 'off',
    label: 'Run a PowerShell command',
    description: "Runs a PowerShell command in the session's sandbox pod, where one is set up (#5864), as Bash does. Off where none is.",
  },
]

/** The access Bash and PowerShell default to where they run in a sandbox pod (#5864): each call parks for a person. */
export const SANDBOXED_ACCESS: ToolAccess = 'outward'
/** The built-ins that run in a session's sandbox pod where one is set up (#5864, builtins/sandboxShell.ts). */
export const SHELL_BUILTINS: readonly string[] = ['Bash', 'PowerShell']

export const NATIVE_NAMES: ReadonlySet<string> = new Set(NATIVE_BUILTINS.map((b) => b.name))

/** A built-in the session serves in place of Claude Code's (builtins/server.ts `BUILTINS`); `background`: kept past the turn. */
export type SessionBuiltin = NativeBuiltin & { background: boolean }

/**
 * The session's own built-ins, under `builtin:claude-code` beside Claude Code's (#2726 R2: every
 * session has every built-in). WebFetch and WebSearch reach the internet, so `outward`, as
 * every package built-in was. CronCreate, ScheduleWakeup and Workflow start turns, or a
 * Workflow's agents, that spend after this one, and CronDelete and TaskStop end what those
 * started: each changes what the session keeps, so `write`. CronList only reads it, so `read`.
 *
 * Monitor is `write`, by its source (`sessionTierOf`): a `ws` source connects to a web host as
 * WebFetch does, so `outward`; a `command` source runs a shell where the session's Bash runs,
 * unsandboxed in the worker pod (decision Q4), so the turn offers it only while Bash is on, at
 * Bash's tier or Monitor's, the stricter, and never where Bash runs in a sandbox pod (#5864:
 * the command would run in the worker, not the pod; live.ts `MONITOR_SANDBOXED`, #5916). With Bash
 * off (its default) a Monitor watches a WebSocket only, and asks first.
 */
export const SESSION_BUILTINS: readonly SessionBuiltin[] = [
  { name: 'WebFetch', access: 'outward', background: false, label: 'Fetch a web page', description: 'Fetches a URL, egress-checked, and answers a prompt about the page.' },
  { name: 'WebSearch', access: 'outward', background: false, label: 'Search the web', description: 'Searches the web and returns the results with their URLs.' },
  { name: 'CronCreate', access: 'write', background: true, label: 'Schedule a prompt', description: 'Schedules a prompt to be sent to the session later, once or by a cron expression.' },
  { name: 'CronDelete', access: 'write', background: true, label: 'Cancel a scheduled prompt', description: 'Cancels a job CronCreate scheduled.' },
  { name: 'CronList', access: 'read', background: true, label: 'List the scheduled prompts', description: "Lists the session's scheduled jobs." },
  { name: 'ScheduleWakeup', access: 'write', background: true, label: 'Wake the session later', description: 'Sends a prompt to the session again after a delay, to pace a loop.' },
  {
    name: MONITOR,
    access: 'write',
    background: true,
    label: 'Watch a source',
    description:
      "Streams a WebSocket's frames, or a shell command's output lines, to the session as events. A WebSocket asks first, as WebFetch does; " +
      'a command runs only while Bash is on, at its tier, and never where Bash runs in a sandbox pod.',
  },
  { name: 'Workflow', access: 'write', background: true, label: 'Run a workflow', description: 'Runs a script that orchestrates subagents in the background.' },
  { name: 'TaskStop', access: 'write', background: true, label: 'Stop a background task', description: "Stops one of the session's cron jobs, wakeups, monitors or workflows." },
]

export const SESSION_NAMES: ReadonlySet<string> = new Set(SESSION_BUILTINS.map((b) => b.name))

/** Every item of `builtin:claude-code`: Claude Code's own, then the session's. */
export const BUILTIN_ITEMS: readonly NativeBuiltin[] = [...NATIVE_BUILTINS, ...SESSION_BUILTINS]

/** The prefix Claude Code gives the session's built-ins (builtins/server.ts `BUILTIN_SERVER`, `mcp__builtin__<Name>`). */
export const SERVED_PREFIX = 'mcp__builtin__'

/** The task-list tools: a turn that has one gets its list linked into the session directory (stateDirs.ts `linkTaskList`). */
export const TASK_TOOLS: ReadonlySet<string> = new Set(['TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate'])

/** One built-in's access now: its item under `builtin:claude-code`, and the packages that name it (resolver.ts `native`). */
export type NativeAccess = {
  native(name: string, declared: ToolAccess, packages: readonly string[], rows?: readonly AccessRow[]): Promise<ToolAccess>
  /** The source's rows, read once for every built-in; absent where there are no rows (DEFAULT_ACCESS). */
  rows?(sources: readonly string[]): Promise<AccessRow[]>
}

export type BuiltinTier = { name: string; tier: RiskTier }

/**
 * The built-ins a turn offers, each at its tier: every one whose access is not `off`. `native`
 * are Claude Code's own, `served` the session's (SESSION_BUILTINS), the background ones only
 * with `background` (the session keeps background tasks: ClaimedSession.backgroundTools on an agent with
 * them). `named` is
 * each built-in the turn's allowed packages name, with those packages: a package can switch on
 * one `off` by default, never one a person switched off, and never one off (resolver.ts `native`).
 */
export async function turnBuiltins(
  access: NativeAccess,
  named: readonly { name: string; packages: readonly string[] }[],
  options: { background: boolean },
): Promise<{ native: BuiltinTier[]; served: BuiltinTier[] }> {
  const rows = access.rows ? await access.rows([BUILTIN_SOURCES.claudeCode]) : undefined
  const offered = async (builtins: readonly NativeBuiltin[]) => {
    const out: BuiltinTier[] = []
    for (const b of builtins) {
      const packages = named.find((n) => n.name === b.name)?.packages ?? []
      const now = await access.native(b.name, b.access, packages, rows)
      if (now !== 'off') out.push({ name: b.name, tier: now })
    }
    return out
  }
  return {
    native: await offered(NATIVE_BUILTINS),
    served: await offered(SESSION_BUILTINS.filter((b) => options.background || !b.background)),
  }
}

/** `access` raised by a Monitor call's source: `outward` for `ws`, `shell` (Bash's access now) for `command`. */
export function monitorAccess(access: ToolAccess, input: unknown, shell: ToolAccess | undefined): ToolAccess {
  const given = typeof input === 'object' && input !== null ? (input as Record<string, unknown>) : {}
  // Input not yet validated: a call naming neither source fails, and one naming both is refused as `outward` or by its shell.
  const bySource: ToolAccess[] = []
  if (given.ws !== undefined || given.command === undefined) bySource.push('outward')
  if (given.command !== undefined) bySource.push(shell ?? 'off')
  return strictest([access, ...bySource]) ?? access
}

/**
 * The served built-ins' tiers, by the name Claude Code gives them (`mcp__builtin__<Name>`);
 * a Monitor's by its source (`monitorAccess`), with `shell` the turn's Bash tier (none: off).
 */
export function sessionTierOf(served: readonly BuiltinTier[], shell: RiskTier | undefined): TierResolver {
  const tiers = new Map(served.map((t) => [`${SERVED_PREFIX}${t.name}`, t.tier]))
  return (toolName, input) => {
    const tier = tiers.get(toolName)
    if (tier === undefined || toolName !== `${SERVED_PREFIX}${MONITOR}`) return tier
    const now = monitorAccess(tier, input, shell)
    // A command with Bash off is refused by the tool itself (no command queue); tiered `outward` until then.
    return now === 'off' ? 'outward' : now
  }
}
