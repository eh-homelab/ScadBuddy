import { randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { cp, mkdir, readFile, rename, rm, utimes, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { declaredConfigs, hookEvents, noCommand } from '../../harness/plugins.js'
import { hashTree, PackageContentError } from './hash.js'
import { hookId, inlineCommandAt, packageItems, type PackageItems, SAFE_NAME_RE } from './vet.js'
import { packageSource, type ToolAccess } from '../../access/model.js'
import { strictest } from '../../access/items.js'
import type { PackageAccess } from '../../access/resolver.js'
import type { LoadedPackage } from './install.js'

// The copy of a package (or of the own plugin) that a turn loads (#4757): without its http
// MCP servers, which reach the turn through the forwarder (mcp.ts `forwardPackageServers`;
// a query with `strictMcpConfig` off would start them itself, harness/run.ts), and without
// what the turn's access switches off. It is `<root>/<hex>/<package name>`: named by its own
// hash, so two turns with different access to one pin each have their own and neither
// overwrites the other's, in a folder named after the package, because Claude Code 2.1.310
// names a root skill with no frontmatter `name` by its folder (`_7t`) and a plugin with no
// manifest name by its folder too ("this plugin is named after its directory"; both read
// with `strings -n 20` in SDK 0.3.310's bundled `claude`). So every access variant gives such
// a skill one name: vet.ts `rootSkillName`.

/**
 * What a derived copy leaves out, by name: skills (commands too) and subagents as
 * `packageItems` keys them, hook ids (`hookId`), and the local servers that are switched
 * off or whose names another package server carries (mcp.ts `collidingServerNames`).
 * Frontmatter names are not read here: an item left in whose frontmatter `name` is an off
 * item's is still loaded, and Claude Code may offer it under that name (a skills path that
 * is itself a skill, or a subagent); Task 5's call check refuses such a call through
 * `PackageReview.aliases`.
 */
export type Omitted = {
  skills: ReadonlySet<string>
  subagents: ReadonlySet<string>
  hooks: ReadonlySet<string>
  servers: ReadonlySet<string>
}

export const NOTHING_OMITTED: Omitted = { skills: new Set(), subagents: new Set(), hooks: new Set(), servers: new Set() }

/**
 * What a package's copy leaves out (#4757): its skills, subagents and hooks switched off in
 * `access` (a hook's row is `hook:<event>:<id>`, so its id is what follows the last `:`), and
 * its servers whose switch is off in `servers`, keyed by the package's own server name (the
 * caller adds the ones whose names another package's server carries, mcp.ts
 * `collidingServerNames`). Nothing without either.
 */
export function omittedOf(access: PackageAccess | undefined, servers: ReadonlyMap<string, { enabled: boolean }>): Omitted {
  return {
    skills: new Set(access?.off.skills ?? []),
    subagents: new Set(access?.off.subagents ?? []),
    hooks: new Set([...(access?.off.hooks ?? [])].map((hook) => hook.slice(hook.lastIndexOf(':') + 1))),
    servers: new Set([...servers].filter(([, s]) => !s.enabled).map(([name]) => name)),
  }
}

/**
 * What package `pkg`'s copy leaves out in a turn, classic and durable alike: `omittedOf` its
 * items' access and its own servers' switches (`switches`, by `packageServerSource`, mcp.ts
 * `serverAccess`), its servers whose names another package's server carries
 * (`colliding`, mcp.ts `collidingServerNames`), and those no turn can offer
 * (`unoffered`, mcp.ts `unofferedLocalServers`).
 */
export function packageOmitted(
  pkg: string,
  access: ReadonlyMap<string, PackageAccess>,
  switches: ReadonlyMap<string, { enabled: boolean }>,
  colliding: ReadonlyMap<string, readonly { pkg: string; server: string }[]>,
  unoffered: readonly { pkg: string; server: string }[] = [],
): Omitted {
  const prefix = `${packageSource(pkg)}/mcp:`
  const own = new Map([...switches].flatMap(([source, at]) => (source.startsWith(prefix) ? [[source.slice(prefix.length), at] as const] : [])))
  const omitted = omittedOf(access.get(packageSource(pkg)), own)
  const named = [...[...colliding.values()].flat(), ...unoffered].filter((o) => o.pkg === pkg).map((o) => o.server)
  return { ...omitted, servers: new Set([...omitted.servers, ...named]) }
}

/** Whether `omitted` leaves anything out. */
export function omitsAny(omitted: Omitted): boolean {
  return omitted.skills.size + omitted.subagents.size + omitted.hooks.size + omitted.servers.size > 0
}

/**
 * One built-in's access across the allowed packages of `loaded` that name it, as
 * resolver.ts `builtin` reads it: each one's `builtin:<name>` (`off`, or its tier), `outward`
 * where it has none, the strictest winning; `outward` when none names it.
 */
export function builtinAccessOf(name: string, loaded: readonly LoadedPackage[], access: ReadonlyMap<string, PackageAccess>): ToolAccess {
  const each = loaded
    .filter((p) => p.allowed && p.builtinTools.includes(name))
    .map((p): ToolAccess => {
      const stored = access.get(packageSource(p.name))
      return stored?.off.builtins.has(name) ? 'off' : (stored?.builtinTiers.get(name) ?? 'outward')
    })
  return strictest(each) ?? 'outward'
}

/**
 * The built-ins a turn offers none of (#4757): those whose access across the allowed
 * packages naming them (`builtinAccessOf`, an unset package counting `outward`) is `off`. One
 * set for the turn, which leaves them out of its `builtinTools`.
 */
export function builtinsOffOf(loaded: readonly LoadedPackage[], access: ReadonlyMap<string, PackageAccess>): Set<string> {
  const named = new Set(loaded.filter((p) => p.allowed).flatMap((p) => p.builtinTools))
  return new Set([...named].filter((name) => builtinAccessOf(name, loaded, access) === 'off'))
}

/** Removing one item can show another of the same name that the first hid; this many rounds at most. */
const PASSES = 4

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function serverEntries(map: unknown): [string, unknown][] {
  const servers = isRecord(map) && isRecord(map.mcpServers) ? map.mcpServers : map
  return isRecord(servers) ? Object.entries(servers) : []
}

/** Whether a server entry is reached by URL (http, sse): the forwarder's, never Claude Code's. */
export function isRemoteEntry(config: Record<string, unknown>): boolean {
  return noCommand(config) && (typeof config.url === 'string' || config.type === 'http' || config.type === 'sse')
}

/** Whether a plugin directory declares any server reached by URL. */
export function declaresRemoteServers(dir: string): boolean {
  return declaredConfigs(dir).mcp.some(({ map }) => serverEntries(map).some(([, c]) => isRecord(c) && isRemoteEntry(c)))
}

/** Marks a package copy as just used: its folder's mtime is what packageServers.ts `sweepPackageCopies` reads. */
export async function touch(dir: string): Promise<void> {
  const now = new Date()
  await utimes(dir, now, now).catch(() => {})
}

function localServerNames(dir: string): string[] {
  return declaredConfigs(dir).mcp.flatMap(({ map }) =>
    serverEntries(map)
      .filter(([, c]) => !(isRecord(c) && isRemoteEntry(c)))
      .map(([name]) => name),
  )
}

/** `packageItems` of `dir`, its root skill named after the package `name`. */
function itemsOf(dir: string, name: string): PackageItems {
  return packageItems(dir, declaredConfigs(dir).manifest, name)
}

/** What `dir` holds that `omitted` names, as `kind name` lines. */
function heldOmitted(dir: string, name: string, omitted: Omitted, items: PackageItems = itemsOf(dir, name)): string[] {
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
async function leaveOut(tmp: string, name: string, omitted: Omitted): Promise<void> {
  const items = itemsOf(tmp, name)
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

export async function hashesTo(dir: string, hash: string): Promise<boolean> {
  if (!existsSync(dir)) return false
  try {
    return (await hashTree(dir)).hash === hash
  } catch (err) {
    if (err instanceof PackageContentError || (err as NodeJS.ErrnoException).code === 'ENOENT') return false
    throw err
  }
}

/** `<root>/<hex of hash>/<name>`, where a copy that hashes to `hash` is loaded from. */
export function copyPath(root: string, hash: string, name: string): string {
  const hex = hash.replace(/^sha256:/, '')
  if (!/^[0-9a-f]{64}$/.test(hex)) throw new Error(`not a package content hash: ${hash}`)
  if (!SAFE_NAME_RE.test(name)) throw new Error(`not a package name: ${name}`)
  return path.join(root, hex, name)
}

/**
 * Puts the copy at `tmp` (which hashes to `want`) at `target`, so that every read of
 * `target` sees a whole copy: one there that hashes to `want` (another turn's, perhaps in
 * use) is kept as it is and `tmp` is left for the caller to remove; one that does not is
 * moved aside whole before `tmp` is renamed in. Nothing is removed in place. Its folder
 * (`<hex>`) is touched first: its mtime is what packageServers.ts `sweepPackageCopies` keeps
 * it by (#2395), so a sweep that takes it after the touch sees the use and puts it back.
 */
export async function placeCopy(tmp: string, target: string, want: string): Promise<void> {
  const folder = path.dirname(target)
  await mkdir(folder, { recursive: true })
  await touch(folder)
  if (await hashesTo(target, want)) return
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      await rename(tmp, target)
      return
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code
      if (code !== 'ENOTEMPTY' && code !== 'EEXIST') throw err
    }
    // Another turn placed it first: fine when it is the same copy.
    if (await hashesTo(target, want)) return
    const aside = path.join(path.dirname(folder), `.sweep-${randomUUID()}`)
    await rename(target, aside).then(
      () => rm(aside, { recursive: true, force: true }),
      (err: NodeJS.ErrnoException) => {
        if (err.code !== 'ENOENT') throw err
      },
    )
  }
  throw new Error(`could not place the copy at ${target}`)
}

/**
 * The package as a turn's Claude Code loads it. `dir` and `contentHash` themselves when it
 * declares no server reached by URL, holds nothing `omitted` names and names nothing by its
 * folder (it has a manifest name and no root skill); else a copy at
 * `root/<hex>/<package name>` (`copyPath`), named by the copy's own hash, which it answers,
 * with links followed (the own plugin's checkout links `skills` and `agents`, which
 * `hashTree` refuses), and its http servers and every omitted item left out. The package
 * name is the manifest's, else `name` (the pin's). With a pin hash, the copy must hash to
 * it before anything is left out: the source is read again after its loader hashed it, and
 * a link it holds would be followed. The own plugin has no pin hash (`contentHash`
 * undefined, a trusted image path): only its copy is hashed. A copy is made again from
 * `dir` on every call: one already there is kept only while it hashes to the fresh copy,
 * so it is never trusted as it is. The original is never written to. It fails when the
 * copy still declares an http server or an omitted item. `items` are the source's
 * (`packageItems`), whatever the copy left out.
 */
export async function derivedPackage(
  dir: string,
  contentHash: string | undefined,
  root: string,
  omitted: Omitted = NOTHING_OMITTED,
  name?: string,
): Promise<{ dir: string; contentHash: string | undefined; items: PackageItems }> {
  const source = path.resolve(dir)
  const manifest = declaredConfigs(source).manifest
  const pkgName = typeof manifest.name === 'string' ? manifest.name : name
  if (pkgName === undefined) throw new Error(`${dir} has no name: no manifest name and none given`)
  if (!SAFE_NAME_RE.test(pkgName)) throw new Error(`${dir} has a name that is not a safe path segment`)
  // What the source holds, read once: the turn's call rule takes its items and aliases from it.
  const items = itemsOf(source, pkgName)
  const namedByFolder = typeof manifest.name !== 'string' || [...items.skills.values()].some((at) => at.includes('.'))
  if (!namedByFolder && !declaresRemoteServers(source) && heldOmitted(source, pkgName, omitted, items).length === 0) return { dir, contentHash, items }
  await mkdir(root, { recursive: true })
  const tmp = path.join(root, `.tmp-${randomUUID()}`)
  try {
    await cp(source, tmp, { recursive: true, dereference: true })
    if (contentHash !== undefined && (await hashTree(tmp)).hash !== contentHash) {
      throw new Error(`the copy of ${dir} does not hash to its pin`)
    }
    for (let pass = 0; pass < PASSES && (declaresRemoteServers(tmp) || heldOmitted(tmp, pkgName, omitted).length); pass++) {
      await leaveOut(tmp, pkgName, omitted)
    }
    if (declaresRemoteServers(tmp)) throw new Error(`could not strip the http MCP servers of ${dir}`)
    const left = heldOmitted(tmp, pkgName, omitted)
    if (left.length) throw new Error(`could not leave out ${left.join(', ')} of ${dir}`)
    const want = (await hashTree(tmp)).hash
    const target = copyPath(root, want, pkgName)
    await placeCopy(tmp, target, want)
    return { dir: target, contentHash: want, items }
  } finally {
    await rm(tmp, { recursive: true, force: true })
  }
}
