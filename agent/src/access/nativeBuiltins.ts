import type { RiskTier } from '../harness/permissions.js'
import type { ToolAccess } from './model.js'

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
// every session's directory). A sandbox pod (#779) is the follow-up. An `allow_refused`
// package that names one still switches it on (`nativeBuiltinTools`), at `outward`.
//
// WebFetch and WebSearch stay ScadBuddy's egress-checked ones (decision Q2, builtins/web.ts);
// CronCreate, ScheduleWakeup, Monitor, Workflow and TaskStop stay the session's own
// (src/background/). Neither is in this source.

export type NativeBuiltin = { name: string; access: ToolAccess; label: string; description: string }

export const NATIVE_BUILTINS: readonly NativeBuiltin[] = [
  { name: 'Read', access: 'read', label: 'Read a file', description: "Reads a file of the session's working directory." },
  { name: 'Glob', access: 'read', label: 'Find files', description: "Lists the session directory's files that match a glob pattern." },
  { name: 'Grep', access: 'read', label: 'Search files', description: "Searches the session directory's files with a regular expression." },
  { name: 'TaskCreate', access: 'read', label: 'Add a task', description: "Adds a task to the session's task list." },
  { name: 'TaskGet', access: 'read', label: 'Read a task', description: "Reads one task of the session's task list." },
  { name: 'TaskList', access: 'read', label: 'List the tasks', description: "Lists the session's task list." },
  { name: 'TaskUpdate', access: 'read', label: 'Update a task', description: "Changes a task of the session's task list." },
  { name: 'Write', access: 'write', label: 'Write a file', description: "Writes a file in the session's working directory." },
  { name: 'Edit', access: 'write', label: 'Edit a file', description: "Replaces exact text in a file of the session's working directory." },
  { name: 'NotebookEdit', access: 'write', label: 'Edit a notebook', description: "Edits a notebook cell in the session's working directory." },
  {
    name: 'Bash',
    access: 'off',
    label: 'Run a shell command',
    description: 'Runs a shell command in the worker pod. Off: no sandbox can run there yet (#779), so it could read every session and the key.',
  },
  {
    name: 'PowerShell',
    access: 'off',
    label: 'Run a PowerShell command',
    description: 'Runs a PowerShell command in the worker pod. Off, as Bash is.',
  },
]

export const NATIVE_NAMES: ReadonlySet<string> = new Set(NATIVE_BUILTINS.map((b) => b.name))

/** One built-in's access now: its item under `builtin:claude-code`, and the packages that name it (resolver.ts `native`). */
export type NativeAccess = {
  native(name: string, declared: ToolAccess, packages: readonly string[]): Promise<ToolAccess>
}

/**
 * The built-ins a turn offers Claude Code, each at its tier: every one whose access is not
 * `off`. `named` is each built-in the turn's allowed packages name, with those packages: a
 * package can switch an `off` one on, never one off.
 */
export async function nativeBuiltinTools(
  access: NativeAccess,
  named: readonly { name: string; packages: readonly string[] }[],
): Promise<{ name: string; tier: RiskTier }[]> {
  const out: { name: string; tier: RiskTier }[] = []
  for (const b of NATIVE_BUILTINS) {
    const packages = named.find((n) => n.name === b.name)?.packages ?? []
    const now = await access.native(b.name, b.access, packages)
    if (now !== 'off') out.push({ name: b.name, tier: now })
  }
  return out
}
