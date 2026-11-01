import { lstat, realpath } from 'node:fs/promises'
import path from 'node:path'
import type { GuardVerdict, InputGuard } from './permissions.js'

// Claude Code's file tools reach only the session's working directory (plan 2026-10-13 Step
// 9.6, decision Q3): the seam (permissions.ts, as both canUseTool and the PreToolUse hook)
// refuses a call whose path resolves outside it, lexically (`..`, an absolute path) or through
// a link (the real path of the target, or of its nearest existing parent for a new file).
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

const GLOB_CHARS = /[*?[\]{}]/

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

/** A guard that refuses a file tool's path outside `root` (an absolute, existing directory). */
export function confineGuard(root: string): InputGuard {
  let realRoot: Promise<string> | undefined
  const deny = (tool: string, why: string): GuardVerdict => ({
    deny: `${tool} reaches only the session's working directory (${root}): ${why}`,
  })
  const outside = async (value: string): Promise<boolean> => {
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
      if (pattern.split(/[\\/]/).includes('..')) return deny(toolName, `its pattern ${pattern} climbs out with ..`)
      if (path.isAbsolute(pattern)) {
        // The part before the first wildcard is a directory the pattern starts from.
        const parts = pattern.split('/')
        const wild = parts.findIndex((p) => GLOB_CHARS.test(p))
        const fixed = parts.slice(0, wild === -1 ? parts.length : wild).join('/') || '/'
        if (await outside(fixed)) return deny(toolName, `its pattern ${pattern} starts outside it`)
      }
    }
    return undefined
  }
}
