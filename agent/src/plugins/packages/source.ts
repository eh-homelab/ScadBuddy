import { isLoopbackHost } from '../../http/egress.js'
import { PLUGIN_NAME_RE, PluginError } from '../registry.js'

// Where a plugin package comes from (issue #297, "Installing a plugin":
// "A git URL or marketplace entry, pinned to a commit"; #6935: any URL Claude Code takes,
// https://code.claude.com/docs/en/plugins/marketplace-reference.md, "Plugin sources").
//
//   git              a repository, optionally a directory inside it, at a ref
//   marketplace      a repository holding `.claude-plugin/marketplace.json` and
//                    the name of one of its entries
//                    (https://code.claude.com/docs/en/plugin-marketplaces)
//   marketplace-url  a bare URL of a `marketplace.json` and the name of one of its
//                    entries (https://code.claude.com/docs/en/plugins/host-marketplace.md:
//                    "Claude Code downloads only that file")
//   archive          a zip (or tar) URL; `ref` is the `sha256:<hex>` the file must
//                    have, or ''
//   npm              a package from an npm registry: `url` is `<registry>/<name>` (a
//                    scope's `/` as `%2f`, as the registry takes it) and `ref` its
//                    version or dist-tag; or an https tarball link, with `ref` ''
//   hosted           a ScadBuddy hosted plugin (#6934, spec 2026-11-21 §6.3 step 5, §6.5):
//                    always its latest published version, read with the zip over the API's
//                    CRUD area `hosted-plugins` (hosted.ts), pinned to that version
//
// A git and a marketplace source are pinned to a commit; archive and npm ones, and a
// marketplace entry of either kind, to the sha256 of the file fetched at install (pin
// kind `bytes`, install.ts).
//
// Every git value here ends up on a `git` command line (git.ts), so each is held
// to a narrow alphabet rather than escaped: no value may start with `-`
// (it would be read as an option), none may contain `..`, whitespace,
// control characters or `$`. The other kinds are fetched over HTTP only (download.ts).

export type PackageSource =
  | { kind: 'git'; url: string; ref: string; path: string }
  | { kind: 'marketplace'; url: string; ref: string; entry: string }
  | { kind: 'marketplace-url'; url: string; ref: string; entry: string }
  | { kind: 'archive'; url: string; ref: string }
  | { kind: 'npm'; url: string; ref: string }
  | { kind: 'hosted'; name: string }

export type SourceKind = PackageSource['kind']

/** The kinds pinned to a commit; the others are pinned to bytes. */
export function isGitSource(source: PackageSource): source is Extract<PackageSource, { kind: 'git' | 'marketplace' }> {
  return source.kind === 'git' || source.kind === 'marketplace'
}

/** The default ref: the remote's HEAD, whatever its default branch is. */
export const DEFAULT_REF = 'HEAD'
/** The npm registry a package name is fetched from when none is named. */
export const NPM_REGISTRY = 'https://registry.npmjs.org'

const REF_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/
const PATH_SEGMENT_RE = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$/
export const COMMIT_RE = /^([0-9a-f]{40}|[0-9a-f]{64})$/
export const SHA256_RE = /^[0-9a-f]{64}$/
// npm's own name rules (https://github.com/npm/validate-npm-package-name): lowercase,
// URL-safe, at most 214 characters, an optional scope.
const NPM_NAME_RE = /^(@[a-z0-9][a-z0-9._~-]*\/)?[a-z0-9][a-z0-9._~-]*$/
// An exact version (https://semver.org) or a dist-tag. A range (`^2.0.0`) is refused: the
// registry answers a version or a tag, and a pin is one file.
const NPM_VERSION_RE = /^(\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?|[A-Za-z][A-Za-z0-9._-]{0,63})$/
/**
 * Hosts whose tarball links Claude Code refuses for an `npm` source ("A tarball link on
 * github.com, gist.github.com, gitlab.com, bitbucket.org, or git.sr.ht: refused even when
 * the link is a GitHub release download, unless it's a GitLab npm registry link under
 * `gitlab.com/api/v4/`", marketplace-reference "npm plugin source").
 */
const NPM_REFUSED_HOSTS = new Set(['github.com', 'gist.github.com', 'gitlab.com', 'bitbucket.org', 'git.sr.ht'])

/**
 * A URL: https (http to loopback or a private address is decided by the egress check,
 * `assertEndpointAllowed`), no credentials, no fragment, no `$`; a query only when
 * `query` allows it (never for a repository: git would read it). ssh, git://, file://
 * and `ext::` are refused: nothing would reach them through the egress check. With
 * `loopbackHttp` (every file ScadBuddy downloads itself: a bare marketplace URL, an
 * archive, an npm registry or tarball, spec §6.6 "https only"), plain http is taken for a
 * loopback host only.
 */
export function normaliseUrl(raw: string, what = 'source url', options: { query?: boolean; loopbackHttp?: boolean } = {}): string {
  let url: URL
  try {
    url = new URL(raw.trim())
  } catch {
    throw new PluginError(`${what} is not a valid URL`, 400)
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    throw new PluginError(`${what} must be https (or http to loopback), not ${url.protocol}//`, 400)
  }
  if (options.loopbackHttp && url.protocol === 'http:' && !isLoopbackHost(url.hostname)) {
    throw new PluginError(`${what} must be https (plain http is allowed for loopback only)`, 400)
  }
  if (url.username || url.password) throw new PluginError(`${what} must not carry credentials`, 400)
  if ((url.search && !options.query) || url.hash) {
    throw new PluginError(`${what} must not have a ${url.hash ? 'fragment' : 'query'}`, 400)
  }
  const text = url.toString()
  if (text.includes('$')) throw new PluginError(`${what} must not contain "$"`, 400)
  if (text.length > 2048) throw new PluginError(`${what} is longer than 2048 characters`, 400)
  return text
}

/** A repository URL (`normaliseUrl` with no query). */
export function normaliseGitUrl(raw: string): string {
  return normaliseUrl(raw)
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

/** An archive's expected digest, as `sha256:<lowercase hex>` ('' for none); 64 hex characters of either case are taken. */
export function validateArchiveSha(raw: string | undefined): string {
  const value = (raw ?? '').trim().toLowerCase().replace(/^sha256:/, '')
  if (value === '') return ''
  if (!SHA256_RE.test(value)) throw new PluginError('sha256 is not 64 hexadecimal characters', 400)
  return `sha256:${value}`
}

/** An npm version or dist-tag (`latest` for none). */
export function validateNpmVersion(raw: string | undefined): string {
  const version = (raw ?? 'latest').trim()
  if (!NPM_VERSION_RE.test(version)) {
    throw new PluginError(`npm version "${raw}" is not an exact version or a dist-tag (ranges are refused)`, 400)
  }
  return version
}

/** An https tarball link of an npm source, refused where Claude Code refuses it. */
function npmTarballUrl(raw: string): string {
  const url = new URL(normaliseUrl(raw, 'npm package link', { query: true, loopbackHttp: true }))
  const host = url.hostname.toLowerCase()
  if (NPM_REFUSED_HOSTS.has(host) && !(host === 'gitlab.com' && url.pathname.startsWith('/api/v4/'))) {
    throw new PluginError(`a tarball link on ${host} is refused; use a github, url or git-subdir source for a repository`, 400)
  }
  return url.toString()
}

/**
 * The npm source for `pkg`: a registry name (optionally `name@version`) with `version`
 * and `registry`, or an https tarball link. A git address, a folder, `file:` and `npm:`
 * aliases are refused, as Claude Code refuses them.
 */
export function npmSource(pkg: string, version?: string, registry?: string): Extract<PackageSource, { kind: 'npm' }> {
  const value = pkg.trim()
  if (/^https?:\/\//i.test(value)) {
    if (version !== undefined || registry !== undefined) throw new PluginError('a tarball link takes no version or registry', 400)
    return { kind: 'npm', url: npmTarballUrl(value), ref: '' }
  }
  const at = value.lastIndexOf('@')
  const [name, inline] = at > 0 ? [value.slice(0, at), value.slice(at + 1)] : [value, undefined]
  if (name.length > 214 || !NPM_NAME_RE.test(name) || name.includes('..')) {
    throw new PluginError(`npm package "${pkg}" is not a registry package name or an https tarball link`, 400)
  }
  if (inline !== undefined && version !== undefined) throw new PluginError('name the npm version once', 400)
  const base = normaliseUrl(registry ?? NPM_REGISTRY, 'npm registry', { loopbackHttp: true }).replace(/\/+$/, '')
  return { kind: 'npm', url: `${base}/${name.replace('/', '%2f')}`, ref: validateNpmVersion(inline ?? version) }
}

/** An npm source as stored (`url`, `ref`), checked again: a registry package or a tarball link. */
function storedNpm(url: string, ref: string): Extract<PackageSource, { kind: 'npm' }> {
  if (ref === '') return { kind: 'npm', url: npmTarballUrl(url), ref: '' }
  const parsed = new URL(normaliseUrl(url, 'npm package', { loopbackHttp: true }))
  const slash = parsed.pathname.lastIndexOf('/')
  const name = decodeURIComponent(parsed.pathname.slice(slash + 1))
  const registry = `${parsed.origin}${parsed.pathname.slice(0, slash)}`
  return npmSource(name, ref, registry)
}

export type SourceInput = {
  kind: SourceKind
  name?: string | undefined
  url?: string | undefined
  ref?: string | undefined
  path?: string | undefined
  entry?: string | undefined
  sha256?: string | undefined
  package?: string | undefined
  version?: string | undefined
  registry?: string | undefined
}

/**
 * A source as the routes take it, or as it is stored (both are accepted, so a command's
 * check can validate what its route already did): every value checked and normalised.
 */
export function validateSource(input: SourceInput): PackageSource {
  const only = (allowed: readonly (keyof SourceInput)[]) => {
    for (const key of ['ref', 'path', 'entry', 'sha256', 'package', 'version', 'registry', 'name'] as const) {
      if (input[key] !== undefined && !allowed.includes(key)) throw new PluginError(`${key} is not for a ${input.kind} source`, 400)
    }
  }
  const url = () => {
    if (input.url === undefined) throw new PluginError(`a ${input.kind} source needs its url`, 400)
    return input.url
  }
  switch (input.kind) {
    case 'git':
      only(['ref', 'path'])
      return { kind: 'git', url: normaliseGitUrl(url()), ref: validateRef(input.ref), path: normaliseRepoPath(input.path) }
    case 'marketplace':
      if (input.path !== undefined) throw new PluginError('path is for a git source; a marketplace entry names its own', 400)
      only(['ref', 'entry'])
      if (input.entry === undefined) throw new PluginError('a marketplace source needs the entry to install', 400)
      return { kind: 'marketplace', url: normaliseGitUrl(url()), ref: validateRef(input.ref), entry: validateEntryName(input.entry) }
    case 'marketplace-url':
      only(['entry', ...(input.ref === '' ? (['ref'] as const) : [])])
      if (input.entry === undefined) throw new PluginError('a marketplace source needs the entry to install', 400)
      return {
        kind: 'marketplace-url',
        url: normaliseUrl(url(), 'marketplace url', { query: true, loopbackHttp: true }),
        ref: '',
        entry: validateEntryName(input.entry),
      }
    case 'archive': {
      only(['sha256', 'ref'])
      if (input.sha256 !== undefined && input.ref !== undefined) throw new PluginError('name the archive sha256 once', 400)
      return { kind: 'archive', url: normaliseUrl(url(), 'archive url', { query: true, loopbackHttp: true }), ref: validateArchiveSha(input.sha256 ?? input.ref) }
    }
    case 'npm':
      if (input.package !== undefined) {
        only(['package', 'version', 'registry'])
        if (input.url !== undefined) throw new PluginError('an npm source names its package, not a url', 400)
        return npmSource(input.package, input.version, input.registry)
      }
      only(['ref'])
      return storedNpm(url(), input.ref ?? '')
    case 'hosted':
      only(['name'])
      if (input.url !== undefined) throw new PluginError('a hosted source names its plugin, not a url', 400)
      return { kind: 'hosted', name: validateHostedName(input.name) }
    default:
      throw new PluginError(`source kind ${JSON.stringify((input as { kind: unknown }).kind)} is not one ScadBuddy installs`, 400)
  }
}

/** A hosted plugin's name: the hosted plugins' alphabet (backend hosted_plugins/rules.py `NAME_RE`). */
export function validateHostedName(raw: string | undefined): string {
  const name = (raw ?? '').trim()
  if (!PLUGIN_NAME_RE.test(name) || name.includes('--')) {
    throw new PluginError(`"${raw ?? ''}" is not a hosted plugin name`, 400)
  }
  return name
}

/** The columns a source is stored in (`ai_plugin_packages` `source_url`, `source_ref`). A hosted source has no URL: it is `hosted:<name>`. */
export function sourceColumns(source: PackageSource): { url: string; ref: string } {
  return source.kind === 'hosted' ? { url: hostedUrl(source.name), ref: '' } : { url: source.url, ref: source.ref }
}

/** Where a hosted package's files come from, as its `fetch_url`: no URL is fetched, the zip is read over CRUD. */
export function hostedUrl(name: string): string {
  return `hosted:${name}`
}

/** The `reason` of a re-pin refused by `RepinFixedError`. */
export const REPIN_FIXED = 'repin-fixed-sha256'

/**
 * An archive installed with its `sha256`: a re-pin fetches the same URL and must find the
 * same bytes, so it could only answer "already pinned" or a sha256 mismatch. Refused 409
 * `repin-fixed-sha256` before anything is fetched.
 */
export class RepinFixedError extends PluginError {
  override name = 'RepinFixedError'
  constructor() {
    super(
      'this archive was installed with its sha256, so a re-pin could only fetch the same file; remove the package and install the new file',
      409,
    )
  }
}

/**
 * `current` with the ref a re-pin asks for: a branch, tag or commit for a repository
 * source, a version or dist-tag for a registry npm package. The other kinds have no ref
 * to move: a re-pin fetches their URL again (a hosted plugin's latest version), except an
 * archive installed with its sha256 (`RepinFixedError`).
 */
export function withRepinRef(current: PackageSource, ref: string | undefined): PackageSource {
  if (current.kind === 'archive' && current.ref !== '') throw new RepinFixedError()
  if (isGitSource(current)) return { ...current, ref: validateRef(ref ?? current.ref) }
  if (current.kind === 'npm' && current.ref !== '') return { ...current, ref: validateNpmVersion(ref ?? current.ref) }
  if (ref !== undefined) {
    const what = current.kind === 'npm' ? 'tarball link' : current.kind === 'hosted' ? 'hosted plugin (it takes its latest version)' : current.kind
    throw new PluginError(`a ${what} source has no ref to re-pin to`, 400)
  }
  return current
}
