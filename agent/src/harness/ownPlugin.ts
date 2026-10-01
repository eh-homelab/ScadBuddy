import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { TierResolver } from './permissions.js'

// ScadBuddy's own plugin as the harness loads it (#896, part of #299).
//
// agent/plugins/scadbuddy holds its own manifest and links `skills` and
// `agents` to the repository's plugins/scadbuddy, the one copy Claude Code
// installs from the marketplace. The external plugin cannot be loaded as it is:
// its `userConfig` and `.mcp.json` (whose `${user_config.*}` placeholders could
// expand to a secret) are for installs outside ScadBuddy, and in the harness
// its tools are already the in-process `scadbuddy` server
// (`mcp__scadbuddy__<tool>`, tools/harness.ts), which the subagents' `tools`
// allow. The image replaces the links with copies (Dockerfile, agent-build).
//
// Its skills and subagents need the Skill and Agent tools, the only built-ins
// a run that loads it gets (`tools`, harness/options.ts; spec D7 as amended
// by #896). Both are `read`: a skill is instructions, and a subagent can call
// only tools the session already has, each through the same permission seam
// (canUseTool and the PreToolUse hook run for a subagent's calls too).
// Measured on Claude Code 2.1.283 (test/harnessWiring.test.ts): the init
// message lists the subagent tool as `Task`, its older name, and a call named
// `Agent` runs it; neither asks canUseTool, so the PreToolUse hook is where
// their tier applies.

const HERE = path.dirname(fileURLToPath(import.meta.url))

/** agent/plugins/scadbuddy, from src/harness or dist/harness alike. */
export const OWN_PLUGIN_DIR = path.resolve(HERE, '..', '..', 'plugins', 'scadbuddy')

/** The built-in tools a run that loads the plugin is given. */
export const OWN_PLUGIN_TOOLS = ['Skill', 'Agent'] as const

const TIERED: ReadonlySet<string> = new Set([...OWN_PLUGIN_TOOLS, 'Task'])

export const ownPluginTierOf: TierResolver = (toolName) => (TIERED.has(toolName) ? 'read' : undefined)
