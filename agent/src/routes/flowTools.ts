import type { Hono } from 'hono'
import { isLoopbackPeer } from '../http/origins.js'
import { flowToolTiers } from '../temporal/flowTools.js'
import type { Tool, ToolServices } from '../tools/registry.js'
import type { RouteModule } from './module.js'

// The tools a flow may call, at their tiers now (#2704, temporal/flowTools.ts), for the
// API in this pod: it checks a run grant's tools and lists the editor's with them, and
// refuses both when this does not answer. Read-only.
//
// Who calls: only a loopback peer (the API container shares the pod's network; the
// ingress is never loopback), and never through a proxy that names another client.
// Outside /api/v1/ai/, the prefix the ingress routes to the agent (docs/ai/operating.md
// §1.1), so no ingress or dev proxy reaches it either.

export const FLOW_TOOLS_PATH = '/internal/flows/tools'

export type FlowToolsDeps = { tools: readonly Tool[]; services: Pick<ToolServices, 'toolOverrides'> }

declare module '../app.js' {
  interface AppDeps {
    flowTools?: FlowToolsDeps | undefined
  }
}

export const route: RouteModule = {
  register(app: Hono, deps) {
    app.get(FLOW_TOOLS_PATH, async (c) => {
      const flow = deps.flowTools
      if (!flow || !isLoopbackPeer(deps.remoteAddress(c)) || c.req.header('x-forwarded-for') !== undefined) {
        return c.json({ error: 'not found' }, 404)
      }
      return c.json({ tools: await flowToolTiers(flow.tools, flow.services) })
    })
  },
}
