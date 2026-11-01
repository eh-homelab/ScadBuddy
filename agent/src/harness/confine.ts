import { lstat, realpath } from 'node:fs/promises'
import path from 'node:path'
import type { GuardVerdict, InputGuard } from './permissions.js'

// Claude Code's file tools reach only the session's working directory (plan 2026-10-13 Step
// 9.6, decision Q3): the seam (permissions.ts, as both canUseTool and the PreToolUse hook)
// refuses a call whose path resolves outside it, lexically (`..`, an absolute path) or through
// a link (the real path of the target, or of its nearest existing parent for a new file).
//
// Claude Code expands a leading `~` to HOME (the state dir: its config, transcripts and
// credentials). Read and Write do it before the seam, but Glob's and Grep's `path` reach it as
// `~` and the tool expands it after (measured on 2.1.310, test/nativeBuiltins.sdk.test.ts). So
// any path, Glob pattern or Grep glob whose first segment starts with `~` is refused, `~user`
// too (taken as a name in the directory on 2.1.310, but refused in case a later build expands
// it). `$HOME` and `${HOME}` were not expanded either: names in the directory, still allowed.
//
// What remains: a link swapped in between this check and the tool's own open would be
// followed. Only a process the session runs can make one (a Monitor command, an allowed
// package's Bash), and the write-back never follows one (workspace/directory.ts).

/** Each tool's path inputs, and whether the tool needs one. */
const PATHS: Readonly<Record<string, { field: string; required: boolean }>> = {
  Read: { field: 'file_path', required: true },
  Write: { field: 'file_path', required: true },
  Edit: { field: 'file_path', required: true },
  NotebookEdit: { field: 'notebook_path', required: true },
  Glob: { field: 'path', required: false },
  Grep: { field: 'path', required: false },
}

/** The tools this guard confines: offered only to a turn with a working directory. */
export const CONFINED_TOOLS: ReadonlySet<string> = new Set(Object.keys(PATHS))

/** Whether `value` starts at a home directory (`~`, `~/x`, `~user/x`). */
const fromHome = (value: string): boolean => value.startsWith('~')

const GLOB_CHARS = /[*?[\]{}]/

/** Every character glob syntax can wrap a `.` in: escapes, braces, classes, extglobs, wildcards. */
const GLOB_SYNTAX = /[\\{},[\]()|!@+?*]/g
const DOTS_AND_SYNTAX = /^[.\\{},[\]()|!@+?*]+$/

/**
 * Whether a glob could reach `..`, whatever syntax spells it, failing closed: a `..` once the
 * glob syntax is taken out (`.{.,x}`, `.\.`, `[.][.]`, `@(.).`, `{..,x}`), or a segment of
 * only dots and glob syntax other than `.` (`.?`, `.*`, `*.*`). The engine is not relied on.
 */
function mayClimb(glob: string): boolean {
  if (glob.replace(GLOB_SYNTAX, '').includes('..')) return true
  return glob.split('/').some((segment) => segment !== '.' && segment.includes('.') && DOTS_AND_SYNTAX.test(segment))
}

const inside = (root: string, abs: string): boolean => {
  const rel = path.relative(root, abs)
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel))
}

/** The real path of `abs`, or of its nearest existing parent with the rest appended. */
async function realOf(abs: string): Promise<string> {
  let at = abs
  const rest: string[] = []
  for (;;) {
    try {
      await lstat(at)
      return path.join(await realpath(at), ...rest)
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err
      const parent = path.dirname(at)
      if (parent === at) return abs
      rest.unshift(path.basename(at))
      at = parent
    }
  }
}

/**
 * A guard that refuses a file tool's path outside `root` (an absolute, existing directory).
 * With no `root` it refuses every file tool: the turn has no directory to confine them to.
 */
export function confineGuard(root: string | undefined): InputGuard {
  if (root === undefined) {
    return (toolName) =>
      Promise.resolve(PATHS[toolName] ? { deny: `${toolName} is not available: this turn has no working directory.` } : undefined)
  }
  // Resolved once per turn: the session directory is placed before the turn and replaced only
  // between turns (workspace/directory.ts), so it is the same directory for this guard's life.
  let realRoot: Promise<string> | undefined
  const deny = (tool: string, why: string): GuardVerdict => ({
    deny: `${tool} reaches only the session's working directory (${root}): ${why}`,
  })
  const outside = async (value: string): Promise<boolean> => {
    if (fromHome(value)) return true
    const abs = path.resolve(root, value)
    if (!inside(root, abs)) return true
    realRoot ??= realpath(root)
    return !inside(await realRoot, await realOf(abs))
  }
  return async (toolName, input) => {
    const spec = PATHS[toolName]
    if (!spec) return undefined
    const args = typeof input === 'object' && input !== null ? (input as Record<string, unknown>) : {}
    const value = args[spec.field]
    if (value === undefined) {
      if (spec.required) return deny(toolName, `it names no ${spec.field}`)
    } else if (typeof value !== 'string') {
      return deny(toolName, `its ${spec.field} is not a path`)
    } else if (await outside(value)) {
      return deny(toolName, `${value} is outside it`)
    }
    if (toolName === 'Glob') {
      const pattern = args.pattern
      if (typeof pattern !== 'string') return deny(toolName, 'its pattern is not a string')
      // Claude Code 2.1.310's Glob found nothing for `{..,x}/*` (test/nativeBuiltins.sdk.test.ts),
      // but the guard does not rely on the engine. A name with `..` in it (`a..b`) is refused too;
      // `path` can name such a directory instead.
      if (mayClimb(pattern)) return deny(toolName, `its pattern ${pattern} may reach ..`)
      if (fromHome(pattern)) return deny(toolName, `its pattern ${pattern} starts at a home directory`)
      if (path.isAbsolute(pattern)) {
        // The part before the first wildcard is a directory the pattern starts from.
        const parts = pattern.split('/')
        const wild = parts.findIndex((p) => GLOB_CHARS.test(p))
        const fixed = parts.slice(0, wild === -1 ? parts.length : wild).join('/') || '/'
        if (await outside(fixed)) return deny(toolName, `its pattern ${pattern} starts outside it`)
      }
    }
    // Grep's `path` is a path (checked above, no glob); its `glob` filter follows the Glob rule.
    if (toolName === 'Grep' && args.glob !== undefined) {
      if (typeof args.glob !== 'string') return deny(toolName, 'its glob is not a string')
      if (fromHome(args.glob)) return deny(toolName, `its glob ${args.glob} starts at a home directory`)
      if (mayClimb(args.glob)) return deny(toolName, `its glob ${args.glob} may reach ..`)
    }
    return undefined
  }
}
