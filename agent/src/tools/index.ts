import { approvalTools } from './approvals.js'
import { catalogueTools } from './catalogue.js'
import { customizerTools } from './customizer.js'
import { historyTools } from './history.js'
import { libraryTools } from './libraries.js'
import { outputTools } from './outputs.js'
import { printTools } from './print.js'
import { printMediaTools } from './prints.js'
import type { Tier } from '../auth/principal.js'
import type { Tool } from './registry.js'
import { sessionTools } from './sessions.js'
import { settingsTools } from './settings.js'

// Every ScadBuddy tool, in one list both projections read (spec §5.1, D3).
// Not here yet: browser_* tools (#254, #266), which are browser-only and so
// are the one permitted difference between the two lists; and the tools in
// coverage.ts `PENDING_ROUTES`. The session tools (#300) are sessions.ts.

export const ALL_TOOLS: readonly Tool[] = [
  ...catalogueTools,
  ...customizerTools,
  ...outputTools,
  ...historyTools,
  ...libraryTools,
  ...settingsTools,
  ...printTools,
  ...printMediaTools,
  ...approvalTools,
  ...sessionTools,
]

const byName = new Map<string, Tool>()
for (const tool of ALL_TOOLS) {
  if (byName.has(tool.name)) throw new Error(`duplicate tool name ${tool.name}`)
  byName.set(tool.name, tool)
}

const HARNESS_PREFIX = 'mcp__scadbuddy__'

/**
 * A tool's tier from the name the Agent SDK reports (`mcp__scadbuddy__<name>`,
 * https://code.claude.com/docs/en/agent-sdk/custom-tools), for the harness
 * permission seam (#255, PR #354: `runHarness({ mcpServers, tierOf })`).
 * `undefined` for anything that is not a ScadBuddy tool, which that seam
 * treats as `outward` (spec §8.1).
 */
export function tierOf(toolName: string): Tier | undefined {
  return toolName.startsWith(HARNESS_PREFIX) ? byName.get(toolName.slice(HARNESS_PREFIX.length))?.risk : undefined
}
