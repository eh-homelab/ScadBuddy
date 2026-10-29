import { randomUUID } from 'node:crypto'
import { packageServerSource } from '../../access/model.js'
import type { AccessResolver } from '../../access/resolver.js'
import { existsSync } from 'node:fs'
import { cp, mkdir, readFile, rename, rm, utimes, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { declaredConfigs, noCommand } from '../../harness/plugins.js'
import { EgressError, type Resolver, systemResolver } from '../../http/egress.js'
import {
  type ForwardOptions,
  type HarnessPlugin,
  type PluginEndpoint,
  pluginEndpoint,
  pluginFingerprint,
  type PluginForwarder,
  type Registration,
} from '../forwarder.js'
import type { RiskTier } from '../../harness/permissions.js'
import {
  assertEndpointAllowed,
  harnessToolName,
  headerSecretVariants,
  normalisePluginUrl,
  PluginError,
  type RemotePlugin,
  validateHeaderName,
  validateSecret,
} from '../registry.js'
import { hashTree, PackageContentError } from './hash.js'
import type { LoadedPackage } from './install.js'
import { packageLedgerId } from '../pluginSessions.js'

// A plugin package's http MCP servers, reached the way a remote plugin is (#2320):
// through the loopback forwarder (../forwarder.ts), never by the package's URL. Claude
// Code's own MCP client follows redirects and OAuth discovery with the configured header
// (measured, forwarder.ts "WHY"), and resolves the name again after the egress check; the
// forwarder connects to the address the check passed, follows no redirect, turns a 401
// into a 502, adds the header itself and marks every result untrusted.
//
// Each server is a plugin named as Claude Code names a plugin's server,
// `plugin_<package>_<server>`, so its tools keep their harness names
// (`mcp__plugin_<package>_<server>__<tool>`). Its tools' tiers and disabled tools are
// the `package:<pkg>/mcp:<server>` rows in ai_access (#2171); a tool with none stored,
// or every tool without the resolver, is outward; a server whose `*` is off there is not
// offered, nor servers whose names come out equal (`a.b` and `a_b`, #4757). vet.ts
// allows no `$` in a URL or header. The package is someone else's repository, so its URL
// and its one header are checked as a remote plugin's are when it is registered (registry.ts
// `normalisePluginUrl`, `validateHeaderName`, `validateSecret`: a token name, none the
// transport sets, no control character), and `X-Forwarded-*` and `Proxy-*` are refused
// too. That header is the registration's own, the one the forwarder adds; the forwarder
// passes no other header from anyone. A server declaring more than one is not offered.

export type PackageServers = {
  plugins: HarnessPlugin[]
  /** Each of `plugins`, in order: its package and server, whose ai_access source the seam reads per call (access/live.ts). */
  servers: { name: string; pkg: string; server: string }[]
  /** Each server's `pluginEndpoint` (its declared URL and header), in `plugins` order. */
  endpoints?: PluginEndpoint[]
  problems: string[]
  /** The header values and the forwarder URLs, for redaction. */
  secrets: string[]
  /**
   * A server's route for a client that outlives this run (#2581, a durable session's kept
   * MCP client), as forwarder.ts `PluginsForRun.keep`.
   */
  keep?(name: string): { fingerprint: string; register(): Registration } | undefined
  /** Ends the servers' upstream MCP sessions, then releases their routes (forwarder.ts `Registration.terminate`). */
  terminate(): Promise<void>
  release(): void
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function entries(map: unknown): [string, Record<string, unknown>][] {
  const servers = isRecord(map) && isRecord(map.mcpServers) ? map.mcpServers : map
  return isRecord(servers)
    ? Object.entries(servers).filter((e): e is [string, Record<string, unknown>] => isRecord(e[1]))
    : []
}

/** The name a package's server's tools carry: `plugin_<package>_<server>`. */
export function packageServerName(pkg: string, server: string): string {
  return `plugin_${harnessToolName(pkg)}_${harnessToolName(server)}`
}

/**
 * Every server a package offers a turn: its http servers (reached through the forwarder)
 * and, for a package approved with `allow_refused`, its local (stdio) ones.
 */
function offeredServers(loaded: readonly LoadedPackage[]): { pkg: LoadedPackage; server: string; config: Record<string, unknown>; local: boolean }[] {
  const out: { pkg: LoadedPackage; server: string; config: Record<string, unknown>; local: boolean }[] = []
  for (const pkg of loaded) {
    for (const { map } of declaredConfigs(pkg.dir).mcp) {
      for (const [server, config] of entries(map)) {
        if (typeof config.url === 'string' && noCommand(config)) out.push({ pkg, server, config, local: false })
        else if (pkg.allowed && typeof config.command === 'string') out.push({ pkg, server, config, local: true })
      }
    }
  }
  return out
}

/** Every server a package offers a turn (`offeredServers`), by the name its tools carry. */
export function declaredPackageServers(loaded: readonly LoadedPackage[]): { name: string; pkg: string; server: string; local: boolean }[] {
  return offeredServers(loaded).map(({ pkg, server, local }) => ({ name: packageServerName(pkg.name, server), pkg: pkg.name, server, local }))
}

/**
 * The server names (`packageServerName`) more than one package server would carry, http
 * and local alike (`a.b`'s `s` and `a_b`'s `s` are both `plugin_a_b_s`), with their owners.
 * None of them is offered: one name would stand for two servers.
 */
export function collidingServerNames(loaded: readonly LoadedPackage[]): Map<string, { pkg: string; server: string }[]> {
  const owners = new Map<string, { pkg: string; server: string }[]>()
  for (const { pkg, server } of offeredServers(loaded)) {
    const name = packageServerName(pkg.name, server)
    const list = owners.get(name) ?? []
    if (!list.some((o) => o.pkg === pkg.name && o.server === server)) list.push({ pkg: pkg.name, server })
    owners.set(name, list)
  }
  return new Map([...owners].filter(([, list]) => list.length > 1))
}

/** Why a server named like another is not offered, or undefined when its name is its own. */
export function collisionProblem(colliding: ReadonlyMap<string, { pkg: string; server: string }[]>, pkg: string, server: string): string | undefined {
  const name = packageServerName(pkg, server)
  const owners = colliding.get(name)
  if (!owners) return undefined
  const others = owners
    .filter((o) => !(o.pkg === pkg && o.server === server))
    .map((o) => `"${o.server}" of plugin package ${o.pkg}`)
    .join(', ')
  return `plugin package ${pkg}: MCP server "${server}" is named ${name}, as ${others} are, so none of them is offered`
}

/** A package server's switch and stored tool access (`package:<pkg>/mcp:<server>` in ai_access). */
export type ServerAccess = { enabled: boolean; toolTiers: Record<string, RiskTier>; disabledTools: string[] }

/**
 * Every offered server's access, by `packageServerSource`, in one read; without the
 * resolver every server is at its default (on, every tool outward).
 */
export async function serverAccess(
  loaded: readonly LoadedPackage[],
  access?: Pick<AccessResolver, 'pluginSources'>,
): Promise<Map<string, ServerAccess>> {
  const sources = [...new Set(offeredServers(loaded).map(({ pkg, server }) => packageServerSource(pkg.name, server)))]
  if (access) return access.pluginSources(sources)
  return new Map(sources.map((s) => [s, { enabled: true, toolTiers: {}, disabledTools: [] }]))
}

/** A package's http server that passed its checks, as the forwarder registers it. */
export type PackageServerConfig = { pkg: string; server: string; name: string; plugin: RemotePlugin; address: string }

/**
 * Each package's http servers that pass their checks, with their tiers and disabled tools
 * from ai_access. A server switched off (`*` off) is left out and says nothing; one whose
 * name another server would carry too is left out, and each such says why.
 */
export async function packageServerConfigs(
  loaded: readonly LoadedPackage[],
  resolve: Resolver,
  access?: Pick<AccessResolver, 'plugin' | 'enabled'>,
): Promise<{ configs: PackageServerConfig[]; problems: string[] }> {
  const configs: PackageServerConfig[] = []
  const problems: string[] = []
  const colliding = collidingServerNames(loaded)
  for (const { pkg, server, config, local } of offeredServers(loaded)) {
    if (local) continue
    const name = packageServerName(pkg.name, server)
    const what = `plugin package ${pkg.name}: MCP server "${server}"`
    if (name.includes('__')) {
      problems.push(`${what} has a name that cannot be offered`)
      continue
    }
    const collision = collisionProblem(colliding, pkg.name, server)
    if (collision) {
      problems.push(collision)
      continue
    }
    const source = packageServerSource(pkg.name, server)
    if (access && !(await access.enabled(source))) continue
    const headers = isRecord(config.headers)
      ? Object.entries(config.headers).filter((e): e is [string, string] => typeof e[1] === 'string')
      : []
    if (headers.length > 1) {
      problems.push(`${what} declares ${headers.length} headers; only one is sent, through the forwarder`)
      continue
    }
    let url: string
    let header: { name: string; value: string } | undefined
    try {
      url = normalisePluginUrl(config.url as string)
      if (headers[0]) {
        const name = validateHeaderName(headers[0][0])
        if (/^(x-forwarded-|proxy-)/i.test(name)) throw new PluginError(`header "${name}" is set by proxies, not a plugin`, 400)
        header = { name, value: validateSecret(headers[0][1]) }
      }
    } catch (err) {
      if (!(err instanceof PluginError)) throw err
      problems.push(`${what} is not offered: ${err.message}`)
      continue
    }
    let address: string | undefined
    try {
      ;[address] = await assertEndpointAllowed(url, resolve)
      if (address === undefined) throw new EgressError(`${config.url as string} resolves to no address`)
    } catch (err) {
      if (!(err instanceof EgressError)) throw err
      problems.push(`${what} is not offered: ${err.message}`)
      continue
    }
    const { toolTiers, disabledTools } = access ? await access.plugin(source) : { toolTiers: {}, disabledTools: [] }
    configs.push({ pkg: pkg.name, server, name, plugin: { name, url, ...(header ? { header } : {}), toolTiers, disabledTools }, address })
  }
  return { configs, problems }
}

/** Each package's http servers as remote plugins, registered with the forwarder; `release()` when the run ends. */
export async function forwardPackageServers(
  loaded: readonly LoadedPackage[],
  forwarder: Pick<PluginForwarder, 'register'>,
  resolve: Resolver = systemResolver,
  options: Pick<ForwardOptions, 'record'> & { access?: Pick<AccessResolver, 'plugin' | 'enabled'> } = {},
): Promise<PackageServers> {
  const { access, ...forwardOptions } = options
  const { configs, problems } = await packageServerConfigs(loaded, resolve, access)
  // Its `*` switch at each call, on every path that reaches it (#4757): a classic or
  // durable turn's, a flow's. A read that fails refuses the call (forwarder.ts).
  const routeOptions = ({ pkg, server, name }: PackageServerConfig): ForwardOptions => {
    const source = packageServerSource(pkg, server)
    const callCheck = access
      ? async () => ((await access.enabled(source)) ? undefined : `${server} of plugin package ${pkg} is switched off in Settings → Tools & plugins`)
      : undefined
    return { ledgerId: packageLedgerId(name), ...(callCheck ? { callCheck } : {}) }
  }
  const registrations = configs.map((config) => ({
    config,
    registration: forwarder.register(config.plugin, config.address, { ...forwardOptions, ...routeOptions(config) }),
  }))
  const secrets: string[] = []
  for (const { config, registration } of registrations) {
    if (config.plugin.header) secrets.push(...headerSecretVariants(config.plugin.header.value))
    secrets.push(registration.url)
  }
  return {
    plugins: registrations.map(({ config: { plugin }, registration }) => ({
      name: plugin.name,
      url: registration.url,
      toolTiers: plugin.toolTiers,
      disabledTools: plugin.disabledTools,
    })),
    servers: configs.map(({ name, pkg, server }) => ({ name, pkg, server })),
    endpoints: configs.map(({ plugin }) => pluginEndpoint(plugin)),
    problems,
    secrets,
    // As forwardForRun's: a route of its own for a durable session's kept client (#2581).
    keep: (name) => {
      const config = configs.find((c) => c.name === name)
      return config
        ? {
            fingerprint: pluginFingerprint({ plugin: config.plugin, address: config.address }),
            register: () => forwarder.register(config.plugin, config.address, { record: true, ...routeOptions(config) }),
          }
        : undefined
    },
    terminate: async () => {
      await Promise.all(registrations.map(({ registration }) => registration.terminate()))
    },
    release: () => registrations.forEach(({ registration }) => registration.release()),
  }
}

/** Whether a server entry is reached by URL (http, sse): the forwarder's, never Claude Code's. */
export function isRemoteEntry(config: Record<string, unknown>): boolean {
  return noCommand(config) && (typeof config.url === 'string' || config.type === 'http' || config.type === 'sse')
}

/** Whether a plugin directory declares any server reached by URL. */
export function declaresRemoteServers(dir: string): boolean {
  return declaredConfigs(dir).mcp.some(({ map }) => entries(map).some(([, config]) => isRemoteEntry(config)))
}

async function hashesTo(dir: string, hash: string): Promise<boolean> {
  if (!existsSync(dir)) return false
  try {
    return (await hashTree(dir)).hash === hash
  } catch (err) {
    if (err instanceof PackageContentError || (err as NodeJS.ErrnoException).code === 'ENOENT') return false
    throw err
  }
}

function withoutRemote(map: unknown): unknown {
  // A manifest's mcpServers may be a list of config paths and inline maps (harness/plugins.ts).
  if (Array.isArray(map)) return map.map((entry: unknown) => (typeof entry === 'string' ? entry : withoutRemote(entry)))
  const nested = isRecord(map) && isRecord(map.mcpServers)
  const servers = nested ? (map.mcpServers as Record<string, unknown>) : map
  if (!isRecord(servers)) return map
  const kept = Object.fromEntries(Object.entries(servers).filter(([, c]) => !(isRecord(c) && isRemoteEntry(c))))
  return nested ? { ...(map as Record<string, unknown>), mcpServers: kept } : kept
}

/**
 * The package as Claude Code may load it when its own MCP configs are read (a query with
 * `strictMcpConfig` off, for an allowed package, harness/run.ts):
 * `dir` itself when it declares no server reached by URL, else a copy under `root`
 * whose `.mcp.json` and manifest keep only its local servers. Its http servers reach
 * the turn through the forwarder instead (`forwardPackageServers`). The copy is named
 * by the content hash and made again from `dir` (re-hashed by its loader) on every
 * load: one already there is kept only while it hashes to the fresh copy, so it is
 * never trusted as it is. The original is never written to.
 */
/** Marks a package copy as just used: its folder's mtime is what turnPackages.ts `sweepPackageCopies` reads. */
export async function touch(dir: string): Promise<void> {
  const now = new Date()
  await utimes(dir, now, now).catch(() => {})
}

export async function withoutRemoteServers(dir: string, contentHash: string, root: string): Promise<string> {
  if (!declaresRemoteServers(dir)) return dir
  const target = path.join(root, contentHash.replace(/^sha256:/, ''))
  await mkdir(root, { recursive: true })
  const tmp = path.join(root, `.tmp-${randomUUID()}`)
  try {
    await cp(dir, tmp, { recursive: true })
    for (const { where, map } of declaredConfigs(tmp).mcp) {
      if (where === '.mcp.json') {
        await writeFile(path.join(tmp, '.mcp.json'), JSON.stringify(withoutRemote(map)))
      } else if (where === 'plugin.json mcpServers') {
        const file = path.join(tmp, '.claude-plugin', 'plugin.json')
        const manifest = JSON.parse(await readFile(file, 'utf8')) as Record<string, unknown>
        await writeFile(file, JSON.stringify({ ...manifest, mcpServers: withoutRemote(manifest.mcpServers) }))
      } else {
        // A config file the manifest names: rewritten in place in the copy.
        await writeFile(path.join(tmp, where), JSON.stringify(withoutRemote(map)))
      }
    }
    if (declaresRemoteServers(tmp)) throw new Error(`could not strip the http MCP servers of ${dir}`)
    const want = (await hashTree(tmp)).hash
    // Its last use, which turnPackages.ts `sweepPackageCopies` keeps it by (#2395),
    // touched before it is checked, as sharePackage does.
    await touch(target)
    if (await hashesTo(target, want)) return target
    await rm(target, { recursive: true, force: true })
    await rename(tmp, target).catch(async (err: NodeJS.ErrnoException) => {
      // Another turn placed it first: fine when it is the same copy.
      if ((err.code !== 'ENOTEMPTY' && err.code !== 'EEXIST') || !(await hashesTo(target, want))) throw err
    })
  } finally {
    await rm(tmp, { recursive: true, force: true })
  }
  return target
}
