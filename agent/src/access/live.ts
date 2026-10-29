import { HTTP_TOOL, HTTP_TOOL_NAME, httpTierOf } from '../harness/httpRequest.js'
import { toolPrefix } from '../plugins/registry.js'
import { SERVER_NAME } from '../tools/projections.js'
import type { Tool } from '../tools/registry.js'
import { type CallPlugin, namesItem, offCallItem } from './items.js'
import { BUILTIN_SOURCES, packageServerSource, registrySource, remoteSource, type ToolAccess } from './model.js'
import type { AccessResolver } from './resolver.js'

/**
 * One call's access as it is now (#2171, spec §4.4), by the name Claude Code gives the
 * tool; undefined where ai_access says nothing of its own for this call, so the turn's
 * resolver stands. A read that fails rejects: the seam then refuses the call.
 */
export type LiveAccess = (harnessName: string, input?: unknown) => Promise<ToolAccess | undefined>

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
}

/** The prefix of the built-ins a classic turn serves itself (builtins/server.ts `BUILTIN_SERVER`, `mcp__builtin__<Name>`). */
const SERVED_PREFIX = 'mcp__builtin__'

/**
 * The seam's per-call read (harness/permissions.ts `LiveSeam`): a registry tool's access
 * (`off` too when `builtin:browser` is switched off, for a browser tool), a remote
 * plugin's tool's (`off` when the plugin is switched off), a package server's tool's
 * (`off` when the server is switched off, else outward unless stored), and
 * http_request's (`off` when `builtin:http` is, its item when one is stored, else its
 * method's tier), a Skill, Agent or Task call's (`off` when it names an item that is off,
 * items.ts `offCallItem`, else undefined: the turn's tier stands) and a package built-in's
 * (resolver.ts `builtin` over the packages that name it, by its own name or the name the
 * session serves it under). Anything else is undefined.
 */
export function liveAccess(resolver: Pick<AccessResolver, 'item' | 'entry' | 'switchedItem' | 'builtin' | 'rows'>, turn: LiveTurn): LiveAccess {
  const own = new Map(turn.tools.map((t) => [`${toolPrefix(SERVER_NAME)}${t.name}`, t]))
  const sources: { prefix: string; source: string }[] = [
    ...turn.remote.map((name) => ({ prefix: toolPrefix(name), source: remoteSource(name) })),
    ...turn.packageServers.map((s) => ({ prefix: toolPrefix(s.name), source: packageServerSource(s.pkg, s.server) })),
  ]
  const callSources = turn.plugins.map((p) => p.source)
  const builtins = new Map(turn.builtins.map((b) => [b.name, b.packages]))
  return async (harnessName, input) => {
    if (namesItem(harnessName)) {
      if (callSources.length === 0) return undefined
      return offCallItem(harnessName, input, turn.plugins, await resolver.rows(callSources)) === undefined ? undefined : 'off'
    }
    const builtin = harnessName.startsWith(SERVED_PREFIX) ? harnessName.slice(SERVED_PREFIX.length) : harnessName
    const packages = builtins.get(builtin)
    if (packages) return resolver.builtin(builtin, packages)
    const tool = own.get(harnessName)
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
