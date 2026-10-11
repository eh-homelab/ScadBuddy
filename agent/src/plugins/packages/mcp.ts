import { randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { cp, mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { declaredConfigs } from '../../harness/plugins.js'
import { EgressError, type Resolver, systemResolver } from '../../http/egress.js'
import type { HarnessPlugin, PluginForwarder } from '../forwarder.js'
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
import type { LoadedPackage } from './install.js'

// A plugin package's http MCP servers, reached the way a remote plugin is (#2320):
// through the loopback forwarder (../forwarder.ts), never by the package's URL. Claude
// Code's own MCP client follows redirects and OAuth discovery with the configured header
// (measured, forwarder.ts "WHY"), and resolves the name again after the egress check; the
// forwarder connects to the address the check passed, follows no redirect, turns a 401
// into a 502, adds the header itself and marks every result untrusted.
//
// Each server is a plugin named as Claude Code names a plugin's server,
// `plugin_<package>_<server>`, so its tools keep their harness names
// (`mcp__plugin_<package>_<server>__<tool>`). No package tool is ScadBuddy's, so none
// has a tier listed: every call is outward, as before. vet.ts allows no `$` in a URL or
// header. The package is someone else's repository, so its URL and its one header are
// checked as a remote plugin's are when it is registered (registry.ts
// `normalisePluginUrl`, `validateHeaderName`, `validateSecret`: a token name, none the
// transport sets, no control character), and `X-Forwarded-*` and `Proxy-*` are refused
// too. That header is the registration's own, the one the forwarder adds; the forwarder
// passes no other header from anyone. A server declaring more than one is not offered.

export type PackageServers = {
  plugins: HarnessPlugin[]
  problems: string[]
  /** The header values and the forwarder URLs, for redaction. */
  secrets: string[]
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

/** Each package's http servers as remote plugins, registered with the forwarder; `release()` when the run ends. */
export async function forwardPackageServers(
  loaded: readonly LoadedPackage[],
  forwarder: Pick<PluginForwarder, 'register'>,
  resolve: Resolver = systemResolver,
): Promise<PackageServers> {
  const plugins: HarnessPlugin[] = []
  const problems: string[] = []
  const secrets: string[] = []
  const releases: (() => void)[] = []
  for (const pkg of loaded) {
    for (const { map } of declaredConfigs(pkg.dir).mcp) {
      for (const [server, config] of entries(map)) {
        if (typeof config.url !== 'string' || config.command !== undefined) continue
        const name = packageServerName(pkg.name, server)
        const what = `plugin package ${pkg.name}: MCP server "${server}"`
        if (name.includes('__')) {
          problems.push(`${what} has a name that cannot be offered`)
          continue
        }
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
          url = normalisePluginUrl(config.url)
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
          if (address === undefined) throw new EgressError(`${config.url} resolves to no address`)
        } catch (err) {
          if (!(err instanceof EgressError)) throw err
          problems.push(`${what} is not offered: ${err.message}`)
          continue
        }
        const plugin: RemotePlugin = {
          name,
          url,
          ...(header ? { header } : {}),
          toolTiers: {},
          disabledTools: [],
        }
        const registration = forwarder.register(plugin, address)
        releases.push(registration.release)
        if (header) secrets.push(...headerSecretVariants(header.value))
        secrets.push(registration.url)
        plugins.push({ name, url: registration.url, toolTiers: {}, disabledTools: [] })
      }
    }
  }
  return { plugins, problems, secrets, release: () => releases.forEach((r) => r()) }
}

/** Whether a server entry is reached by URL (http, sse): the forwarder's, never Claude Code's. */
export function isRemoteEntry(config: Record<string, unknown>): boolean {
  return config.command === undefined && (typeof config.url === 'string' || config.type === 'http' || config.type === 'sse')
}

/** Whether a plugin directory declares any server reached by URL. */
export function declaresRemoteServers(dir: string): boolean {
  return declaredConfigs(dir).mcp.some(({ map }) => entries(map).some(([, config]) => isRemoteEntry(config)))
}

function withoutRemote(map: unknown): unknown {
  const nested = isRecord(map) && isRecord(map.mcpServers)
  const servers = nested ? (map.mcpServers as Record<string, unknown>) : map
  if (!isRecord(servers)) return map
  const kept = Object.fromEntries(Object.entries(servers).filter(([, c]) => !(isRecord(c) && isRemoteEntry(c))))
  return nested ? { ...(map as Record<string, unknown>), mcpServers: kept } : kept
}

/**
 * The package as Claude Code may load it when its own MCP configs are read (a query with
 * `strictMcpConfig` off, for the headless browser or an allowed package, harness/run.ts):
 * `dir` itself when it declares no server reached by URL, else a copy under `root`
 * whose `.mcp.json` and manifest keep only its local servers. Its http servers reach
 * the turn through the forwarder instead (`forwardPackageServers`). The copy is named
 * by the content hash and kept: the original is never written to.
 */
export async function withoutRemoteServers(dir: string, contentHash: string, root: string): Promise<string> {
  if (!declaresRemoteServers(dir)) return dir
  const target = path.join(root, contentHash.replace(/^sha256:/, ''))
  if (existsSync(target) && !declaresRemoteServers(target)) return target
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
    await rm(target, { recursive: true, force: true })
    await rename(tmp, target).catch(async (err: NodeJS.ErrnoException) => {
      if ((err.code !== 'ENOTEMPTY' && err.code !== 'EEXIST') || declaresRemoteServers(target)) throw err
    })
  } finally {
    await rm(tmp, { recursive: true, force: true })
  }
  return target
}
