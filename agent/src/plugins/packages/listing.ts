import type { PackageListingPut, ToolListings } from '../../access/listings.js'
import { packageServerSource } from '../../access/model.js'
import { type Resolver, systemResolver } from '../../http/egress.js'
import type { PluginForwarder } from '../forwarder.js'
import { PluginError } from '../registry.js'
import { DEFAULT_PLUGIN_TEST_TIMEOUT_MS, type PluginTest, testPlugin } from '../testConnection.js'
import { type LoadedPackage, type PackageInstaller, PackageRefusedError } from './install.js'
import { collisionProblem, offeredServers, packageServerConfigs, packageServerName } from './mcp.js'
import type { PackageRepo } from './store.js'
import type { PackageReview } from './vet.js'

// Listing a plugin package's http MCP server (#4757): Settings → Tools & plugins' refresh of a
// `package:<pkg>/mcp:<server>` source, as a remote plugin's test lists its tools (routes/plugins.ts
// `testAndList`). The pin is acquired as a turn loads it (install.ts), the server checked as a turn
// offers it (mcp.ts `packageServerConfigs`), and one `tools/list` made through the forwarder at the
// address the egress check passed. A local (stdio) server is never listed here: listing it would
// start the package's own process outside a turn.

/** The servers a package's review says a turn offers: its http ones, and its local ones while it is allowed. */
export function reviewServers(review: PackageReview, allowed: boolean): { server: string; local: boolean }[] {
  const out: { server: string; local: boolean }[] = []
  for (const { name } of review.mcp_servers) if (!out.some((s) => s.server === name)) out.push({ server: name, local: false })
  if (allowed) for (const name of review.local_servers ?? []) if (!out.some((s) => s.server === name)) out.push({ server: name, local: true })
  return out
}

/**
 * The server names (`packageServerName`) more than one of `servers` would carry, with their
 * owners: mcp.ts `collidingServerNames` over what the packages a turn loads offer.
 */
export function serverCollisions(servers: readonly { pkg: string; server: string }[]): Map<string, { pkg: string; server: string }[]> {
  const owners = new Map<string, { pkg: string; server: string }[]>()
  for (const { pkg, server } of servers) {
    const name = packageServerName(pkg, server)
    const list = owners.get(name) ?? []
    if (!list.some((o) => o.pkg === pkg && o.server === server)) list.push({ pkg, server })
    owners.set(name, list)
  }
  return new Map([...owners].filter(([, list]) => list.length > 1))
}

// One listing per server at a time in this process; ToolListings.exclusiveServer holds it across replicas.
const listing = new Set<string>()

// As routes/plugins.ts: a test ends within its timeout, and 2 s more covers the listing's write.
const RETRY_AFTER_S = Math.ceil(DEFAULT_PLUGIN_TEST_TIMEOUT_MS / 1000) + 2

export type PackageListingAnswer = { status: 200 | 404 | 409 | 429 | 502; body: PluginTest | { detail: string }; retryAfter?: number }

const refusal = (status: 404 | 409 | 502, detail: string): PackageListingAnswer => ({ status, body: { detail } })

/**
 * Lists package `pkg`'s server `server` and, on a good test, stores its tools under
 * `package:<pkg>/mcp:<server>`; a failed test stores nothing. 429 while another listing of it
 * runs; 404 for an unknown package or server; 409 for an unapproved pin, a pin refused at
 * load, a local server, and a server a turn would not offer (its problem: named as another,
 * a name that cannot be offered, a URL the egress check refuses).
 */
export async function listPackageServer(
  deps: {
    packages: Pick<PackageRepo, 'pinOf' | 'get' | 'list'>
    installer: Pick<PackageInstaller, 'acquire'>
    forwarder: PluginForwarder
    listings: Pick<ToolListings, 'exclusiveServer'>
    resolve?: Resolver
    test?: typeof testPlugin
  },
  pkg: string,
  server: string,
): Promise<PackageListingAnswer> {
  const source = packageServerSource(pkg, server)
  const busy: PackageListingAnswer = { status: 429, body: { detail: 'a listing of this server is already running' }, retryAfter: RETRY_AFTER_S }
  if (listing.has(source)) return busy
  listing.add(source)
  try {
    const view = await deps.packages.get(pkg)
    const pin = view && (await deps.packages.pinOf(pkg))
    if (!view || !pin) return refusal(404, `no plugin package named "${pkg}"`)
    if (!view.approved) return refusal(409, 'approve the pin before listing its servers')
    const run = async (put: PackageListingPut): Promise<PackageListingAnswer> => {
      let acquired: Awaited<ReturnType<PackageInstaller['acquire']>>
      try {
        acquired = await deps.installer.acquire(pin)
      } catch (err) {
        if (err instanceof PackageRefusedError) return refusal(409, `plugin package ${pkg} is not loaded: ${err.problems.join('; ')}`)
        return refusal(502, `plugin package ${pkg} could not be loaded: ${err instanceof PluginError || err instanceof Error ? err.message : String(err)}`)
      }
      try {
        const loaded: LoadedPackage = { name: pkg, contentHash: pin.contentHash, dir: acquired.dir, allowed: pin.allowRefused === true, builtinTools: [] }
        const declared = offeredServers([loaded]).find((s) => s.server === server)
        if (!declared) return refusal(404, `plugin package ${pkg} has no MCP server "${server}"`)
        if (declared.local) return refusal(409, `"${source}" is started only by a turn, so its tools are not listed here`)
        // Named as a server of another package a turn loads: neither is offered (as a turn's own are, below).
        const others = (await deps.packages.list()).filter((p) => p.name !== pkg && p.enabled && p.approved)
        const collision = collisionProblem(
          serverCollisions([
            { pkg, server },
            ...others.flatMap((p) => reviewServers(p.review, p.allow_refused).map((s) => ({ pkg: p.name, server: s.server }))),
          ]),
          pkg,
          server,
        )
        if (collision) return refusal(409, collision)
        const { configs, problems } = await packageServerConfigs([loaded], deps.resolve ?? systemResolver)
        const config = configs.find((c) => c.server === server)
        if (!config) {
          const what = `plugin package ${pkg}: MCP server "${server}"`
          return refusal(409, problems.find((p) => p.startsWith(what)) ?? `${what} is not offered`)
        }
        const test = await (deps.test ?? testPlugin)(config.plugin, config.address, deps.forwarder)
        if (test.ok) await put(pin.contentHash, test.stored, { truncated: test.truncated })
        return { status: 200, body: test }
      } finally {
        acquired.release()
      }
    }
    const held = await deps.listings.exclusiveServer(pkg, server, run)
    if (held === 'busy') return busy
    if (held === 'full') return { status: 429, body: { detail: 'too many connection tests are running; try again shortly' }, retryAfter: RETRY_AFTER_S }
    if (held === 'unreachable') return refusal(502, 'the AI database is unreachable, so the listing cannot be stored; try again shortly')
    return held.value
  } finally {
    listing.delete(source)
  }
}
