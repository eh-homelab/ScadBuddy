import { HTTP_TOOL, HTTP_TOOL_NAME, httpTierOf } from '../harness/httpRequest.js'
import { toolPrefix } from '../plugins/registry.js'
import { SERVER_NAME } from '../tools/projections.js'
import type { Tool } from '../tools/registry.js'
import { type CallPlugin, namesItem, offCallItem, offItemReason } from './items.js'
import { BUILTIN_SOURCES, packageServerSource, registrySource, remoteSource, type ToolAccess } from './model.js'
import { monitorAccess, NATIVE_BUILTINS, SERVED_PREFIX, SESSION_BUILTINS } from './nativeBuiltins.js'
import type { AccessResolver } from './resolver.js'

/**
 * One call's access as it is now (#2171, spec §4.4), by the name Claude Code gives the
 * tool; undefined where ai_access says nothing of its own for this call, so the turn's
 * resolver stands. A read that fails rejects: the seam then refuses the call.
 */
export type LiveAccess = (harnessName: string, input?: unknown) => Promise<LiveAnswer>

/** A call's access now: a tier or `off`, `{off}` with the one sentence why when it names an item that is off, or undefined. */
export type LiveAnswer = ToolAccess | { off: string } | undefined

/** What a turn offers, so each call is read from its own source. */
export type LiveTurn = {
  tools: readonly Tool[]
  /** The remote plugins' names. */
  remote: readonly string[]
  /**
   * Each package server: the name its tools carry (packageServerName) and where its access
   * lives. The http ones (through the forwarder) and the local ones Claude Code starts.
   */
  packageServers: readonly { name: string; pkg: string; server: string }[]
  /** The plugins a Skill, Agent or Task call may name (#4757): each loaded package, and the own plugin while it is loaded. */
  plugins: readonly CallPlugin[]
  /** Each built-in the turn's allowed packages name, with the packages that name it (#4757). */
  builtins: readonly { name: string; packages: readonly string[] }[]
  /** Whether the turn offers Claude Code's own built-ins (nativeBuiltins.ts, Step 9.6): each call reads `builtin:claude-code`. */
  native?: boolean
  /** The session's own built-ins the turn serves (nativeBuiltins.ts `SESSION_BUILTINS`): each call reads `builtin:claude-code` too. */
  served?: readonly string[]
  /** Whether the turn gave Monitor a command queue (Bash was on when it was assembled): without one a command call is refused. */
  shell?: boolean
}

/** Why a Monitor `command` call is refused while Bash is off. */
export const MONITOR_SHELL_OFF =
  "Monitor's command runs a shell where Bash runs, and Bash is off in Settings → Tools & plugins (no sandbox, #779). Watch a WebSocket (`ws`) instead."

/**
 * The seam's per-call read (harness/permissions.ts `LiveSeam`): a registry tool's access
 * (`off` too when `builtin:browser` is switched off, for a browser tool), a remote
 * plugin's tool's (`off` when the plugin is switched off), a package server's tool's
 * (`off` when the server is switched off, else outward unless stored), and
 * http_request's (`off` when `builtin:http` is, its item when one is stored, else its
 * method's tier), a Skill, Agent or Task call's (`off` when it names an item that is off,
 * items.ts `offCallItem`, else undefined: the turn's tier stands), a built-in's of
 * `builtin:claude-code` (resolver.ts `native`: Claude Code's own by its name, the session's
 * by the name it serves them under) and any other package built-in's (resolver.ts `builtin`
 * over the packages that name it). Anything else is undefined.
 */
export function liveAccess(
  resolver: Pick<AccessResolver, 'item' | 'entry' | 'switchedItem' | 'builtin' | 'native' | 'rows'>,
  turn: LiveTurn,
): LiveAccess {
  const native = new Map(turn.native ? NATIVE_BUILTINS.map((b) => [b.name, b.access]) : [])
  const bashDeclared = NATIVE_BUILTINS.find((b) => b.name === 'Bash')!.access
  const served = new Map(SESSION_BUILTINS.filter((b) => turn.served?.includes(b.name)).map((b) => [`${SERVED_PREFIX}${b.name}`, b]))
  const registry = new Map(turn.tools.map((t) => [`${toolPrefix(SERVER_NAME)}${t.name}`, t]))
  const sources: { prefix: string; source: string }[] = [
    ...turn.remote.map((name) => ({ prefix: toolPrefix(name), source: remoteSource(name) })),
    ...turn.packageServers.map((s) => ({ prefix: toolPrefix(s.name), source: packageServerSource(s.pkg, s.server) })),
  ]
  const callSources = turn.plugins.map((p) => p.source)
  const builtins = new Map(turn.builtins.map((b) => [b.name, b.packages]))
  return async (harnessName, input) => {
    if (namesItem(harnessName) && callSources.length) {
      const off = offCallItem(harnessName, input, turn.plugins, await resolver.rows(callSources))
      if (off) return { off: offItemReason(off) }
    }
    // A package built-in by its access, Skill, Agent and Task too when a package names them (#4757 review M4).
    const builtin = harnessName.startsWith(SERVED_PREFIX) ? harnessName.slice(SERVED_PREFIX.length) : harnessName
    // Claude Code's own: its item, which a package that names it can only switch on.
    const declared = native.get(harnessName)
    if (declared !== undefined) return resolver.native(harnessName, declared, builtins.get(harnessName) ?? [])
    // The session's own: its item too; a Monitor's by its source as well (nativeBuiltins.ts `monitorAccess`).
    const own = served.get(harnessName)
    if (own) {
      const access = await resolver.native(own.name, own.access, builtins.get(own.name) ?? [])
      if (own.name !== 'Monitor' || access === 'off') return access
      const command = typeof input === 'object' && input !== null && (input as Record<string, unknown>).command !== undefined
      // Bash off now, or off when the turn was assembled (its server has no command queue): refused, never asked.
      if (command && !turn.shell) return { off: MONITOR_SHELL_OFF }
      const shell = command ? await resolver.native('Bash', bashDeclared, builtins.get('Bash') ?? []) : undefined
      if (shell === 'off') return { off: MONITOR_SHELL_OFF }
      return monitorAccess(access, input, shell)
    }
    const packages = builtins.get(builtin)
    if (packages) return resolver.builtin(builtin, packages)
    if (namesItem(harnessName)) return undefined
    const tool = registry.get(harnessName)
    if (tool) {
      const source = registrySource(tool.name)
      return source === BUILTIN_SOURCES.browser
        ? resolver.switchedItem(source, tool.name, tool.risk)
        : resolver.item(source, tool.name, tool.risk)
    }
    if (harnessName === HTTP_TOOL_NAME) {
      const { enabled, access } = await resolver.entry(BUILTIN_SOURCES.http, HTTP_TOOL)
      if (!enabled) return 'off'
      return access ?? httpTierOf(HTTP_TOOL_NAME, input)
    }
    const from = sources.find((s) => harnessName.startsWith(s.prefix))
    if (!from) return undefined
    return resolver.switchedItem(from.source, harnessName.slice(from.prefix.length), 'outward')
  }
}
