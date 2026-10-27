import { HTTP_TOOL, HTTP_TOOL_NAME } from '../harness/httpRequest.js'
import { toolPrefix } from '../plugins/registry.js'
import { SERVER_NAME } from '../tools/projections.js'
import type { Tool } from '../tools/registry.js'
import { BUILTIN_SOURCES, packageServerSource, registrySource, remoteSource, type ToolAccess } from './model.js'
import type { AccessResolver } from './resolver.js'

/**
 * One call's access as it is now (#2171, spec §4.4), by the name Claude Code gives the
 * tool; undefined where ai_access says nothing of its own for this call, so the turn's
 * resolver stands. A read that fails rejects: the seam then refuses the call.
 */
export type LiveAccess = (harnessName: string) => Promise<ToolAccess | undefined>

/** What a turn offers, so each call is read from its own source. */
export type LiveTurn = {
  tools: readonly Tool[]
  /** The remote plugins' names. */
  remote: readonly string[]
  /** Each package http server: the name its tools carry (packageServerName) and where its access lives. */
  packageServers: readonly { name: string; pkg: string; server: string }[]
}

/**
 * The seam's per-call read (harness/permissions.ts `LiveSeam`): a registry tool's access,
 * a remote plugin's or a package server's tool's (outward unless stored), and
 * http_request's switch and item. Anything else (Skill, Agent, Claude Code's built-ins,
 * http_request with nothing stored) is undefined.
 */
export function liveAccess(resolver: Pick<AccessResolver, 'item' | 'entry'>, turn: LiveTurn): LiveAccess {
  const own = new Map(turn.tools.map((t) => [`${toolPrefix(SERVER_NAME)}${t.name}`, t]))
  const sources: { prefix: string; source: string }[] = [
    ...turn.remote.map((name) => ({ prefix: toolPrefix(name), source: remoteSource(name) })),
    ...turn.packageServers.map((s) => ({ prefix: toolPrefix(s.name), source: packageServerSource(s.pkg, s.server) })),
  ]
  return async (harnessName) => {
    const tool = own.get(harnessName)
    if (tool) return resolver.item(registrySource(tool.name), tool.name, tool.risk)
    if (harnessName === HTTP_TOOL_NAME) {
      const { enabled, access } = await resolver.entry(BUILTIN_SOURCES.http, HTTP_TOOL)
      return enabled ? access : 'off'
    }
    const from = sources.find((s) => harnessName.startsWith(s.prefix))
    return from ? resolver.item(from.source, harnessName.slice(from.prefix.length), 'outward') : undefined
  }
}
