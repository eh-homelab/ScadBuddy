import { declaredConfigs } from '../../harness/plugins.js'
import { EgressError, type Resolver, systemResolver } from '../../http/egress.js'
import type { HarnessPlugin, PluginForwarder } from '../forwarder.js'
import { assertEndpointAllowed, harnessToolName, headerSecretVariants, type RemotePlugin } from '../registry.js'
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
// header, so what is declared is what is sent. The forwarder adds one header; a server
// declaring more is not offered.

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
        let address: string | undefined
        try {
          ;[address] = await assertEndpointAllowed(config.url, resolve)
          if (address === undefined) throw new EgressError(`${config.url} resolves to no address`)
        } catch (err) {
          if (!(err instanceof EgressError)) throw err
          problems.push(`${what} is not offered: ${err.message}`)
          continue
        }
        const [header] = headers
        const plugin: RemotePlugin = {
          name,
          url: config.url,
          ...(header ? { header: { name: header[0], value: header[1] } } : {}),
          toolTiers: {},
          disabledTools: [],
        }
        const registration = forwarder.register(plugin, address)
        releases.push(registration.release)
        if (header) secrets.push(...headerSecretVariants(header[1]))
        secrets.push(registration.url)
        plugins.push({ name, url: registration.url, toolTiers: {}, disabledTools: [] })
      }
    }
  }
  return { plugins, problems, secrets, release: () => releases.forEach((r) => r()) }
}
