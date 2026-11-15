import { lstat, realpath } from 'node:fs/promises'
import { tmpdir } from 'node:os'
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
//
// One place outside it is open to Read and Grep only (#5542): this session's own
// `tool-results` directory in the Claude Code config dir. When a tool result is too large to
// return inline, Claude Code saves it there and tells the model to read it. The path is
// `<config dir>/projects/<encoded cwd>/<session id>/tool-results/`. Read from the bundled
// 2.1.314 binary (2026-11-06): the directory is `join(projectDir(cwd), sessionId,
// "tool-results")`, with `cwd` the process's own `process.cwd()` (a real path), the session id
// the root session's (a subagent saves into its parent's directory), and the project directory
// `join(CLAUDE_CONFIG_DIR, "projects", name)`. The name is CLAUDE_CODE_PROJECT_DIR_NAME when set
// (the harness env never sets it), or else the cwd with every character outside `[a-zA-Z0-9]`
// replaced by `-` (`/`, `.` and `_` too), cut to 200 characters plus `-` and a hash when longer
// (`projectDirName`). The session id is the one the run gives Claude Code (`sessionId` on the
// first turn, `resume` after: the same id, test/nativeBuiltins.sdk.test.ts). Everything else in
// the config dir stays refused: config, transcripts, credentials, other sessions' results.
//
// A resumed turn has a second config dir (#6117). To resume from a SessionStore, the Agent SDK
// (`@anthropic-ai/claude-agent-sdk` 0.3.314, the `agent/package.json` pin, `sdk.mjs`) writes the
// transcript into `join(os.tmpdir(), "claude-resume-" + crypto.randomUUID())` and runs Claude
// Code with CLAUDE_CONFIG_DIR set there, so Claude Code saves its results under
// `<temp dir>/claude-resume-<uuid>/projects/<encoded cwd>/<session id>/tool-results/`. Read and
// Grep may reach that directory too: the real path of the target must be under the real temp
// dir, its first segment exactly `claude-resume-` and a lower-case v4 UUID, then the same names
// as above, this session's id only. The rest of that dir (the transcript copy, the copied
// `.credentials.json` and `.claude.json`, other sessions' and other projects' results) stays
// refused.

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
    // Everything else names itself, a `]` that closes no class too (minimatch and picomatch read
    // it as the character; a `[` that opens none is refused above).
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

/**
 * The globs Claude Code's Grep passes to `rg --glob` for its `glob` input. Read from the bundled
 * 2.1.314 binary (the code that builds Grep's rg arguments, 2026-11-03): the value is split on
 * `/\s+/`, a piece holding both `{` and `}` is kept whole, and any other piece is split on every
 * `,`, empty pieces dropped. Each whitespace piece is checked whole and each comma piece too,
 * so the check covers both readings. Even unchecked, a piece only filters the files rg walks
 * under `path` (checked as a path): Claude Code's rg 14.1.1, run in a directory beside a file
 * `secret`, found nothing for `--glob '../*'` or `--glob '../secret'` (measured 2026-11-03).
 */
function grepGlobs(glob: string): string[] {
  const out: string[] = []
  for (const piece of glob.split(/\s+/).filter(Boolean)) {
    out.push(piece)
    if (!(piece.includes('{') && piece.includes('}'))) out.push(...piece.split(',').filter(Boolean))
  }
  return out
}

/** The tools that may read this session's `tool-results` directory: they read and never write. */
const READS_TOOL_RESULTS: ReadonlySet<string> = new Set(['Read', 'Grep'])

/** A session id the harness gives Claude Code: a UUID (sdk.d.ts `sessionId`: "Must be a valid UUID"). */
const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/**
 * The config dir the Agent SDK makes to resume from a SessionStore: `claude-resume-` and a
 * `crypto.randomUUID()` (lower case, version 4), straight under `os.tmpdir()` (SDK 0.3.314).
 */
const RESUME_DIR = /^claude-resume-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

/** Longer names are cut and given a hash (2.1.314). */
const MAX_PROJECT_DIR_NAME = 200

/** The 32-bit string hash Claude Code 2.1.314 adds to a long project directory name. */
function nameHash(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return h
}

/** The name of `cwd`'s directory under `<config dir>/projects/`, as Claude Code 2.1.314 makes it. */
export function projectDirName(cwd: string): string {
  const name = cwd.replace(/[^a-zA-Z0-9]/g, '-')
  if (name.length <= MAX_PROJECT_DIR_NAME) return name
  return `${name.slice(0, MAX_PROJECT_DIR_NAME)}-${Math.abs(nameHash(cwd)).toString(36)}`
}

/** Where this run's Claude Code saves a tool result too large to return inline. */
export type ToolResultsDir = {
  /** CLAUDE_CONFIG_DIR (options.ts `claudeConfigDir`). */
  configDir: string
  /** The session id the run gives Claude Code (`sessionId` or `resume`). */
  sessionId: string
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
 * With `toolResults`, Read and Grep may also reach that session's `tool-results` directory.
 */
export function confineGuard(root: string | undefined, toolResults?: ToolResultsDir): InputGuard {
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
  // The real `tool-results` directory: the config dir's real path, then the names Claude Code
  // uses, none of them resolved. So a link anywhere below the config dir (`tool-results` itself
  // too) moves the target's real path out of it, and the path is refused.
  let realResults: Promise<string | undefined> | undefined
  const resultsDir = async (): Promise<string | undefined> => {
    if (toolResults === undefined || !SESSION_ID.test(toolResults.sessionId)) return undefined
    realRoot ??= realpath(root)
    const cwd = await realRoot
    return path.join(await realpath(toolResults.configDir), 'projects', projectDirName(cwd), toolResults.sessionId, 'tool-results')
  }
  // A resumed turn's config dir (#6117): `<real temp dir>/claude-resume-<uuid>`, any uuid, with
  // the same names below it. Matched on the target's real path, so a link anywhere on the way
  // (the resume dir itself too) moves it out of the pattern, and the path is refused.
  let realTmp: Promise<string | undefined> | undefined
  const inResumeResults = async (real: string): Promise<boolean> => {
    if (toolResults === undefined || !SESSION_ID.test(toolResults.sessionId)) return false
    realTmp ??= realpath(tmpdir()).catch(() => undefined)
    const tmp = await realTmp
    if (tmp === undefined || !inside(tmp, real)) return false
    realRoot ??= realpath(root)
    const [resume, projects, project, session, results] = path.relative(tmp, real).split(path.sep)
    return (
      RESUME_DIR.test(resume ?? '') &&
      projects === 'projects' &&
      project === projectDirName(await realRoot) &&
      session === toolResults.sessionId &&
      results === 'tool-results'
    )
  }
  const inToolResults = async (toolName: string, value: string): Promise<boolean> => {
    if (!READS_TOOL_RESULTS.has(toolName) || fromHome(value) || value.split(/[/\\]/).includes('..')) return false
    realResults ??= resultsDir().catch(() => undefined)
    const dir = await realResults
    const real = await realOf(path.resolve(root, value))
    return (dir !== undefined && inside(dir, real)) || (await inResumeResults(real))
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
    } else if ((await outside(value)) && !(await inToolResults(toolName, value))) {
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
    // Grep's `path` is a path (checked above, no glob); its `glob` filter follows the Glob rule,
    // for the whole value and for each piece Grep makes of it (`grepGlobs`).
    if (toolName === 'Grep' && args.glob !== undefined) {
      if (typeof args.glob !== 'string') return deny(toolName, 'its glob is not a string')
      for (const glob of [args.glob, ...grepGlobs(args.glob)]) {
        if (fromHome(glob)) return deny(toolName, `its glob ${glob} starts at a home directory`)
        if (mayClimb(glob)) return deny(toolName, `its glob ${glob} may reach .. (${CLIMB_HINT})`)
      }
    }
    return undefined
  }
}
