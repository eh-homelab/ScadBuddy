import { lstat, readdir, rename, rm } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { declaredConfigs } from '../harness/plugins.js'
import type { LoadedPackage } from '../plugins/packages/install.js'
import { toolOffReason } from '../access/items.js'
import { packageServerSource } from '../access/model.js'
import {
  collidingServerNames,
  collisionProblem,
  packageServerName,
  type ServerAccess,
  unofferableNameProblem,
} from '../plugins/packages/mcp.js'
import { pluginTierResolver } from '../plugins/registry.js'
import type { TurnServer } from './turnTools.js'

// A plugin package's local (stdio) MCP servers as turn servers (#2320), for a flow run's
// `tool('<server>__<tool>', …)` (turnTools.ts): a session turn's Claude Code starts them itself
// from the package it loads. They carry the names Claude Code gives them
// (`plugin_<package>_<server>`), so their tiers are a classic turn's: each server's
// `package:<pkg>/mcp:<server>` rows in ai_access, a tool with none outward; a server switched off
// there is not offered, nor any whose name another package server carries too (#4757). Only for a
// package approved with `allow_refused`, started in the package's folder with only PATH, HOME and
// its own declared `env`. A package's http servers are reached through the loopback forwarder
// instead (plugins/packages/mcp.ts `forwardPackageServers`).
//
// And the sweep of package copies (#2395): a classic turn's under a pod's `plugins-local/`
// (plugins/packages/derived.ts), and those a durable session shared under the payload volume's
// `plugins/` before the Python turn retired (plan 2026-10-13 Step 8.1), until they are gone.

/** Where durable sessions shared their packages on the payload volume: swept until empty. */
export const SHARED_PACKAGES = 'plugins'

/** A copy is kept this long after the last turn that used it (#2395). */
export const PACKAGE_COPY_KEPT_MS = 7 * 24 * 3600_000
/** A copy left half-made by a replica that stopped mid-copy is removed after this. */
const TMP_KEPT_MS = 3600_000

/**
 * Removes package copies no turn has used for PACKAGE_COPY_KEPT_MS (#2395), and copies
 * left half-made: under a pod's `plugins-local/`, and under the payload volume's `plugins/`
 * left by durable sessions (every replica sweeps it, so two may remove the same one). A pin
 * still enabled is touched by every turn that loads it (derived.ts `touch`), and copied again
 * if it was removed; a turn runs for minutes, never for the week a copy is kept.
 */
export async function sweepPackageCopies(root: string, now = Date.now()): Promise<string[]> {
  const names = await readdir(root).catch((err: NodeJS.ErrnoException) => {
    if (err.code === 'ENOENT') return [] as string[]
    throw err
  })
  const removed: string[] = []
  for (const name of names) {
    const keep = /^[0-9a-f]{64}$/.test(name) ? PACKAGE_COPY_KEPT_MS : /^\.(tmp|sweep)-/.test(name) ? TMP_KEPT_MS : undefined
    if (keep === undefined) continue
    const dir = path.join(root, name)
    const old = async (at: string) =>
      lstat(at).then(
        (s) => s.isDirectory() && now - s.mtimeMs >= keep,
        () => false,
      )
    if (!(await old(dir))) continue
    // Taken out of the way first, then checked again: a turn that used it in between
    // touched it before checking it (derived.ts), so it goes back; one that uses it
    // after finds nothing there and copies it again.
    const trash = path.join(root, `.sweep-${randomUUID()}`)
    try {
      await rename(dir, trash)
    } catch {
      continue
    }
    if (!(await old(trash))) {
      // Put back unless a turn already copied it again; either way the folder is a good copy.
      await rename(trash, dir).catch(() => rm(trash, { recursive: true, force: true }))
      continue
    }
    await rm(trash, { recursive: true, force: true })
    removed.push(name)
  }
  return removed
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
  const client = new Client({ name: 'scadbuddy-turn-tools', version: '1' })
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
 * A package server known but switched off in Settings → Tools & plugins (#4757 final review
 * M2): not listed, never connected to, each call refused in a classic turn's words.
 */
export function switchedOffServer(name: string): TurnServer {
  return {
    name,
    connect: () => Promise.reject(new Error(`${name} is switched off in Settings → Tools & plugins`)),
    tierOf: () => 'write',
    refusal: async (harnessName) => toolOffReason(harnessName),
    unlisted: true,
  }
}

/**
 * The local MCP servers of the packages approved with `allow_refused`, as turn servers;
 * their http servers come through the forwarder instead (turnServers.ts). A server
 * switched off in `access` (by `packageServerSource`) is not started: it is known, its
 * calls refused in a classic turn's words (`switchedOffServer`, #4757 final review M2).
 * One missing from `access` is not started either, nor one whose name another package
 * server carries (`collidingServerNames`).
 * A tool's tier is its stored one, else outward; a tool set off is `offTools`.
 */
export function localPackageServers(
  loaded: readonly LoadedPackage[],
  problems: string[],
  access: ReadonlyMap<string, ServerAccess>,
): TurnServer[] {
  const servers: TurnServer[] = []
  const colliding = collidingServerNames(loaded)
  for (const pkg of loaded) {
    for (const { map } of declaredConfigs(pkg.dir).mcp) {
      for (const [server, config] of entries(map)) {
        if (!pkg.allowed || typeof config.command !== 'string') continue
        const name = packageServerName(pkg.name, server)
        if (name.includes('__')) {
          problems.push(unofferableNameProblem(pkg.name, server))
          continue
        }
        const collision = collisionProblem(colliding, pkg.name, server)
        if (collision) {
          problems.push(collision)
          continue
        }
        const stored = access.get(packageServerSource(pkg.name, server))
        if (!stored) continue
        if (!stored.enabled) {
          servers.push(switchedOffServer(name))
          continue
        }
        const tiers = pluginTierResolver([{ name, toolTiers: stored.toolTiers }])
        const tierOf = (harnessName: string) => tiers(harnessName) ?? 'outward'
        servers.push({
          name,
          connect: () => connectStdio(config, pkg.dir),
          tierOf,
          // Any may park: a tier raised to outward later in the turn is asked about.
          mayPark: () => true,
          fixedTier: tierOf,
          ...(stored.disabledTools.length ? { offTools: new Set(stored.disabledTools) } : {}),
        })
      }
    }
  }
  return servers
}
