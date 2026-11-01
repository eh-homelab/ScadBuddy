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
/** What a refused glob's message suggests instead, so the model does not retry the same one. */
const CLIMB_HINT =
  'refused, failing closed: name a directory with `path` instead of `..`, give each part a letter or extension ' +
  '(`**/*.ts`, not `*.*` or `.*`), and escape a literal `(`, `)`, `|`, `{`, `}` or `[` with `\\`'

/** More alternatives than this and the glob is refused rather than read. */
const MAX_ALTERNATIVES = 256

/** The index of the `close` that ends the group opened at `open`, past escapes, classes and nested groups; -1 if none. */
function groupEnd(s: string, open: number, close: '}' | ')'): number {
  let depth = 0
  for (let i = open; i < s.length; i++) {
    const c = s[i]
    if (c === '\\') i++
    else if (c === '[') i = classEnd(s, i)
    else if (c === '{' || c === '(') depth++
    else if (c === close || c === '}' || c === ')') {
      depth--
      if (depth === 0) return c === close ? i : -1
    }
  }
  return -1
}

/** The index of the `]` that ends the class opened at `open`, or `open` when it is a literal `[`. */
function classEnd(s: string, open: number): number {
  let i = open + 1
  if (s[i] === '!' || s[i] === '^') i++
  if (s[i] === ']') i++
  for (; i < s.length; i++) {
    if (s[i] === '\\') i++
    else if (s[i] === '[' && s[i + 1] === ':') {
      const end = s.indexOf(':]', i + 2)
      if (end !== -1) i = end + 1
    } else if (s[i] === ']') return i
  }
  return open
}

/** `s` split on `sep` at its top level (not inside a class, an escape or a nested group). */
function splitTop(s: string, sep: string): string[] {
  const parts: string[] = []
  let depth = 0
  let from = 0
  for (let i = 0; i < s.length; i++) {
    const c = s[i]
    if (c === '\\') i++
    else if (c === '[') i = classEnd(s, i)
    else if (c === '{' || c === '(') depth++
    else if (c === '}' || c === ')') depth--
    else if (c === sep && depth === 0) {
      parts.push(s.slice(from, i))
      from = i + 1
    }
  }
  parts.push(s.slice(from))
  return parts
}

/**
 * The strings a glob's braces and extglobs spell, each still holding its classes and
 * wildcards; `undefined` (refuse) for more than MAX_ALTERNATIVES, an alternative with a `/`
 * in it, or a brace with no comma that holds `..` (a sequence such as `{-..0}` spells `.` and `/`).
 * A repeating extglob (`*(x)`, `+(x)`) is read as none, one and two of each alternative.
 */
function alternatives(glob: string): string[] | undefined {
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i]
    if (c === '\\') {
      i++
      continue
    }
    if (c === '[') {
      i = classEnd(glob, i)
      continue
    }
    const ext = '@+?*!'.includes(c!) && glob[i + 1] === '('
    if (c !== '{' && !ext) continue
    const open = ext ? i + 1 : i
    const end = groupEnd(glob, open, ext ? ')' : '}')
    if (end === -1) return undefined
    const body = glob.slice(open + 1, end)
    const parts = splitTop(body, ext ? '|' : ',')
    if (parts.some((p) => p.includes('/'))) return undefined
    if (!ext && parts.length === 1 && body.includes('..')) return undefined
    let alts: string[]
    if (!ext) alts = parts
    else if (c === '!') alts = ['*']
    else if (c === '@') alts = parts
    else if (c === '?') alts = ['', ...parts]
    else if (c === '+') alts = [...parts, ...parts.map((p) => p + p)]
    else alts = ['', ...parts, ...parts.map((p) => p + p)]
    const prefix = glob.slice(0, i)
    const out: string[] = []
    for (const alt of alts) {
      const rest = alternatives(alt + glob.slice(end + 1))
      if (rest === undefined) return undefined
      for (const r of rest) out.push(prefix + r)
      if (out.length > MAX_ALTERNATIVES) return undefined
    }
    return out
  }
  return [glob]
}

/** Whether a class body (between `[` and `]`) can match `.`: negated, naming it, a range over it, or a POSIX class. */
function classMatchesDot(body: string): boolean {
  if (body.startsWith('!') || body.startsWith('^') || body.includes('.') || body.includes('[:')) return true
  // Every `-` is read as a range, open where it has no neighbour (`[-0]`), failing closed.
  for (let i = 0; i < body.length; i++) {
    if (body[i] !== '-') continue
    const lo = i > 0 ? body.charCodeAt(i - 1) : 0
    const hi = i + 1 < body.length ? body.charCodeAt(i + 1) : 0x10ffff
    if (lo <= 0x2e && hi >= 0x2e) return true
  }
  return false
}

/**
 * Whether a segment (no braces or extglobs left) could be `..`: once each escape is its
 * character, each class that can match `.` a `.`, and `*` and `?` wildcards, it holds only
 * dots and wildcards with a dot among them, and is not a lone `.`.
 */
function segmentMayClimb(segment: string): boolean {
  const tokens: string[] = []
  for (let i = 0; i < segment.length; i++) {
    const c = segment[i]!
    if (c === '\\') {
      i++
      tokens.push(segment[i] === '.' ? '.' : 'x')
    } else if (c === '[' && classEnd(segment, i) !== i) {
      const end = classEnd(segment, i)
      tokens.push(classMatchesDot(segment.slice(i + 1, end)) ? '.' : 'x')
      i = end
    } else if ('(){}|['.includes(c)) {
      // Group syntax left once the readable groups are spelled out, or a `[` that opens no
      // class: an engine may still read it (picomatch takes a top-level `|` as an alternation,
      // `?.|x`; minimatch reads `0.[:0+*\|` as matching `..`), so refuse. `\(` names one.
      return true
    } else if (c === '*' || c === '?') tokens.push('w')
    else tokens.push(c === '.' ? '.' : 'x')
  }
  if (tokens.length === 1 && tokens[0] === '.') return false
  return tokens.includes('.') && tokens.every((t) => t === '.' || t === 'w')
}

/**
 * Whether a glob could reach `..`, whatever syntax spells it, failing closed. Refused: a `..`
 * once the glob syntax is taken out (`.{.,x}`, `.\.`, `[.][.]`); a glob whose braces or
 * extglobs cannot be read (`alternatives`); and one with a segment, in any of its
 * alternatives, that could be `..` (`.{x,.}`, `.[!x]`, `.@(x|.)`, and `.?`, `.*`, `*.*`).
 * The engine is not relied on.
 */
function mayClimb(glob: string): boolean {
  if (glob.replace(GLOB_SYNTAX, '').includes('..')) return true
  const spelled = alternatives(glob)
  if (spelled === undefined) return true
  return spelled.some((s) => s.split('/').some(segmentMayClimb))
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
      if (mayClimb(pattern)) return deny(toolName, `its pattern ${pattern} may reach .. (${CLIMB_HINT})`)
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
      if (mayClimb(args.glob)) return deny(toolName, `its glob ${args.glob} may reach .. (${CLIMB_HINT})`)
    }
    return undefined
  }
}
