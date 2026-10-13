import { z } from 'zod'
import { ok } from './call.js'
import { approvalTimeout, definitionId, grantRule, runInputs } from './flows.js'
import { defineTool, json, type Tool } from './registry.js'

// Flow triggers (#2484, spec 2026-10-11 §12): runs of a pinned flow version started on a
// Temporal Schedule or on an event, as whoever made the trigger, with that principal's tiers
// at each fire. Routes: backend/scadbuddy/api/flow_triggers.py. Making, editing or resuming
// one, and attaching its standing grant, are outward: each sets up unattended runs. Approving
// or declining a standing grant is the user's alone (coverage.ts).

const triggerId = z.string().uuid().describe('A flow trigger id, as create_flow_trigger or list_flow_triggers returns it')
const schedule = z
  .object({
    cron: z.array(z.string().min(1).max(200)).min(1).max(4).optional().describe('Cron expressions, five to seven fields each'),
    interval_s: z.number().int().min(60).optional().describe('Or every this many seconds'),
    offset_s: z.number().int().min(0).default(0),
    time_zone: z.string().default('UTC').describe('An IANA time zone'),
    catchup_window_s: z.number().int().min(10).max(604_800).default(300).describe('How late a missed fire may still run'),
  })
  .strict()
  .describe('When it fires: `cron` or `interval_s`')
const event = z
  .object({
    kind: z.enum(['render.finished', 'model.changed']),
    slug: z.string().regex(/^[a-z0-9][a-z0-9-]{0,62}$/).optional().describe('Only for this template'),
  })
  .strict()
  .describe('Or the event it fires on')
const overlap = z
  .enum(['skip', 'allow_all'])
  .describe('Whether a fire starts a run while the previous one is still open')
const standingGrant = z
  .object({
    expires_at: z.string().datetime({ offset: true }).describe('ISO 8601 with its offset; at most 30 days ahead'),
    sessions: z.boolean().default(false),
    ask_starter_session: z.boolean().default(false),
    rules: z.array(grantRule).max(32),
  })
  .strict()
  .describe(
    "A proposed standing grant: each fire's run gets a run grant with these rules (fresh limits per run). An " +
      "agent's is only requested: it does nothing until the user approves it on the flow's page.",
  )

export const flowTriggerTools: Tool[] = [
  defineTool({
    name: 'list_flow_triggers',
    description:
      "Flow triggers, newest first, or one flow's: each with its pinned version, when it fires (`schedule`'s " +
      '`next_fires`, or `event`), whether it is paused and why, its standing grant, and its last fires with ' +
      "each run's id and status.",
    input: z.object({ name: z.string().regex(/^[a-z0-9][a-z0-9-]{0,62}$/).optional().describe("Only this flow's") }),
    risk: 'read',
    routes: ['GET /api/v1/flow-triggers'],
    handler: async ({ name }, { backend }) =>
      json(await ok(backend.GET('/api/v1/flow-triggers', { params: { query: { name } } }), 'list flow triggers')),
  }),

  defineTool({
    name: 'get_flow_trigger',
    description: 'One flow trigger, with its next fires, standing grant and last fires.',
    input: z.object({ trigger_id: triggerId }),
    risk: 'read',
    routes: ['GET /api/v1/flow-triggers/{trigger_id}'],
    handler: async ({ trigger_id }, { backend }) =>
      json(
        await ok(backend.GET('/api/v1/flow-triggers/{trigger_id}', { params: { path: { trigger_id } } }), `get flow trigger ${trigger_id}`),
      ),
  }),

  defineTool({
    name: 'create_flow_trigger',
    description:
      'Make a trigger that starts runs of one flow version, always that version, with these inputs: on a ' +
      '`schedule` (cron or interval, time zone, catch-up window) or on an `event` (a render finished, a ' +
      'template changed). Its runs act as this principal, with its tiers as they are at each fire, and ' +
      'without this session. Their outward calls wait for the user unless the user approves a standing ' +
      'grant (attach_flow_trigger_grant).',
    input: z.object({
      definition_id: definitionId,
      schedule: schedule.optional(),
      event: event.optional(),
      overlap: overlap.default('skip'),
      inputs: runInputs.optional(),
      approval_timeout: approvalTimeout.optional(),
    }),
    risk: 'outward',
    routes: ['POST /api/v1/flow-triggers'],
    summarize: ({ definition_id, schedule: when, event: on }) =>
      `Start runs of flow version ${definition_id} ${when ? `on a schedule (${JSON.stringify(when)})` : `on ${on?.kind ?? 'an event'}`}`,
    handler: async (body, { backend }) =>
      json(await ok(backend.POST('/api/v1/flow-triggers', { body }), `create a trigger for ${body.definition_id}`)),
  }),

  defineTool({
    name: 'update_flow_trigger',
    description:
      'Change a trigger this principal made: its version, inputs, schedule or event, overlap or approval ' +
      'timeout. Only what is sent changes. Any change revokes its standing grant: the user approves a new one.',
    input: z.object({
      trigger_id: triggerId,
      definition_id: definitionId.optional(),
      schedule: schedule.optional(),
      event: event.optional(),
      overlap: overlap.optional(),
      inputs: runInputs.optional(),
      approval_timeout: approvalTimeout.optional(),
    }),
    risk: 'outward',
    routes: ['PATCH /api/v1/flow-triggers/{trigger_id}'],
    summarize: ({ trigger_id }) => `Change flow trigger ${trigger_id}`,
    handler: async ({ trigger_id, ...body }, { backend }) =>
      json(
        await ok(
          backend.PATCH('/api/v1/flow-triggers/{trigger_id}', { params: { path: { trigger_id } }, body }),
          `update flow trigger ${trigger_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'pause_flow_trigger',
    description: 'Pause a trigger this principal made: it fires nothing until resumed. Runs already started go on.',
    input: z.object({ trigger_id: triggerId }),
    risk: 'write',
    routes: ['POST /api/v1/flow-triggers/{trigger_id}/pause'],
    handler: async ({ trigger_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/flow-triggers/{trigger_id}/pause', { params: { path: { trigger_id } } }),
          `pause flow trigger ${trigger_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'resume_flow_trigger',
    description: "Resume a paused trigger this principal made. Refused while its flow is archived or deleted.",
    input: z.object({ trigger_id: triggerId }),
    risk: 'outward',
    routes: ['POST /api/v1/flow-triggers/{trigger_id}/resume'],
    summarize: ({ trigger_id }) => `Resume flow trigger ${trigger_id}`,
    handler: async ({ trigger_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/flow-triggers/{trigger_id}/resume', { params: { path: { trigger_id } } }),
          `resume flow trigger ${trigger_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'delete_flow_trigger',
    description:
      'Delete a trigger this principal made, with its Schedule. Its standing grant is revoked, and so are the ' +
      'grants of the runs it started; those runs go on.',
    input: z.object({ trigger_id: triggerId }),
    risk: 'write',
    routes: ['DELETE /api/v1/flow-triggers/{trigger_id}'],
    handler: async ({ trigger_id }, { backend }) => {
      await ok(
        backend.DELETE('/api/v1/flow-triggers/{trigger_id}', { params: { path: { trigger_id } } }),
        `delete flow trigger ${trigger_id}`,
      )
      return json({ deleted: trigger_id })
    },
  }),

  defineTool({
    name: 'attach_flow_trigger_grant',
    description:
      'Propose a standing grant for a trigger this principal made. It is requested only, and does nothing ' +
      "until the user approves it (maybe edited) on the flow's page. 409 while the trigger has a live one.",
    input: z.object({ trigger_id: triggerId, grant: standingGrant }),
    risk: 'outward',
    routes: ['POST /api/v1/flow-triggers/{trigger_id}/grant'],
    summarize: ({ trigger_id }) => `Request a standing grant for flow trigger ${trigger_id}`,
    handler: async ({ trigger_id, grant }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/flow-triggers/{trigger_id}/grant', { params: { path: { trigger_id } }, body: grant }),
          `attach a grant to flow trigger ${trigger_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'revoke_flow_trigger_grant',
    description:
      "Revoke the standing grant of a trigger this principal made: final, and the grants of the runs it " +
      'started are revoked with it, so their next outward calls wait. Approving one is the user\'s alone.',
    input: z.object({ trigger_id: triggerId }),
    risk: 'write',
    routes: ['POST /api/v1/flow-triggers/{trigger_id}/grant/revoke'],
    handler: async ({ trigger_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/flow-triggers/{trigger_id}/grant/revoke', { params: { path: { trigger_id } } }),
          `revoke the grant of flow trigger ${trigger_id}`,
        ),
      ),
  }),
]
