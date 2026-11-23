import { HttpResponse, http } from 'msw'
import type { McpAuthMode } from '../../api/mcpTokens'
import { isBuiltIn } from '../../api/aiPlugins'
import type { PluginFeedPackage, PluginFeedView } from '../../api/pluginFeeds'
import { packageList } from '../aiPlugins'

/**
 * The agent service's `GET /api/v1/ai/plugin-feeds/{feed}` (#6933,
 * `agent/src/routes/pluginFeeds.ts`): Settings' view of the marketplace feed, and its
 * `PUT` and `DELETE …/packages/{name}` (#6936), which list an installed package
 * (`mocks/aiPlugins.ts`) at its pin as the agent would, or take one out.
 */

const URL_BASE = 'https://scadbuddy.example/api/v1/ai/plugin-feeds/default'

const UNLISTED = {
  listed: false,
  reason: 'not-approved',
  kind: null,
  version: null,
  commit: null,
  archive: null,
  allow_refused: false,
} as const satisfies Omit<PluginFeedPackage, 'name'>

function seed(): PluginFeedPackage[] {
  return [
    { ...UNLISTED, name: 'greeter', listed: true, reason: null, kind: 'hosted', version: 3 },
    { ...UNLISTED, name: 'print-helpers', listed: true, reason: null, kind: 'git', commit: '4b825dc642cb6eb9a060e54bf8d69288fbee4904' },
    { ...UNLISTED, name: 'labels' },
  ]
}

const state = {
  packages: seed(),
  authMode: 'bearer' as McpAuthMode | null,
  marketplaceUrl: `${URL_BASE}/marketplace.json` as string | null,
}

export function reset(): void {
  state.packages = seed()
  state.authMode = 'bearer'
  state.marketplaceUrl = `${URL_BASE}/marketplace.json`
}

/** For tests: what the feed names, its auth mode and its address. */
export function setPluginFeed(changes: { packages?: PluginFeedPackage[]; authMode?: McpAuthMode | null; marketplaceUrl?: string | null }): void {
  if (changes.packages) state.packages = changes.packages
  if (changes.authMode !== undefined) state.authMode = changes.authMode
  if (changes.marketplaceUrl !== undefined) state.marketplaceUrl = changes.marketplaceUrl
}

/** An installed package as the feed lists it: at its approved pin, or not yet. */
function listedAs(name: string): PluginFeedPackage | undefined {
  const pkg = packageList().find((p) => p.name === name)
  if (!pkg || isBuiltIn(pkg)) return undefined
  const base = { ...UNLISTED, name, allow_refused: pkg.allow_refused }
  if (!pkg.approved) return base
  if (pkg.pin_kind === 'bytes') return { ...base, listed: true, reason: null, kind: 'archive', archive: pkg.bytes_sha256 }
  return { ...base, listed: true, reason: null, kind: 'git', commit: pkg.commit_sha }
}

const missing = (what: string) => HttpResponse.json({ detail: `no ${what}` }, { status: 404 })

export const handlers = [
  http.put('/api/v1/ai/plugin-feeds/:feed/packages/:name', ({ params }) => {
    if (params.feed !== 'default') return missing('plugin feed')
    const name = String(params.name)
    if (state.packages.some((p) => p.name === name)) return HttpResponse.json({ name, added: false })
    const row = listedAs(name)
    if (!row) return missing(`plugin package named "${name}"`)
    state.packages = [...state.packages, row].sort((a, b) => a.name.localeCompare(b.name))
    return HttpResponse.json({ name, added: true }, { status: 201 })
  }),
  http.delete('/api/v1/ai/plugin-feeds/:feed/packages/:name', ({ params }) => {
    if (params.feed !== 'default') return missing('plugin feed')
    state.packages = state.packages.filter((p) => p.name !== params.name)
    return new HttpResponse(null, { status: 204 })
  }),
  http.get('/api/v1/ai/plugin-feeds/:feed', ({ params }) => {
    if (params.feed !== 'default') return HttpResponse.json({ detail: 'no such plugin feed, plugin or version' }, { status: 404 })
    const view: PluginFeedView = {
      slug: 'default',
      name: 'scadbuddy',
      owner: 'ScadBuddy',
      marketplace_url: state.marketplaceUrl,
      auth_mode: state.authMode,
      packages: state.packages,
    }
    return HttpResponse.json(view)
  }),
]
