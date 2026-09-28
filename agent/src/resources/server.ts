import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import {
  type CallToolResult,
  CompleteRequestSchema,
  ErrorCode,
  ListResourcesRequestSchema,
  ListResourceTemplatesRequestSchema,
  McpError,
  ReadResourceRequestSchema,
  type ReadResourceResult,
  type Resource,
  SubscribeRequestSchema,
  UnsubscribeRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'
import { hasTier, type Principal } from '../auth/principal.js'
import { principalFrom } from '../tools/projections.js'
import { type Tool, type ToolContext, ToolError, type ToolServices } from '../tools/registry.js'
import { expand, isTemplate, matchUri, RESOURCES, type ResourceDef, tierFor, variablesOf } from './catalog.js'
import { type ResourceHub, SubscriptionLimitError } from './hub.js'

// The resource half of the `/mcp` server (issue #264): `resources/list`,
// `resources/templates/list`, `resources/read`, `resources/subscribe`,
// `resources/unsubscribe` and `completion/complete` for template arguments
// (https://modelcontextprotocol.io/specification/2025-11-25/server/resources,
// https://modelcontextprotocol.io/specification/2025-11-25/server/utilities/completion).
//
// Installed on the low-level `Server` rather than through McpServer's
// `registerResource`, because that API can neither filter the list by the
// caller's tier nor handle subscriptions, which it leaves to the server.
//
// AUTHORIZATION. Every request resolves its principal the way tool calls do
// (`principalFrom`, set by src/mcp/http.ts from `authenticate`), re-read per
// request so a revoked token stops at once. Reading and subscribing need the
// resource's tier (catalog.ts `tierFor`): its backing tool's (`read` for all
// of them) or higher (`write` for settings). A caller never sees a resource it
// may not read in `resources/list`. Resources are read-only: nothing here
// calls a tool that is not `read`, which `installResources` checks at start.
//
// ERRORS follow the spec's "Error Handling": -32002 for a resource that does
// not exist (an unknown URI, or a backend 404), -32602 for a bad argument,
// -32603 for anything else.

export const RESOURCE_NOT_FOUND = -32002

type Extra = { signal: AbortSignal; authInfo?: unknown }

export type ResourceDeps = {
  tools: readonly Tool[]
  services: ToolServices
  hub: ResourceHub
}

function notFound(uri: string, why = 'Resource not found'): McpError {
  return new McpError(RESOURCE_NOT_FOUND, why, { uri })
}

/** A tool result as resource contents, in the resource's own MIME type. */
export function toContents(uri: string, def: ResourceDef, result: CallToolResult): ReadResourceResult['contents'] {
  if (result.isError) {
    const text = result.content.find((c) => c.type === 'text')?.text ?? 'the read failed'
    throw new McpError(ErrorCode.InternalError, text, { uri })
  }
  // Over binary.ts's inline limit the tool answers with a link and a JSON
  // note saying where to fetch the bytes; a resource can only carry the note.
  if (result.content.some((c) => c.type === 'resource_link')) {
    const note = result.content.find((c) => c.type === 'text')
    return [{ uri, mimeType: 'application/json', text: note?.type === 'text' ? note.text : '{}' }]
  }
  return result.content.flatMap((c): ReadResourceResult['contents'] => {
    switch (c.type) {
      case 'text':
        return [{ uri, mimeType: def.mimeType, text: c.text }]
      case 'image':
        return [{ uri, mimeType: c.mimeType, blob: c.data }]
      case 'resource':
        return 'blob' in c.resource
          ? [{ uri, mimeType: c.resource.mimeType ?? def.mimeType, blob: c.resource.blob }]
          : [{ uri, mimeType: c.resource.mimeType ?? def.mimeType, text: c.resource.text }]
      default:
        return []
    }
  })
}

/**
 * Wires resources into one `/mcp` session's server and attaches it to the hub.
 * Call before `server.connect`; the returned `detach` stops its notifications.
 */
export function installResources(server: McpServer, deps: ResourceDeps): { detach: () => void } {
  const byName = new Map(deps.tools.map((t) => [t.name, t]))
  const toolOf = (def: ResourceDef): Tool => {
    const tool = byName.get(def.tool)
    if (!tool) throw new Error(`resource ${def.template} names the unknown tool ${def.tool}`)
    if (tool.risk !== 'read') throw new Error(`resource ${def.template} is backed by ${def.tool}, which is not a read tool`)
    return tool
  }
  // Fails at start, not at the first read, when a backing tool is missing.
  for (const def of RESOURCES) toolOf(def)

  const low = server.server
  low.registerCapabilities({ resources: { subscribe: true, listChanged: true }, completions: {} })

  const { subscriptions, detach } = deps.hub.attach({
    updated: (uri) => low.sendResourceUpdated({ uri }),
    listChanged: () => low.sendResourceListChanged(),
  })

  const principalOf = (extra: Extra): Principal => {
    const principal = principalFrom(extra)
    if (!principal) throw new McpError(ErrorCode.InvalidRequest, 'unauthenticated')
    return principal
  }
  const readable = (principal: Principal, def: ResourceDef) => hasTier(principal, tierFor(def, toolOf(def)))
  const ctx = (principal: Principal, extra: Extra): ToolContext => ({
    ...deps.services,
    principal,
    progress: async () => {},
    signal: extra.signal,
  })
  const run = async (def: ResourceDef, vars: Record<string, string>, principal: Principal, extra: Extra) =>
    toolOf(def).execute(def.args ? def.args(vars) : vars, ctx(principal, extra))

  /** The resource `uri` names, checked against the caller's tier. */
  const resolve = (uri: string, principal: Principal) => {
    const match = matchUri(uri)
    if (!match) throw notFound(uri, `Resource not found: ${uri} is not a ScadBuddy resource`)
    const tier = tierFor(match.def, toolOf(match.def))
    if (!hasTier(principal, tier)) {
      throw new McpError(
        ErrorCode.InvalidRequest,
        `${match.def.name} needs the "${tier}" tier; this caller has ${principal.tiers.join(', ') || 'none'}`,
        { uri },
      )
    }
    return match
  }

  const describe = (def: ResourceDef, uri: string, title = def.title): Resource => ({
    uri,
    name: def.name,
    title,
    description: def.description,
    mimeType: def.mimeType,
  })

  low.setRequestHandler(ListResourcesRequestSchema, async (_req, extra) => {
    const principal = principalOf(extra)
    const fixed = RESOURCES.filter((d) => !isTemplate(d) && readable(principal, d)).map((d) => describe(d, d.template))
    // Each model is listed; everything under it is reached by template.
    const modelDef = RESOURCES.find((d) => d.template === 'scadbuddy://models/{slug}')!
    const models = readable(principal, modelDef) ? await slugs(principal, extra) : []
    return {
      resources: [
        ...fixed,
        ...models.map(({ slug, name }) => describe(modelDef, expand(modelDef.template, { slug }), name)),
      ],
    }
  })

  low.setRequestHandler(ListResourceTemplatesRequestSchema, async (_req, extra) => {
    const principal = principalOf(extra)
    return {
      resourceTemplates: RESOURCES.filter((d) => isTemplate(d) && readable(principal, d)).map((d) => ({
        uriTemplate: d.template,
        name: d.name,
        title: d.title,
        description: d.description,
        mimeType: d.mimeType,
      })),
    }
  })

  low.setRequestHandler(ReadResourceRequestSchema, async (req, extra) => {
    const principal = principalOf(extra)
    const { def, vars, uri } = resolve(req.params.uri, principal)
    try {
      return { contents: toContents(req.params.uri, def, await run(def, vars, principal, extra)) }
    } catch (err) {
      if (err instanceof McpError) throw err
      if (err instanceof z.ZodError) {
        throw new McpError(ErrorCode.InvalidParams, `invalid resource URI ${uri}: ${z.prettifyError(err)}`, { uri })
      }
      if (err instanceof ToolError && err.status === 404) throw notFound(req.params.uri, `Resource not found: ${err.message}`)
      if (err instanceof ToolError && err.status === 422) throw new McpError(ErrorCode.InvalidParams, err.message, { uri })
      throw new McpError(ErrorCode.InternalError, err instanceof Error ? err.message : String(err), { uri })
    }
  })

  low.setRequestHandler(SubscribeRequestSchema, async (req, extra) => {
    const principal = principalOf(extra)
    const { uri } = resolve(req.params.uri, principal)
    try {
      subscriptions.add(uri)
    } catch (err) {
      if (err instanceof SubscriptionLimitError) throw new McpError(ErrorCode.InvalidRequest, err.message)
      throw err
    }
    return {}
  })

  low.setRequestHandler(UnsubscribeRequestSchema, async (req, extra) => {
    principalOf(extra)
    // Unsubscribing from something never subscribed is not an error.
    const match = matchUri(req.params.uri)
    if (match) subscriptions.delete(match.uri)
    return {}
  })

  async function slugs(principal: Principal, extra: Extra): Promise<{ slug: string; name: string }[]> {
    const rows = await readJson(byName.get('list_models')!, {}, principal, extra)
    return Array.isArray(rows)
      ? rows.flatMap((r: { slug?: unknown; name?: unknown }) =>
          typeof r.slug === 'string' ? [{ slug: r.slug, name: typeof r.name === 'string' ? r.name : r.slug }] : [],
        )
      : []
  }

  async function readJson(tool: Tool, args: Record<string, unknown>, principal: Principal, extra: Extra): Promise<unknown> {
    const result = await tool.execute(args, ctx(principal, extra))
    const text = result.content.find((c) => c.type === 'text')
    return text?.type === 'text' ? JSON.parse(text.text) : undefined
  }

  /** Candidate values for one template variable; best effort, [] when unavailable. */
  async function candidates(name: string, context: Record<string, string>, principal: Principal, extra: Extra) {
    if (!hasTier(principal, 'read')) return []
    const field = (rows: unknown, key: string) =>
      Array.isArray(rows) ? rows.flatMap((r: Record<string, unknown>) => (typeof r[key] === 'string' ? [r[key]] : [])) : []
    switch (name) {
      case 'slug':
        return (await slugs(principal, extra)).map((m) => m.slug)
      case 'commit':
        return context.slug
          ? field(await readJson(byName.get('list_versions')!, { slug: context.slug, limit: 100 }, principal, extra), 'commit')
          : []
      case 'output_id':
        return context.slug
          ? field(await readJson(byName.get('list_outputs')!, { slug: context.slug }, principal, extra), 'id')
          : []
      default:
        return []
    }
  }

  low.setRequestHandler(CompleteRequestSchema, async (req, extra) => {
    const principal = principalOf(extra)
    const empty = { completion: { values: [], hasMore: false } }
    const { ref, argument, context } = req.params
    if (ref.type !== 'ref/resource') return empty
    const def = RESOURCES.find((d) => d.template === ref.uri)
    if (!def || !variablesOf(def.template).includes(argument.name) || !readable(principal, def)) return empty
    let values: string[]
    try {
      values = await candidates(argument.name, context?.arguments ?? {}, principal, extra)
    } catch {
      return empty
    }
    const matching = values.filter((v) => v.startsWith(argument.value))
    // "Must not exceed 100 items" (CompleteResultSchema).
    return { completion: { values: matching.slice(0, 100), total: matching.length, hasMore: matching.length > 100 } }
  })

  return { detach }
}
