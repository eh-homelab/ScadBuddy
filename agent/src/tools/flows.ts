import { randomUUID } from 'node:crypto'
import { z } from 'zod'
import { ok } from './call.js'
import { answered, command } from './command.js'
import { page, PAGED, pageInput } from './pagination.js'
import { defineTool, json, type Tool } from './registry.js'

// Flows (#1057, spec 2026-10-01 §7): a Python script over ScadBuddy's host functions,
// registered by name and run durably as a ProjectWorkflow. Routes:
// backend/scadbuddy/api/flows.py. Deleting a run is a person's (coverage.ts).

const definitionId = z.string().uuid().describe('A flow version id, as register_flow or list_flows returns it')
const runId = z.string().uuid().describe('A flow run id, as start_flow_run returns it')
const approvalTimeout = z
  .union([z.number().int().min(10).max(2_592_000), z.literal('never')])
  .describe('Seconds an outward host call waits for a person to approve it before it is denied, or "never"')

export const flowTools: Tool[] = [
  defineTool({
    name: 'register_flow',
    description:
      'Register a flow: a Python script that runs durably, for as long as it needs, over the host functions ' +
      '`sleep(seconds)`, `wait_for_human(question, timeout_s)` (10 to 86400 s; returns {answer}), ' +
      '`render(slug, params)` (returns {job_id, status, error}; status done, failed or cancelled), ' +
      '`save_output(slug, job_id, name)` (returns the output id), and two that wait for a person to approve them ' +
      'in ScadBuddy: `queue_print(source, request)` (source {output_id} or {file_id}; request is the print ' +
      "route's body, filament_plan and choices; returns {run_id, status, may_have_queued, error}) and " +
      "`arrange(request)` (the arrange route's body; returns {job_id, status, output_id, error}). " +
      '`agent(prompt, skills, result_schema)` starts a durable agent session as the run\'s starter and returns ' +
      'its answer (skills a list, maybe empty; with a JSON Schema the JSON value it answered, else its text), and ' +
      '`ask_session(session_id, message)` sends to a durable session the starter owns and returns its answer; ' +
      "a session's own outward calls wait for approval in its chat. Results are " +
      'dicts; a refusal is an exception in ScadBuddy\'s words, and a denial a ToolApprovalDenied. The script ' +
      'is `import asyncio`, an `async def main()` that awaits host functions, and `asyncio.run(main())`; it may ' +
      'compute for at most a second between host calls. It is type-checked first: a 422 lists `problems` by line. ' +
      'Registering an existing `name` adds its next version; a version never changes. `approval_timeout` sets ' +
      'how long its runs wait for approvals (omit for the global setting).',
    input: z.object({
      name: z.string().regex(/^[a-z0-9][a-z0-9-]{0,62}$/, 'lower-case letters, digits and dashes'),
      script: z.string().min(1).max(65_536),
      approval_timeout: approvalTimeout.optional(),
    }),
    risk: 'write',
    routes: ['POST /api/v1/workflows'],
    handler: async (body, { backend }) =>
      json(await ok(backend.POST('/api/v1/workflows', { body }), `register flow ${body.name}`)),
  }),

  defineTool({
    name: 'list_flows',
    description: 'The registered flows, each at its newest version (no script; get_flow has it).' + PAGED,
    input: z.object({ ...pageInput }),
    risk: 'read',
    routes: ['GET /api/v1/workflows'],
    handler: async (args, { backend }) => {
      const items = await ok(backend.GET('/api/v1/workflows'), 'list flows')
      return json(page(items, args, (item) => item.id, 'list_flows'))
    },
  }),

  defineTool({
    name: 'get_flow',
    description: 'One flow version, with its script.',
    input: z.object({ definition_id: definitionId }),
    risk: 'read',
    routes: ['GET /api/v1/workflows/{definition_id}'],
    handler: async ({ definition_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/workflows/{definition_id}', { params: { path: { definition_id } } }),
          `get flow ${definition_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'start_flow_run',
    description:
      'Start a run of a flow version. Answered at once with the run id (status `starting`): follow it with ' +
      'get_flow_run. The script is checked again first (422 with `problems` if the host functions changed). ' +
      '`approval_timeout` overrides the flow\'s for this run. The run is recorded as started by this session.',
    input: z.object({ definition_id: definitionId, approval_timeout: approvalTimeout.optional() }),
    risk: 'write',
    routes: ['POST /api/v1/workflows/{definition_id}/runs'],
    handler: async ({ definition_id, approval_timeout }, ctx) => {
      // One key for this call, re-sent after an answer that never came, so a re-send reaches the same run.
      const headers = { 'Idempotency-Key': randomUUID().replaceAll('-', '') }
      const what = `start flow ${definition_id}`
      const sent = await answered(
        ctx,
        () =>
          ctx.backend.POST('/api/v1/workflows/{definition_id}/runs', {
            params: { path: { definition_id } },
            body: approval_timeout === undefined ? null : { approval_timeout },
            headers,
          }),
        what,
        ' It may have started anyway: check list_flow_runs before trying again.',
      )
      return json(await ok(Promise.resolve(sent), what))
    },
  }),

  defineTool({
    name: 'list_flow_runs',
    description:
      'Flow runs, newest first: each with its status (running, waiting, succeeded, failed, terminated), steps ' +
      '(one per host call) and result. Filter by flow version or by the agent session that started them; the ' +
      '200 newest match.' +
      PAGED,
    input: z.object({
      definition_id: definitionId.optional(),
      session: z.string().min(1).max(64).optional(),
      ...pageInput,
    }),
    risk: 'read',
    routes: ['GET /api/v1/workflow-runs'],
    handler: async (args, { backend }) => {
      const query = { definition_id: args.definition_id, session: args.session, limit: 200 }
      const items = await ok(backend.GET('/api/v1/workflow-runs', { params: { query } }), 'list flow runs')
      return json(page(items, args, (item) => item.id, 'list_flow_runs'))
    },
  }),

  defineTool({
    name: 'get_flow_run',
    description:
      'One flow run: its record, and `pending`, what it waits on now (a question for a person, or an approval). ' +
      'People answer and approve on the Workflows page, never through a tool. To go back to an earlier point, ' +
      'see preview_flow_reset.',
    input: z.object({ run_id: runId }),
    risk: 'read',
    routes: ['GET /api/v1/workflow-runs/{run_id}'],
    handler: async ({ run_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/workflow-runs/{run_id}', { params: { path: { run_id } } }),
          `get flow run ${run_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'preview_flow_reset',
    description:
      'What resetting a flow run to an earlier point would send again: its outward calls (queue_print, arrange, an outward tool, an agent turn that made one) ' +
      'after `event_id`, a completed workflow task of the run\'s Temporal history after its script started ' +
      '(`valid`). Every host call after the point runs again; answers and approvals after it are undone. ' +
      'Pass the answer\'s `as_of_event_id` and `workflow_run_id` to reset_flow_run.',
    input: z.object({ run_id: runId, event_id: z.number().int().min(1) }),
    risk: 'read',
    routes: ['GET /api/v1/workflow-runs/{run_id}/reset-preview'],
    handler: async ({ run_id, event_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/workflow-runs/{run_id}/reset-preview', {
            params: { path: { run_id }, query: { event_id } },
          }),
          `preview reset of flow run ${run_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'reset_flow_run',
    description:
      'Reset a flow run to `event_id` (Temporal Reset): it continues from there on the current code, and every ' +
      'host call after the point runs again, its outward ones as new prints or arranges. Refused (409, with the ' +
      'new preview) when outward calls landed after the preview\'s `as_of_event_id`, or another Reset replaced ' +
      'its `workflow_run_id`. Needs a human approval.',
    input: z.object({
      run_id: runId,
      event_id: z.number().int().min(1),
      as_of_event_id: z.number().int().min(0).describe('From preview_flow_reset'),
      workflow_run_id: z.string().min(1).max(64).describe('From preview_flow_reset'),
    }),
    risk: 'outward',
    routes: ['POST /api/v1/workflow-runs/{run_id}/reset'],
    summarize: ({ run_id, event_id }) => `Reset flow run ${run_id} to event ${event_id}`,
    handler: async ({ run_id, event_id, as_of_event_id, workflow_run_id }, ctx) =>
      json(
        await command(ctx, `reset flow run ${run_id}`, (headers) =>
          ctx.backend.POST('/api/v1/workflow-runs/{run_id}/reset', {
            params: { path: { run_id } },
            body: { event_id, as_of_event_id, workflow_run_id },
            headers,
          }),
        ),
      ),
  }),
]
