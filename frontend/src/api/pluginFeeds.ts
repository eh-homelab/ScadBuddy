/**
 * Wire types for `/api/v1/ai/plugin-feeds/{feed}` (#6933), Settings' read of the marketplace
 * feed. Served by the agent service (`agent/src/routes/pluginFeeds.ts`), not the backend, so
 * not in the backend's OpenAPI spec. Keep them in step with that file.
 */

import type { McpAuthMode } from './mcpTokens'

/** Why a package the feed names is not listed. */
export type PluginFeedUnlisted = 'not-approved' | 'no-version' | 'unpinnable'

export interface PluginFeedPackage {
  name: string
  listed: boolean
  reason: PluginFeedUnlisted | null
  kind: 'hosted' | 'git' | null
  /** A hosted package's listed version. */
  version: number | null
  /** A git package's pinned commit. */
  commit: string | null
}

export interface PluginFeedView {
  slug: string
  /** The marketplace's name: what `/plugin install <plugin>@<name>` takes. */
  name: string
  owner: string
  /** What `/plugin marketplace add` takes; null while SCADBUDDY_PUBLIC_URL is unset. */
  marketplace_url: string | null
  /** What /mcp, and so the feed, applies; null when the agent could not read it. */
  auth_mode: McpAuthMode | null
  packages: PluginFeedPackage[]
}
