import path from 'node:path'
import type { Options } from '@anthropic-ai/claude-agent-sdk'

// Least-privilege query options for the Claude Agent SDK harness (spec D7 and
// §4.4). Nothing here calls the API; #255 adds credentials and the query loop,
// and must build every query's options through this function.
//
// Option semantics are quoted from the pinned SDK's own type declarations
// (node_modules/@anthropic-ai/claude-agent-sdk/sdk.d.ts, 0.3.283 and 0.3.287):
//
//   tools: "`[]` (empty array) - Disable all built-in tools"
//   settingSources: "Pass `[]` to disable filesystem settings (SDK isolation
//     mode)" — so nothing is read from a host ~/.claude or a project .claude/
//   strictMcpConfig: "Only use MCP servers passed via the `mcpServers` option
//     ... ignoring all other MCP configurations"
//   env: "When set, this value REPLACES the subprocess environment entirely —
//     it is not merged with `process.env`." Passing it explicitly is what keeps
//     the service's own environment (the database URL above all) out of the
//     Claude Code subprocess; credentials are added per query by #255.

/**
 * The service-owned state directory in the image (Dockerfile `agent` stage).
 * It and its two children are the only writable paths besides the plugin cache
 * (spec §4.4); everything else can be a read-only root filesystem.
 */
export const DEFAULT_STATE_DIR = '/var/lib/scadbuddy-agent'

export type HarnessPaths = {
  /** Service-owned state directory; the Claude Code config dir lives under it. */
  stateDir: string
}

export function claudeConfigDir(paths: HarnessPaths): string {
  return path.join(paths.stateDir, 'claude')
}

export function scratchDir(paths: HarnessPaths): string {
  return path.join(paths.stateDir, 'work')
}

/**
 * The plugin package cache (#297, src/plugins/packages/install.ts): a cache
 * only, rebuilt from the pins in Postgres, so an emptyDir will do.
 */
export function pluginCacheDir(paths: HarnessPaths): string {
  return path.join(paths.stateDir, 'plugins')
}

export function buildQueryOptions(paths: HarnessPaths): Options {
  const env: Record<string, string> = {
    CLAUDE_CONFIG_DIR: claudeConfigDir(paths),
    HOME: paths.stateDir,
  }
  // PATH only: the bundled binary is located by absolute path, but a
  // subprocess with no PATH at all cannot resolve anything it shells out to.
  if (process.env.PATH !== undefined) env.PATH = process.env.PATH

  return {
    tools: [],
    settingSources: [],
    strictMcpConfig: true,
    mcpServers: {},
    cwd: scratchDir(paths),
    env,
    settings: { disableSkillShellExecution: true },
  }
}
