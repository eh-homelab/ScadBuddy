import { randomUUID } from 'node:crypto'
import { z } from 'zod'
import { ok } from './call.js'
import { answered, command } from './command.js'
import { page, PAGED, pageInput } from './pagination.js'
import { defineTool, json, text, type Tool } from './registry.js'

// Flows (#1057, spec 2026-10-01 §7): a Python script over ScadBuddy's host functions,
// registered by name and run durably as a ProjectWorkflow. Routes:
// backend/scadbuddy/api/flows.py. Deleting a run is a person's (coverage.ts).

export const definitionId = z
  .string()
  .uuid()
  .describe('A flow version id, as register_flow, list_flows or list_flow_versions returns it')
const flowName = z.string().regex(/^[a-z0-9][a-z0-9-]{0,62}$/).describe("A flow's name")
const flowCommit = z
  .string()
  .regex(/^[0-9a-f]{7,40}$/)
  .describe("One of the flow's versions' commits, as list_flow_versions returns it")
const runId = z.string().uuid().describe('A flow run id, as start_flow_run returns it')
export const grantRule = z
  .object({
    fn: z.enum(['queue_print', 'arrange', 'tool']),
    tool: z.string().optional().describe("For fn `tool`: the outward tool's name"),
    printers: z.array(z.number().int()).optional().describe('queue_print: printer ids it may use'),
    models: z.array(z.string()).optional().describe('queue_print, arrange: template slugs'),
    library_files: z.array(z.number().int()).optional().describe("queue_print: Bambuddy library file ids"),
    max_prints: z.number().int().min(0).optional(),
    max_filament_g: z.number().min(0).optional(),
    max_calls: z.number().int().min(0).optional(),
    args: z
      .array(z.object({ path: z.string(), in: z.array(z.union([z.string(), z.number(), z.boolean(), z.null()])).min(1).max(256) }))
      .optional()
      .describe("tool: JSON pointers into the call's arguments and the values each may hold"),
  })
  .strict()
const grantSpec = z
  .object({
    expires_at: z.string().datetime({ offset: true }).describe('ISO 8601 with its offset; at most 7 days ahead (24 h is the usual)'),
    sessions: z.boolean().default(false).describe('Whether it covers the outward calls of sessions the run starts'),
    ask_starter_session: z.boolean().default(true).describe('Whether calls outside it go to this session first'),
    rules: z.array(grantRule).max(32),
  })
  .strict()
  .describe(
    "A proposed run grant: outward calls inside a rule's bounds would run without asking. An agent's grant is " +
      'only requested: it does nothing until the user approves it (maybe edited) on the run page.',
  )
const inputsSchema = z
  .record(z.string(), z.unknown())
  .describe(
    "The flow's typed inputs (#2483), a JSON Schema committed beside the script as inputs.json: " +
      '{"type": "object", "properties": {...}, "required": [...]}, each property a string, integer, number, ' +
      'boolean or array of one of them, with an optional `description`, `default` and its type\'s constraints ' +
      '(a string or integer may be an `enum`). The script reads them with `inputs()`, typed by it.',
  )
export const runInputs = z
  .record(z.string(), z.unknown())
  .describe(
    "The run's inputs by name, as the flow's `inputs_schema` (get_flow) declares them; one left out takes " +
      'its default. Refused (422 with `problems`) when they do not fit, or for a flow with no inputs schema.',
  )
export const approvalTimeout = z
  .union([z.number().int().min(10).max(2_592_000), z.literal('never')])
  .describe('Seconds an outward host call waits for a person to approve it before it is denied, or "never"')

export const flowTools: Tool[] = [
  defineTool({
    name: 'register_flow',
    description:
      'Register a flow: a Python script that runs durably, for as long as it needs, over the host functions ' +
      '`sleep(seconds)`, `wait_for_human(question, timeout_s)` (10 to 86400 s, default 3600; returns {answer}), ' +
      '`render(slug, params)` (returns {job_id, status, error}; status done, failed or cancelled), ' +
      '`save_output(slug, job_id, name)` (returns the output id), and two that wait for a person to approve them ' +
      'in ScadBuddy: `queue_print(source, request)` (source {output_id} or {file_id}; request is the print ' +
      "route's body, filament_plan and choices; returns {run_id, status, may_have_queued, error}) and " +
      "`arrange(request)` (the arrange route's body; returns {job_id, status, output_id, error}). " +
      '`agent(prompt, skills, result_schema, images=None)` starts a durable agent session as the run\'s starter and returns ' +
      'its answer (skills a list, maybe empty; with a JSON Schema the JSON value it answered, else its text), and ' +
      '`ask_session(session_id, message, images=None)` sends to a durable session the starter owns and returns its answer; ' +
      "a session's own outward calls wait for approval in its chat. `tool(name, args)` calls a tool as the " +
      "run's starter and returns its text: one of ScadBuddy's, or an enabled plugin's or http_request named " +
      '`<server>__<tool>` (e.g. `scadbuddy_http__http_request`); an outward one waits for approval. ' +
      '`tool_result(name, args)` returns {text, images}, each image a pointer {name, mediaType, bytes} kept with ' +
      'the run (never its bytes), which `agent` and `ask_session` send to a session as `images`, and a ' +
      'script may return (`agent_with_images` and `ask_session_with_images` are deprecated names for them); the ' +
      "run page shows the run's images. Results are " +
      'dicts; a refusal is an exception in ScadBuddy\'s words, and a denial a ToolApprovalDenied. The script ' +
      'is `import asyncio`, an `async def main()` that awaits host functions, and `asyncio.run(main())`; it may ' +
      'compute for at most a second between host calls. It is type-checked first: a 422 lists `problems` by line. ' +
      'Registering an existing `name` adds its next version, one commit in its history (list_flow_versions), ' +
      'an unchanged script too; a version never changes. `approval_timeout` sets how long its runs wait for ' +
      'approvals (omit for the global setting). With `inputs_schema`, each run takes typed inputs, which the ' +
      "script reads with `inputs()` (a TypedDict `FlowInputs`, defaults filled in); a 422 lists the schema's " +
      'problems with `file` inputs.json.',
    input: z.object({
      name: z.string().regex(/^[a-z0-9][a-z0-9-]{0,62}$/, 'lower-case letters, digits and dashes'),
      script: z.string().min(1).max(65_536),
      approval_timeout: approvalTimeout.optional(),
      inputs_schema: inputsSchema.optional(),
    }),
    risk: 'write',
    routes: ['POST /api/v1/workflows'],
    handler: async (body, { backend }) =>
      json(await ok(backend.POST('/api/v1/workflows', { body }), `register flow ${body.name}`)),
  }),

  defineTool({
    name: 'check_flow_script',
    description:
      'Type-check a flow script as register_flow would, without registering it: no version, no commit. ' +
      'Answers {problems}, each with its 1-based `line` (null for the whole script) and `message`; empty when ' +
      'register_flow would accept it. With `inputs_schema`, the schema is checked too (problems with `file` ' +
      'inputs.json) and `inputs()` is typed by it.',
    input: z.object({ script: z.string().min(1).max(65_536), inputs_schema: inputsSchema.optional() }),
    risk: 'read',
    routes: ['POST /api/v1/workflow-script/check'],
    handler: async (body, { backend }) =>
      json(await ok(backend.POST('/api/v1/workflow-script/check', { body }), 'check flow script')),
  }),

  defineTool({
    name: 'list_flows',
    description:
      'The registered flows, each at its newest version (no script; get_flow has it). Archived and deleted ' +
      'flows only with `include_archived`, each with `archived_at`/`archived_by` or `deleted_at`/`deleted_by`.' +
      PAGED,
    input: z.object({
      include_archived: z.boolean().optional().describe('Also list archived and deleted flows'),
      ...pageInput,
    }),
    risk: 'read',
    routes: ['GET /api/v1/workflows'],
    handler: async ({ include_archived, ...args }, { backend }) => {
      const query = include_archived ? { archived: true } : {}
      const items = await ok(backend.GET('/api/v1/workflows', { params: { query } }), 'list flows')
      return json(page(items, { include_archived, ...args }, (item) => item.id, 'list_flows'))
    },
  }),

  defineTool({
    name: 'archive_flow',
    description:
      'Archive a flow, every version of it: it leaves the Flows list (list_flows shows it with ' +
      '`include_archived`) and no run of it starts until unarchive_flow. Runs already started keep running, ' +
      'and finished runs keep their script. Reversible.',
    input: z.object({ name: flowName }),
    risk: 'write',
    routes: ['POST /api/v1/workflows/names/{name}/archive'],
    handler: async ({ name }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/workflows/names/{name}/archive', { params: { path: { name } } }),
          `archive flow ${name}`,
        ),
      ),
  }),

  defineTool({
    name: 'unarchive_flow',
    description: 'Unarchive a flow: it is listed again, and its versions start again.',
    input: z.object({ name: flowName }),
    risk: 'write',
    routes: ['POST /api/v1/workflows/names/{name}/unarchive'],
    handler: async ({ name }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/workflows/names/{name}/unarchive', { params: { path: { name } } }),
          `unarchive flow ${name}`,
        ),
      ),
  }),

  defineTool({
    name: 'delete_flow',
    description:
      "Delete a flow: one commit removing its script from the flows' git history, which keeps every version. " +
      'Nothing new starts; runs in progress keep running the version they started, and finished runs still ' +
      'show the script they ran. restore_flow_version on one of its versions (list_flow_versions) brings it ' +
      'back as its next version. Needs a human approval.',
    input: z.object({ name: flowName }),
    risk: 'outward',
    routes: ['DELETE /api/v1/workflows/names/{name}'],
    summarize: ({ name }) => `Delete the flow "${name}" (its history keeps every version)`,
    handler: async ({ name }, { backend }) =>
      json(
        await ok(
          backend.DELETE('/api/v1/workflows/names/{name}', { params: { path: { name } } }),
          `delete flow ${name}`,
        ),
      ),
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
    name: 'list_flow_versions',
    description:
      "A flow's history, newest first, from git: each version's id, commit, version number, message and who " +
      'made it (a person, or an agent with its principal and session). Pass a version id to get_flow or ' +
      'start_flow_run.' +
      PAGED,
    input: z.object({ name: flowName, ...pageInput }),
    risk: 'read',
    routes: ['GET /api/v1/workflows/names/{name}/versions'],
    handler: async ({ name, ...args }, { backend }) => {
      const items = await ok(
        backend.GET('/api/v1/workflows/names/{name}/versions', {
          params: { path: { name }, query: { limit: 500 } },
        }),
        `list versions of flow ${name}`,
      )
      return json(page(items, { name, ...args }, (item) => item.commit, 'list_flow_versions'))
    },
  }),

  defineTool({
    name: 'get_flow_version_source',
    description: "A flow's script at one of its versions, by commit.",
    input: z.object({ name: flowName, commit: flowCommit }),
    risk: 'read',
    routes: ['GET /api/v1/workflows/names/{name}/versions/{commit}/source'],
    handler: async ({ name, commit }, { backend }) =>
      text(
        await ok(
          backend.GET('/api/v1/workflows/names/{name}/versions/{commit}/source', {
            params: { path: { name, commit } },
            parseAs: 'text',
          }),
          `get flow ${name}@${commit}`,
        ),
      ),
  }),

  defineTool({
    name: 'diff_flow_version',
    description:
      "The unified diff of a flow's script at a version against the version before it, or against `base` " +
      '(another of its versions) when given.',
    input: z.object({ name: flowName, commit: flowCommit, base: flowCommit.optional() }),
    risk: 'read',
    routes: ['GET /api/v1/workflows/names/{name}/versions/{commit}/diff'],
    handler: async ({ name, commit, base }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/workflows/names/{name}/versions/{commit}/diff', {
            params: { path: { name, commit }, query: { base } },
          }),
          `diff flow ${name}@${commit}`,
        ),
      ),
  }),

  defineTool({
    name: 'restore_flow_version',
    description:
      "Restore a flow's script at one of its versions as its newest version: a new commit and version, " +
      'type-checked first, with that version\'s approval timeout (nothing is lost). Runs already started keep ' +
      'the version they ran.',
    input: z.object({ name: flowName, commit: flowCommit }),
    risk: 'write',
    routes: ['POST /api/v1/workflows/names/{name}/versions/{commit}/restore'],
    handler: async ({ name, commit }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/workflows/names/{name}/versions/{commit}/restore', {
            params: { path: { name, commit } },
          }),
          `restore flow ${name}@${commit}`,
        ),
      ),
  }),

  defineTool({
    name: 'start_flow_run',
    description:
      'Start a run of a flow version. Answered at once with the run id (status `starting`): follow it with ' +
      'get_flow_run. The script is checked again first (422 with `problems` if the host functions changed). ' +
      '`approval_timeout` overrides the flow\'s for this run. The run is recorded as started by this session. ' +
      '`grant` proposes what its outward calls may do without asking; it is requested, never approved, until ' +
      'the user approves it on the run page. The run starts at once, and its outward calls wait meanwhile. ' +
      '`inputs` are the run\'s typed inputs, recorded on the run; a Reset replays the same ones.',
    input: z.object({
      definition_id: definitionId,
      approval_timeout: approvalTimeout.optional(),
      grant: grantSpec.optional(),
      inputs: runInputs.optional(),
    }),
    risk: 'write',
    routes: ['POST /api/v1/workflows/{definition_id}/runs'],
    handler: async ({ definition_id, approval_timeout, grant, inputs }, ctx) => {
      // One key for this call, re-sent after an answer that never came, so a re-send reaches the same run.
      const headers = { 'Idempotency-Key': randomUUID().replaceAll('-', '') }
      const what = `start flow ${definition_id}`
      const sent = await answered(
        ctx,
        () =>
          ctx.backend.POST('/api/v1/workflows/{definition_id}/runs', {
            params: { path: { definition_id } },
            body:
              approval_timeout === undefined && grant === undefined && inputs === undefined
                ? null
                : {
                    ...(approval_timeout === undefined ? {} : { approval_timeout }),
                    ...(grant === undefined ? {} : { grant }),
                    ...(inputs === undefined ? {} : { inputs }),
                  },
            headers,
          }),
        what,
        ' It may have started anyway: check list_flow_runs before trying again.',
      )
      return json(await ok(Promise.resolve(sent), what))
    },
  }),

  defineTool({
    name: 'revoke_flow_grant',
    description:
      "Revoke a flow run's grant, for a run this principal started: final, and the run goes on, its next " +
      'outward call waiting for a decision. Revoking only takes authority away; approving a grant is the ' +
      "user's alone.",
    input: z.object({ run_id: runId }),
    risk: 'write',
    routes: ['POST /api/v1/workflow-runs/{run_id}/grant/revoke'],
    handler: async ({ run_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/workflow-runs/{run_id}/grant/revoke', { params: { path: { run_id } } }),
          `revoke the grant of flow run ${run_id}`,
        ),
      ),
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
      '(`valid`; get_flow_reset_points lists them). Every host call after the point runs again; answers and approvals after it are undone. ' +
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
    name: 'get_flow_reset_points',
    description:
      'Every point a Reset of a flow run takes (`event_id`s of its current execution, oldest first), each with ' +
      '`first_step`, the seq of the first step a Reset there runs again or reopens (null: none). Preview one ' +
      'with preview_flow_reset before reset_flow_run.',
    input: z.object({ run_id: runId }),
    risk: 'read',
    routes: ['GET /api/v1/workflow-runs/{run_id}/reset-points'],
    handler: async ({ run_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/workflow-runs/{run_id}/reset-points', { params: { path: { run_id } } }),
          `reset points of flow run ${run_id}`,
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
