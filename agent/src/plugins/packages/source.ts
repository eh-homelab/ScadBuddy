import { PluginError } from '../registry.js'

// Where a plugin package comes from (issue #297, "Installing a plugin":
// "A git URL or marketplace entry, pinned to a commit").
//
//   git          a repository, optionally a directory inside it, at a ref
//   marketplace  a repository holding `.claude-plugin/marketplace.json` and
//                the name of one of its entries
//                (https://code.claude.com/docs/en/plugin-marketplaces)
//
// Every value here ends up on a `git` command line (git.ts), so each is held
// to a narrow alphabet rather than escaped: no value may start with `-`
// (it would be read as an option), none may contain `..`, whitespace,
// control characters or `$`.

export type PackageSource =
  | { kind: 'git'; url: string; ref: string; path: string }
  | { kind: 'marketplace'; url: string; ref: string; entry: string }

/** The default ref: the remote's HEAD, whatever its default branch is. */
export const DEFAULT_REF = 'HEAD'

const REF_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/
const PATH_SEGMENT_RE = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$/
export const COMMIT_RE = /^([0-9a-f]{40}|[0-9a-f]{64})$/

/**
 * A repository URL: https (http to loopback is decided by the egress check,
 * `assertEndpointAllowed`), no credentials, no query or fragment, no `$`.
 * ssh, git://, file:// and `ext::` are refused: git would reach them without
 * the egress check.
 */
export function normaliseGitUrl(raw: string): string {
  let url: URL
  try {
    url = new URL(raw.trim())
  } catch {
    throw new PluginError('source url is not a valid URL', 400)
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    throw new PluginError(`source url must be https (or http to loopback), not ${url.protocol}//`, 400)
  }
  if (url.username || url.password) throw new PluginError('source url must not carry credentials', 400)
  if (url.search || url.hash) throw new PluginError('source url must not have a query or fragment', 400)
  const text = url.toString()
  if (text.includes('$')) throw new PluginError('source url must not contain "$"', 400)
  if (text.length > 2048) throw new PluginError('source url is longer than 2048 characters', 400)
  return text
}

/** A branch, tag or commit: git's ref alphabet, minus anything a shell or git option could read. */
export function validateRef(raw: string | undefined): string {
  const ref = (raw ?? DEFAULT_REF).trim()
  if (!REF_RE.test(ref) || ref.includes('..') || ref.includes('//') || ref.endsWith('/') || ref.endsWith('.lock')) {
    throw new PluginError(`ref "${ref}" is not a branch, tag or commit name`, 400)
  }
  return ref
}

/**
 * A directory inside the repository, as `a/b/c` ('' for the root). A leading
 * `./` and trailing `/` are dropped; `..`, absolute paths and hidden (dot)
 * segments are refused.
 */
export function normaliseRepoPath(raw: string | undefined): string {
  const trimmed = (raw ?? '').trim().replace(/^\.\/+/, '').replace(/\/+$/, '')
  if (trimmed === '' || trimmed === '.') return ''
  const segments = trimmed.split('/')
  for (const segment of segments) {
    if (!PATH_SEGMENT_RE.test(segment) || segment === '..' || segment === '.') {
      throw new PluginError(`path "${raw}" is not a directory inside the repository`, 400)
    }
  }
  const out = segments.join('/')
  if (out.length > 512) throw new PluginError('path is longer than 512 characters', 400)
  return out
}

/** A marketplace entry name: the plugin name rules, loosely (checked again as the plugin's name). */
export function validateEntryName(raw: string): string {
  const entry = raw.trim()
  if (!/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(entry)) {
    throw new PluginError(`marketplace entry "${raw}" is not a plugin name`, 400)
  }
  return entry
}

export function validateSource(input: {
  kind: 'git' | 'marketplace'
  url: string
  ref?: string | undefined
  path?: string | undefined
  entry?: string | undefined
}): PackageSource {
  const url = normaliseGitUrl(input.url)
  const ref = validateRef(input.ref)
  if (input.kind === 'git') {
    if (input.entry !== undefined) throw new PluginError('entry is for a marketplace source', 400)
    return { kind: 'git', url, ref, path: normaliseRepoPath(input.path) }
  }
  if (input.path !== undefined) throw new PluginError('path is for a git source; a marketplace entry names its own', 400)
  if (input.entry === undefined) throw new PluginError('a marketplace source needs the entry to install', 400)
  return { kind: 'marketplace', url, ref, entry: validateEntryName(input.entry) }
}
