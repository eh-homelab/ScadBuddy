import type { PackageListingPut, ToolListings } from '../../access/listings.js'
import { packageServerSource } from '../../access/model.js'
import { type Resolver, systemResolver } from '../../http/egress.js'
import type { PluginForwarder } from '../forwarder.js'
import { PluginError } from '../registry.js'
import { DEFAULT_PLUGIN_TEST_TIMEOUT_MS, type PluginTest, testPlugin } from '../testConnection.js'
import { type LoadedPackage, type PackageInstaller, PackageRefusedError } from './install.js'
import { offeredServers, packageServerConfigs } from './mcp.js'
import type { PackageRepo } from './store.js'

// Listing a plugin package's http MCP server (#4757): Settings → Tools & plugins' refresh of a
// `package:<pkg>/mcp:<server>` source, as a remote plugin's test lists its tools (routes/plugins.ts
// `testAndList`). The pin is acquired as a turn loads it (install.ts), the server checked as a turn
// offers it (mcp.ts `packageServerConfigs`), and one `tools/list` made through the forwarder at the
// address the egress check passed. A local (stdio) server is never listed here: listing it would
// start the package's own process outside a turn.

// One listing per server at a time in this process; ToolListings.exclusiveServer holds it across replicas.
const listing = new Set<string>()

// As routes/plugins.ts: a test ends within its timeout, and 2 s more covers the listing's write.
const RETRY_AFTER_S = Math.ceil(DEFAULT_PLUGIN_TEST_TIMEOUT_MS / 1000) + 2

export type PackageListingAnswer = { status: 200 | 404 | 409 | 429 | 502; body: PluginTest | { detail: string }; retryAfter?: number }

const refusal = (status: 404 | 409 | 502, detail: string): PackageListingAnswer => ({ status, body: { detail } })

/**
 * Lists package `pkg`'s server `server` and, on a good test, stores its tools under
 * `package:<pkg>/mcp:<server>`; a failed test stores nothing. 429 while another listing of it
 * runs; 404 for an unknown package or server; 409 for an unapproved pin, a local server, and
 * a server a turn would not offer (`deps.refusal`, or its own problem: a name that cannot be
 * offered, a URL the egress check refuses), before any connection is made.
 */
export async function listPackageServer(
  deps: {
    packages: Pick<PackageRepo, 'pinOf' | 'get'>
    installer: Pick<PackageInstaller, 'acquire'>
    /**
     * Why a turn would not offer the server now, as Settings shows it (access/packageProvider.ts
     * `listingRefusal`: the package or the server switched off, the package refused at load, past
     * the budget or not yet checked, a name another loaded server carries); undefined when it would.
     */
    refusal: (pkg: string, server: string) => Promise<string | undefined>
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
    // No connection is made for a server a turn would not offer.
    const refused = await deps.refusal(pkg, server)
    if (refused !== undefined) return refusal(409, refused)
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
