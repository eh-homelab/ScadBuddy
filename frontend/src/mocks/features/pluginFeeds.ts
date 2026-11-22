import { HttpResponse, http } from 'msw'
import type { McpAuthMode } from '../../api/mcpTokens'
import type { PluginFeedPackage, PluginFeedView } from '../../api/pluginFeeds'

/**
 * The agent service's `GET /api/v1/ai/plugin-feeds/{feed}` (#6933,
 * `agent/src/routes/pluginFeeds.ts`): Settings' view of the marketplace feed.
 */

const URL_BASE = 'https://scadbuddy.example/api/v1/ai/plugin-feeds/default'

function seed(): PluginFeedPackage[] {
  return [
    { name: 'greeter', listed: true, reason: null, kind: 'hosted', version: 3, commit: null },
    { name: 'print-helpers', listed: true, reason: null, kind: 'git', version: null, commit: '4b825dc642cb6eb9a060e54bf8d69288fbee4904' },
    { name: 'labels', listed: false, reason: 'not-approved', kind: null, version: null, commit: null },
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

export const handlers = [
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
