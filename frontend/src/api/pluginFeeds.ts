/**
 * Wire types for `/api/v1/ai/plugin-feeds/{feed}` (#6933), Settings' read of the marketplace
 * feed. Served by the agent service (`agent/src/routes/pluginFeeds.ts`), not the backend, so
 * not in the backend's OpenAPI spec. Keep them in step with that file.
 */

import type { McpAuthMode } from './mcpTokens'

/** Why a package the feed names is not listed. */
export type PluginFeedUnlisted = 'not-approved' | 'no-version' | 'unpinnable' | 'no-mirror'

export interface PluginFeedPackage {
  name: string
  listed: boolean
  reason: PluginFeedUnlisted | null
  /** How it is listed (#6936: `archive` is a bytes pin served from ScadBuddy's mirror). */
  kind: 'hosted' | 'git' | 'archive' | null
  /** A hosted package's listed version. */
  version: number | null
  /** A git package's pinned commit. */
  commit: string | null
  /** An archive's bytes pin: the sha256 of the mirrored file. */
  archive: string | null
  /** Approved with its hooks or MCP servers allowed: every client of the feed runs them. */
  allow_refused: boolean
}

export interface PluginFeedView {
  slug: string
  /** The marketplace's name: what `/plugin install <plugin>@<name>` takes. */
  name: string
  owner: string
  /** What `/plugin marketplace add` takes; on the origin Settings was opened on; null when the agent cannot tell it. */
  marketplace_url: string | null
  /** What /mcp, and so the feed, applies; null when the agent could not read it. */
  auth_mode: McpAuthMode | null
  packages: PluginFeedPackage[]
}
