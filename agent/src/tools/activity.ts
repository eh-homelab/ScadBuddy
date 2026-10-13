import { setTimeout as sleep } from 'node:timers/promises'
import { z } from 'zod'
import { hasTier } from '../auth/principal.js'
import { principalHeader } from './authorship.js'
import { ok } from './call.js'
import { defineTool, json, ToolError, type ToolContext } from './registry.js'

const STATUS = z.enum(['queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled'])
const FINISHED = new Set(['succeeded', 'failed', 'cancelled'])
const ID = z.string().min(3).max(200).describe('An activity id, e.g. "render:<id>" or "operation:<id>"')

/** Spec 2026-10-09 §7: a caller without `outward` sees its own activity only. */
function pinned(ctx: ToolContext, asked: string | undefined): string | undefined {
  return hasTier(ctx.principal, 'outward') ? asked : principalHeader(ctx.principal.id)
}

async function read(ctx: ToolContext, activity_id: string) {
  const found = await ok(
    ctx.backend.GET('/api/v1/activity/{activity_id}', { params: { path: { activity_id } } }),
    `get activity ${activity_id}`,
  )
  if (hasTier(ctx.principal, 'outward')) return found
  const mine = principalHeader(ctx.principal.id)
  if (found.row.principal !== mine) {
    throw new ToolError(`activity ${activity_id} was not started by this caller`, 404)
  }
  return { ...found, children: found.children.filter((child) => child.principal === mine) }
}

export const activityTools = [
  defineTool({
    name: 'activity_list',
    description:
      'List background work (renders, operations, print runs, agent operations), newest first, with its status ' +
      'and who started it. Without the outward tier only your own.',
    input: z.object({
      kind: z.string().max(100).optional(),
      status: STATUS.optional(),
      principal: z.string().max(300).optional().describe('Only with the outward tier; otherwise yours'),
      subject: z.string().max(200).optional(),
      since: z.string().datetime().optional(),
      cursor: z.string().max(400).optional(),
      limit: z.number().int().min(1).max(200).optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/activity'],
    handler: async (args, ctx) => {
      const { kind, status, ...rest } = args
      const query = {
        ...rest,
        kind: kind === undefined ? undefined : [kind],
        status: status === undefined ? undefined : [status],
        principal: pinned(ctx, args.principal),
      }
      return json(await ok(ctx.backend.GET('/api/v1/activity', { params: { query } }), 'list activity'))
    },
  }),
  defineTool({
    name: 'activity_get',
    description: 'Read one piece of background work: status, stages, result or error, and what it made.',
    input: z.object({ activity_id: ID }),
    risk: 'read',
    routes: ['GET /api/v1/activity/{activity_id}'],
    handler: async ({ activity_id }, ctx) => json(await read(ctx, activity_id)),
  }),
  defineTool({
    name: 'activity_watch',
    description: 'Wait up to wait_seconds for a piece of background work to finish, then return it.',
    input: z.object({ activity_id: ID, wait_seconds: z.number().int().min(1).max(600).default(60) }),
    risk: 'read',
    routes: ['GET /api/v1/activity/{activity_id}'],
    handler: async ({ activity_id, wait_seconds }, ctx) => {
      const deadline = Date.now() + wait_seconds * 1000
      let found = await read(ctx, activity_id)
      while (!FINISHED.has(found.row.status) && Date.now() < deadline && !ctx.signal.aborted) {
        await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
        found = await read(ctx, activity_id)
        await ctx.progress(0, undefined, found.row.status)
      }
      return json(found)
    },
  }),
]
