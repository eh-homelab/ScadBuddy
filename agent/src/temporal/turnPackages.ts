import { cp, mkdir, rename, rm } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { declaredConfigs } from '../harness/plugins.js'
import { hashTree, PackageContentError } from '../plugins/packages/hash.js'
import type { LoadedPackage } from '../plugins/packages/install.js'
import { packageServerName, withoutRemoteServers } from '../plugins/packages/mcp.js'
import { type TurnPackage, type TurnServer } from './turnTools.js'

// Plugin packages in a durable session (#2320). A classic turn hands Claude Code each
// enabled package's directory (sessions/manager.ts `packagePlugins`), materialised from
// its pin and re-hashed for the turn (plugins/packages/install.ts `loadPackagesForRun`).
// A durable turn's Claude Code runs in the agent-durable container, so:
//
//   - each loaded package is copied to `<payload dir>/plugins/<content hash>/`, on the
//     volume both sidecars mount, once per content hash (it never changes under that
//     name), and the turn's listing names it (`TurnPackage`); the segment runner hashes
//     the copy again before it loads it, as the agent re-hashes its own cache;
//   - the package's MCP servers run here, as turn tools, as a remote plugin's do: the
//     durable engine starts no MCP server of a plugin (the Temporal plugin sets
//     `strict_mcp_config`; a package that declares an http one is shared without it,
//     `sharePackage`, and the segment runner refuses a copy that still does), and here
//     each call goes through the gate. They carry the
//     names Claude Code gives them (`plugin_<package>_<server>`), so their tiers are
//     the classic turn's: no package tool is ScadBuddy's, so every one is outward.
//     An http server is reached only through the loopback forwarder, as a remote
//     plugin is and as a classic turn reaches it (plugins/packages/mcp.ts
//     `forwardPackageServers`: the egress-checked address, no redirect, no OAuth
//     discovery, the header added there); a local (stdio) server only for a package
//     approved with `allow_refused`, started in the package's folder with only PATH,
//     HOME and its own declared `env`.

/** A durable turn's packages are shared under this folder of the payload volume. */
export const SHARED_PACKAGES = 'plugins'

/** The shared folder's name for a pin's content hash (`sha256:<hex>` → `<hex>`). */
export function sharedName(contentHash: string): string {
  const hex = contentHash.replace(/^sha256:/, '')
  if (!/^[0-9a-f]{64}$/.test(hex)) throw new Error(`not a package content hash: ${contentHash}`)
  return hex
}

/**
 * Copies a loaded package to `root/<hash>` unless a copy that hashes to `<hash>` is
 * already there, and returns its name under `root`. A copy that does not match is
 * replaced: the volume is shared, so it is checked, never trusted. A package that
 * declares an http MCP server is shared without it (`withoutRemoteServers`, made under
 * `localRoot`), named by that copy's own hash: Claude Code in agent-durable would reach
 * it itself, past the forwarder, and the segment runner refuses a copy that declares one.
 */
export async function sharePackage(pkg: LoadedPackage, root: string, localRoot: string): Promise<TurnPackage> {
  const source = await withoutRemoteServers(pkg.dir, pkg.contentHash, localRoot)
  const contentHash = source === pkg.dir ? pkg.contentHash : (await hashTree(source)).hash
  const name = sharedName(contentHash)
  const target = path.join(root, name)
  if (!(await matches(target, contentHash))) {
    await mkdir(root, { recursive: true })
    const tmp = path.join(root, `.tmp-${randomUUID()}`)
    try {
      await cp(source, tmp, { recursive: true, preserveTimestamps: true })
      if (!(await matches(tmp, contentHash))) throw new Error(`the copy of ${pkg.name} does not hash to its pin`)
      await rm(target, { recursive: true, force: true })
      try {
        await rename(tmp, target)
      } catch (err) {
        // Another replica placed it first: fine when it matches.
        const code = (err as NodeJS.ErrnoException).code
        if ((code !== 'ENOTEMPTY' && code !== 'EEXIST') || !(await matches(target, contentHash))) throw err
      }
    } finally {
      await rm(tmp, { recursive: true, force: true })
    }
  }
  return { name: pkg.name, dir: name, content_hash: contentHash, allowed: pkg.allowed, builtin_tools: [...pkg.builtinTools] }
}

async function matches(dir: string, contentHash: string): Promise<boolean> {
  if (!existsSync(dir)) return false
  try {
    return (await hashTree(dir)).hash === contentHash
  } catch (err) {
    if (err instanceof PackageContentError || (err as NodeJS.ErrnoException).code === 'ENOENT') return false
    throw err
  }
}

type McpConfig = Record<string, unknown>

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function entries(map: unknown): [string, McpConfig][] {
  const servers = isRecord(map) && isRecord(map.mcpServers) ? map.mcpServers : map
  return isRecord(servers) ? Object.entries(servers).filter((e): e is [string, McpConfig] => isRecord(e[1])) : []
}

function strings(value: unknown): Record<string, string> {
  return isRecord(value) ? Object.fromEntries(Object.entries(value).filter((e): e is [string, string] => typeof e[1] === 'string')) : {}
}

/** `${CLAUDE_PLUGIN_ROOT}` as Claude Code expands it in a plugin's server command. */
function expandRoot(text: string, root: string): string {
  return text.split('${CLAUDE_PLUGIN_ROOT}').join(root)
}

/** A client on a package's local MCP server, started for one listing or one call. */
async function connectStdio(config: McpConfig, root: string): Promise<Client> {
  const client = new Client({ name: 'scadbuddy-durable-turn', version: '1' })
  const env: Record<string, string> = {
    ...(process.env.PATH ? { PATH: process.env.PATH } : {}),
    ...(process.env.HOME ? { HOME: process.env.HOME } : {}),
    ...Object.fromEntries(Object.entries(strings(config.env)).map(([k, v]) => [k, expandRoot(v, root)])),
  }
  await client.connect(
    new StdioClientTransport({
      command: expandRoot(String(config.command), root),
      args: (Array.isArray(config.args) ? config.args : []).map((a) => expandRoot(String(a), root)),
      env,
      cwd: root,
      stderr: 'ignore',
    }),
  )
  return client
}

/**
 * The local MCP servers of the packages approved with `allow_refused`, as turn servers;
 * their http servers come through the forwarder instead (turnServers.ts).
 */
export function localPackageServers(loaded: readonly LoadedPackage[], problems: string[]): TurnServer[] {
  const servers: TurnServer[] = []
  for (const pkg of loaded) {
    for (const { map } of declaredConfigs(pkg.dir).mcp) {
      for (const [server, config] of entries(map)) {
        if (!pkg.allowed || typeof config.command !== 'string') continue
        const name = packageServerName(pkg.name, server)
        if (name.includes('__')) {
          problems.push(`plugin package ${pkg.name}: MCP server "${server}" has a name a durable session cannot offer`)
          continue
        }
        servers.push({ name, connect: () => connectStdio(config, pkg.dir), tierOf: () => 'outward' })
      }
    }
  }
  return servers
}

/** Built-ins a durable session runs in its segment and cannot ask a human about first. */
const PARKABLE_BUILTINS = new Set(['Bash', 'PowerShell'])

/**
 * An allowed package's built-ins that a durable session cannot offer: every call of one
 * is outward (run.ts `builtinTools`), and only Bash and PowerShell run as their own
 * activities, which can wait for a human (the Temporal plugin's `tool_activities`).
 */
export function unparkableBuiltins(pkg: TurnPackage): string[] {
  return pkg.builtin_tools.filter((t) => !PARKABLE_BUILTINS.has(t) && !OFFERED.has(t))
}

/**
 * What every durable turn's Claude Code has already (the Skill tool). Not Agent: the
 * durable agent starts without it, and one a package names would run unasked.
 */
const OFFERED = new Set(['Skill'])
