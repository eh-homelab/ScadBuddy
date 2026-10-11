import { mkdir } from 'node:fs/promises'
import path from 'node:path'
import type { AuditSink } from '../audit/log.js'
import { HTTP_SERVER, httpRequestEnabled, httpRequestServer, httpTierOf, SETTING_HTTP_REQUEST } from '../harness/httpRequest.js'
import type { HttpLimits } from '../harness/httpRequest.js'
import type { Resolver } from '../http/egress.js'
import type { PluginsForRun } from '../plugins/forwarder.js'
import { pluginTierResolver } from '../plugins/registry.js'
import { connectHttp, connectInProcess, type TurnContext, type TurnServer, type TurnServers } from './turnTools.js'

// What a durable turn's other tools are built from (#2320): the same pieces a classic
// turn's `runTurn` hands Claude Code (sessions/manager.ts), built again for each listing
// and each call, so a plugin disabled, removed or failing its egress check since is gone
// at once, as it would be from a classic turn's next query.

export type TurnServerDeps = {
  /** The enabled remote plugins through the loopback forwarder (main.ts `remotePlugins`). */
  remotePlugins?: (() => Promise<PluginsForRun>) | undefined
  settings?: { get<T>(key: string): Promise<T | undefined> } | undefined
  /** http_request, as a classic turn has it; its saved bodies under `saveRoot/<session>/http`. */
  httpRequest?:
    | {
        saveRoot: string
        /** What no request may carry: every usable credential's secret (fallback.ts candidates). */
        secrets: () => Promise<readonly string[]>
        audit?: AuditSink | undefined
        resolve?: Resolver
        limits?: Partial<HttpLimits>
      }
    | undefined
}

export function turnServers(deps: TurnServerDeps): (context: TurnContext) => Promise<TurnServers> {
  return async (context) => {
    const servers: TurnServer[] = []
    const forwarded = deps.remotePlugins ? await deps.remotePlugins() : undefined
    const secrets: string[] = [...(forwarded?.secrets ?? [])]
    for (const plugin of forwarded?.plugins ?? []) {
      const tiers = pluginTierResolver([plugin])
      servers.push({
        name: plugin.name,
        connect: () => connectHttp(plugin.url),
        // An unlisted plugin tool is outward (registry.ts), as in a classic turn.
        tierOf: (name) => tiers(name) ?? 'outward',
      })
    }
    const http = deps.httpRequest
    if (http && httpRequestEnabled(await deps.settings?.get<unknown>(SETTING_HTTP_REQUEST))) {
      secrets.push(...(await http.secrets()))
      // On the payload volume both sidecars and every replica share, so a body saved by
      // one call is read by http_response_read on whichever replica the next call runs.
      const saveDir = path.join(http.saveRoot, context.session, 'http')
      await mkdir(saveDir, { recursive: true })
      servers.push({
        name: HTTP_SERVER,
        connect: () =>
          connectInProcess(
            () =>
              httpRequestServer({
                saveDir,
                secrets: () => secrets,
                audit: http.audit,
                actor: context.owner,
                sessionId: context.session,
                signal: context.signal,
                ...(http.resolve ? { resolve: http.resolve } : {}),
                ...(http.limits ? { limits: http.limits } : {}),
              }).instance,
          ),
        tierOf: (name, input) => httpTierOf(name, input) ?? 'outward',
      })
    }
    return {
      servers,
      problems: forwarded?.problems ?? [],
      release: () => forwarded?.release(),
    }
  }
}
