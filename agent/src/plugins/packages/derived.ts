import { randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { cp, mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { declaredConfigs, hookEvents } from '../../harness/plugins.js'
import { hashTree, PackageContentError } from './hash.js'
import { declaresRemoteServers, isRemoteEntry, touch } from './mcp.js'
import { hookId, inlineCommandAt, packageItems, type PackageItems } from './vet.js'

// The copy of a package (or of the own plugin) that a turn loads (#4757): without its http
// MCP servers, which reach the turn through the forwarder (mcp.ts `forwardPackageServers`;
// a query with `strictMcpConfig` off would start them itself, harness/run.ts), and without
// what the turn's access switches off. It is named by its own hash, so two turns with
// different access to one pin each have their own, and neither overwrites the other's.

/**
 * What a derived copy leaves out, by name: skills (commands too) and subagents as
 * `packageItems` keys them, hook ids (`hookId`), and the local servers that are switched
 * off or whose names another package server carries (mcp.ts `collidingServerNames`).
 */
export type Omitted = {
  skills: ReadonlySet<string>
  subagents: ReadonlySet<string>
  hooks: ReadonlySet<string>
  servers: ReadonlySet<string>
}

export const NOTHING_OMITTED: Omitted = { skills: new Set(), subagents: new Set(), hooks: new Set(), servers: new Set() }

/** Removing one item can show another of the same name that the first hid; this many rounds at most. */
const PASSES = 4

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function serverEntries(map: unknown): [string, unknown][] {
  const servers = isRecord(map) && isRecord(map.mcpServers) ? map.mcpServers : map
  return isRecord(servers) ? Object.entries(servers) : []
}

function localServerNames(dir: string): string[] {
  return declaredConfigs(dir).mcp.flatMap(({ map }) =>
    serverEntries(map)
      .filter(([, c]) => !(isRecord(c) && isRemoteEntry(c)))
      .map(([name]) => name),
  )
}

/**
 * `packageItems` of `dir`, with a skill at `.` named `rootName`: a package that is one skill
 * is named by its folder, which a copy's is not.
 */
function itemsOf(dir: string, rootName: string): PackageItems {
  const items = packageItems(dir, declaredConfigs(dir).manifest)
  const skills = new Map<string, string[]>()
  for (const [name, at] of items.skills) {
    const key = at.includes('.') ? rootName : name
    skills.set(key, [...(skills.get(key) ?? []), ...at])
  }
  return { ...items, skills }
}

/** What `dir` holds that `omitted` names, as `kind name` lines. */
function heldOmitted(dir: string, rootName: string, omitted: Omitted): string[] {
  const items = itemsOf(dir, rootName)
  return [
    ...[...items.skills.keys()].filter((n) => omitted.skills.has(n)).map((n) => `skill ${n}`),
    ...[...items.subagents.keys()].filter((n) => omitted.subagents.has(n)).map((n) => `subagent ${n}`),
    ...[...new Set(items.hooks.map((h) => h.id))].filter((id) => omitted.hooks.has(id)).map((id) => `hook ${id}`),
    ...[...new Set(localServerNames(dir))].filter((n) => omitted.servers.has(n)).map((n) => `MCP server ${n}`),
  ]
}

/** A manifest path entry as `packageItems` writes paths: no `./`, no trailing `/`, `.` for the root. */
function entryPath(entry: string): string {
  return path.posix.normalize(entry.replaceAll('\\', '/')).replace(/^(\.\/)+/, '').replace(/\/+$/, '') || '.'
}

/** The MCP config without its http servers and the servers `omitted` names. */
function withoutServers(map: unknown, omitted: ReadonlySet<string>): unknown {
  // A manifest's mcpServers may be a list of config paths and inline maps (harness/plugins.ts).
  if (Array.isArray(map)) return map.map((entry: unknown) => (typeof entry === 'string' ? entry : withoutServers(entry, omitted)))
  const nested = isRecord(map) && isRecord(map.mcpServers)
  const servers = nested ? (map.mcpServers as Record<string, unknown>) : map
  if (!isRecord(servers)) return map
  const kept = Object.fromEntries(Object.entries(servers).filter(([name, c]) => !(isRecord(c) && isRemoteEntry(c)) && !omitted.has(name)))
  return nested ? { ...(map as Record<string, unknown>), mcpServers: kept } : kept
}

/**
 * The hooks config without the handlers whose ids `omitted` names: a group left with no
 * handler goes, and an event left with no group. The ids are `hookId`'s of the uncut
 * event, the group's matcher and the handler, as vet.ts computes them.
 */
function withoutHooks(config: unknown, omitted: ReadonlySet<string>): unknown {
  if (omitted.size === 0) return config
  const { file, events } = hookEvents(config as Parameters<typeof hookEvents>[0])
  if (!isRecord(events)) return config
  const kept: Record<string, unknown> = {}
  for (const [event, groups] of Object.entries(events)) {
    const list = Array.isArray(groups) ? groups : [groups]
    let changed = false
    const left = list.flatMap((group: unknown) => {
      if (!isRecord(group)) return [group]
      const handlers = Array.isArray(group.hooks) ? group.hooks : [group.hooks]
      const rest = handlers.filter((handler: unknown) => !omitted.has(hookId(event, group.matcher, handler)))
      if (rest.length === handlers.length) return [group]
      changed = true
      return rest.length ? [{ ...group, hooks: rest }] : []
    })
    if (!changed) kept[event] = groups
    else if (left.length) kept[event] = left
  }
  return file ? { ...(config as Record<string, unknown>), hooks: kept } : kept
}

/** `rel` under `root`, refused when it is the root itself or outside it. */
function within(root: string, rel: string): string {
  const abs = path.resolve(root, rel)
  const inside = path.relative(root, abs)
  if (inside === '' || inside.startsWith('..') || path.isAbsolute(inside)) throw new Error(`${rel} is not inside the package`)
  return abs
}

/** One round of leaving out, in the copy at `tmp`. */
async function leaveOut(tmp: string, rootName: string, omitted: Omitted): Promise<void> {
  const items = itemsOf(tmp, rootName)
  const manifestFile = path.join(tmp, '.claude-plugin', 'plugin.json')
  const manifest = existsSync(manifestFile) ? (JSON.parse(await readFile(manifestFile, 'utf8')) as Record<string, unknown>) : undefined
  let manifestChanged = false
  // A manifest list entry naming a removed path goes too; a list emptied stays `[]`, never
  // absent, which would bring back the default folder.
  const unlist = (key: string, rel: string) => {
    if (!manifest) return
    const value = manifest[key]
    const list = Array.isArray(value) ? value : typeof value === 'string' ? [value] : undefined
    if (!list) return
    const rest = list.filter((e: unknown) => typeof e !== 'string' || entryPath(e) !== rel)
    if (rest.length !== list.length) {
      manifest[key] = rest
      manifestChanged = true
    }
  }

  for (const [name, at] of items.skills) {
    if (!omitted.skills.has(name)) continue
    for (const rel of at) {
      if (rel === '.') {
        // The package root is the skill: never delete it, only its SKILL.md.
        await rm(path.join(tmp, 'SKILL.md'), { force: true })
      } else if (manifest && isRecord(manifest.commands) && rel === inlineCommandAt(name)) {
        // Compared, never parsed: the key is the package's own text.
        const { [name]: _gone, ...rest } = manifest.commands
        manifest.commands = rest
        manifestChanged = true
        continue
      } else {
        await rm(within(tmp, rel), { recursive: true, force: true })
      }
      unlist('skills', rel)
      unlist('commands', rel)
    }
  }
  for (const [name, rel] of items.subagents) {
    if (!omitted.subagents.has(name)) continue
    await rm(within(tmp, rel), { force: true })
    unlist('agents', rel)
  }

  for (const { where, config } of declaredConfigs(tmp).hooks) {
    if (where === 'plugin.json hooks') continue
    const next = withoutHooks(config, omitted.hooks)
    if (next !== config) await writeFile(within(tmp, where), JSON.stringify(next))
  }
  if (manifest && manifest.hooks !== undefined && omitted.hooks.size) {
    const inline = (value: unknown) => (typeof value === 'string' ? value : withoutHooks(value, omitted.hooks))
    const next = Array.isArray(manifest.hooks) ? manifest.hooks.map(inline) : inline(manifest.hooks)
    if (JSON.stringify(next) !== JSON.stringify(manifest.hooks)) {
      manifest.hooks = next
      manifestChanged = true
    }
  }

  for (const { where, map } of declaredConfigs(tmp).mcp) {
    const next = withoutServers(map, omitted.servers)
    if (JSON.stringify(next) === JSON.stringify(map)) continue
    if (where === 'plugin.json mcpServers') {
      if (manifest) {
        manifest.mcpServers = withoutServers(manifest.mcpServers, omitted.servers)
        manifestChanged = true
      }
    } else {
      // `.mcp.json`, or a config file the manifest names: rewritten in place in the copy.
      await writeFile(within(tmp, where), JSON.stringify(next))
    }
  }
  if (manifest && manifestChanged) await writeFile(manifestFile, JSON.stringify(manifest))
}

async function hashesTo(dir: string, hash: string): Promise<boolean> {
  if (!existsSync(dir)) return false
  try {
    return (await hashTree(dir)).hash === hash
  } catch (err) {
    if (err instanceof PackageContentError || (err as NodeJS.ErrnoException).code === 'ENOENT') return false
    throw err
  }
}

/**
 * The package as a turn's Claude Code loads it. `dir` and `contentHash` themselves when it
 * declares no server reached by URL and holds nothing `omitted` names; else a copy under
 * `root`, with links followed (the own plugin's checkout links `skills` and `agents`, which
 * `hashTree` refuses), its http servers and every omitted item left out, named
 * `root/<hex>` by the copy's own hash, which it answers. The own plugin has no pin hash
 * (`contentHash` undefined): only a copy is hashed. A copy is made again from `dir` (re-hashed
 * by its loader) on every call: one already there is kept only while it hashes to the fresh
 * copy, so it is never trusted as it is. The original is never written to. It fails when the
 * copy still declares an http server or an omitted item.
 */
export async function derivedPackage(
  dir: string,
  contentHash: string | undefined,
  root: string,
  omitted: Omitted = NOTHING_OMITTED,
): Promise<{ dir: string; contentHash: string | undefined }> {
  const source = path.resolve(dir)
  const rootName = path.basename(source)
  if (!declaresRemoteServers(source) && heldOmitted(source, rootName, omitted).length === 0) return { dir, contentHash }
  await mkdir(root, { recursive: true })
  const tmp = path.join(root, `.tmp-${randomUUID()}`)
  try {
    await cp(source, tmp, { recursive: true, dereference: true })
    for (let pass = 0; pass < PASSES && (declaresRemoteServers(tmp) || heldOmitted(tmp, rootName, omitted).length); pass++) {
      await leaveOut(tmp, rootName, omitted)
    }
    if (declaresRemoteServers(tmp)) throw new Error(`could not strip the http MCP servers of ${dir}`)
    const left = heldOmitted(tmp, rootName, omitted)
    if (left.length) throw new Error(`could not leave out ${left.join(', ')} of ${dir}`)
    const want = (await hashTree(tmp)).hash
    const target = path.join(root, want.replace(/^sha256:/, ''))
    // Its last use, which turnPackages.ts `sweepPackageCopies` keeps it by (#2395),
    // touched before it is checked, as sharePackage does.
    await touch(target)
    if (!(await hashesTo(target, want))) {
      await rm(target, { recursive: true, force: true })
      await rename(tmp, target).catch(async (err: NodeJS.ErrnoException) => {
        // Another turn placed it first: fine when it is the same copy.
        if ((err.code !== 'ENOTEMPTY' && err.code !== 'EEXIST') || !(await hashesTo(target, want))) throw err
      })
    }
    return { dir: target, contentHash: want }
  } finally {
    await rm(tmp, { recursive: true, force: true })
  }
}
