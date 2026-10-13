import { setTimeout as sleep } from 'node:timers/promises'
import { z } from 'zod'
import { hasTier } from '../auth/principal.js'
import { principalHeader } from './authorship.js'
import { ok } from './call.js'
import { defineTool, json, ToolError, type ToolContext } from './registry.js'

const STATUS = z.enum(['queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled'])
const FINISHED = new Set(['succeeded', 'failed', 'cancelled'])
const NO_NUL = (max: number) => z.string().max(max).refine((v) => !v.includes('\0'), 'must not hold a NUL byte')
const ID = z.string().min(3).max(200).describe('An activity id, e.g. "render:<id>" or "operation:<id>"')

/**
 * Spec 2026-10-09 §7: a caller without `outward` sees its own activity only. The pin is
 * `principalHeader`, the value the backend stored: it cuts at 300 characters and leaves a
 * literal `%` unescaped, so two ids that share their first 300 encoded characters, or a
 * subject holding `%20` against one holding a space, are one principal here.
 */
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
    // The backend's own answer for a missing row: another principal's row is not told apart from it.
    throw new ToolError(`get activity ${activity_id} failed (HTTP 404)`, 404, `no activity '${activity_id}'`)
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
      kind: NO_NUL(100).optional(),
      status: STATUS.optional(),
      principal: z.string().max(300).optional().describe('Only with the outward tier; otherwise yours'),
      subject: NO_NUL(200).optional(),
      since: z.string().datetime({ offset: true }).optional(),
      cursor: NO_NUL(400).optional(),
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
    waitsMs: ({ wait_seconds }) => wait_seconds * 1000,
    handler: async ({ activity_id, wait_seconds }, ctx) => {
      const deadline = Date.now() + wait_seconds * 1000
      let found = await read(ctx, activity_id)
      while (!FINISHED.has(found.row.status) && Date.now() < deadline && !ctx.signal.aborted) {
        try {
          await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
        } catch (err) {
          if (ctx.signal.aborted) break
          throw err
        }
        found = await read(ctx, activity_id)
        await ctx.progress(0, undefined, found.row.status)
      }
      return json(found)
    },
  }),
]
