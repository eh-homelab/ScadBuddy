import { cp, lstat, mkdir, readdir, rename, rm } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { declaredConfigs } from '../harness/plugins.js'
import { hashTree, PackageContentError } from '../plugins/packages/hash.js'
import type { LoadedPackage } from '../plugins/packages/install.js'
import { packageServerSource } from '../access/model.js'
import {
  collidingServerNames,
  collisionProblem,
  packageServerName,
  type ServerAccess,
  touch,
  withoutRemoteServers,
} from '../plugins/packages/mcp.js'
import { pluginTierResolver } from '../plugins/registry.js'
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
//     the classic turn's: each server's `package:<pkg>/mcp:<server>` rows in ai_access,
//     a tool with none outward; a server switched off there is not offered, nor any whose
//     name another package server carries too (#4757).
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
  // Its last share, which the sweep keeps it by (sweepPackageCopies): touched before
  // it is checked, so a sweep that takes it after the touch sees the share and puts it back.
  await touch(target)
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

/** A copy is kept this long after the last turn that shared it (#2395). */
export const PACKAGE_COPY_KEPT_MS = 7 * 24 * 3600_000
/** A copy left half-made by a replica that stopped mid-copy is removed after this. */
const TMP_KEPT_MS = 3600_000

/**
 * Removes package copies no turn has shared for PACKAGE_COPY_KEPT_MS (#2395), and copies
 * left half-made: under the payload volume's `plugins/` (every replica sweeps it, so two
 * may remove the same one) and under a pod's `plugins-local/`. A pin still enabled is
 * shared again, and so touched, by every turn that lists it, and copied again if it was
 * removed; a turn runs for minutes, never for the week a copy is kept.
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
    // Taken out of the way first, then checked again: a turn that shared it in between
    // touched it before checking it (sharePackage), so it goes back; one that shares it
    // after finds nothing there and copies it again.
    const trash = path.join(root, `.sweep-${randomUUID()}`)
    try {
      await rename(dir, trash)
    } catch {
      continue
    }
    if (!(await old(trash))) {
      // Put back unless a share already copied it again; either way the folder is a good copy.
      await rename(trash, dir).catch(() => rm(trash, { recursive: true, force: true }))
      continue
    }
    await rm(trash, { recursive: true, force: true })
    removed.push(name)
  }
  return removed
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
 * their http servers come through the forwarder instead (turnServers.ts). A server
 * switched off in `access` (by `packageServerSource`; one missing from it too) is not
 * started, nor one whose name another package server carries (`collidingServerNames`).
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
          problems.push(`plugin package ${pkg.name}: MCP server "${server}" has a name a durable session cannot offer`)
          continue
        }
        const collision = collisionProblem(colliding, pkg.name, server)
        if (collision) {
          problems.push(collision)
          continue
        }
        const stored = access.get(packageServerSource(pkg.name, server))
        if (!stored?.enabled) continue
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

/** Built-ins the Temporal plugin runs as their own activities, so a call can wait for a human. */
const PARKABLE_BUILTINS = new Set(['Bash', 'PowerShell'])

/**
 * An allowed package's built-ins that a durable session cannot offer: every call of one
 * is outward (run.ts `builtinTools`). Only Bash and PowerShell run as their own
 * activities, which can wait for a human (the Temporal plugin's `tool_activities`); the
 * agent provides others as turn tools (`provided`, builtins/server.ts, #2393).
 */
export function unparkableBuiltins(pkg: TurnPackage, provided: ReadonlySet<string>): string[] {
  return pkg.builtin_tools.filter((t) => !PARKABLE_BUILTINS.has(t) && !OFFERED.has(t) && !provided.has(t))
}

/** What every durable turn's Claude Code has already (the Skill and Agent tools; Task is Agent's old name). */
const OFFERED = new Set(['Skill', 'Agent', 'Task'])
