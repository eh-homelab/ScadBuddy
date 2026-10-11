import type { Operation } from './registry.js'

// The openapi coverage allowlist (spec §5.1: "A CI check fails when an
// operation in backend/openapi.json has neither a tool nor an explicit
// allowlist entry"). test/coverage.test.ts enforces it.

const ANALYZERS_LATER =
  'The print analyzers (#284) land API-first; their agent tools are a follow-up (#368 wraps them). ' +
  'Recording a decision or applying a fix writes only to ScadBuddy (write tier) and sends nothing; ' +
  'a send that consumes accepted diffs must go through the outward approval flow (AI spec §8.2).'

/** Backend operations deliberately left without a tool, each with the reason. */
export const NOT_A_TOOL: readonly { operation: Operation; reason: string }[] = [
  {
    operation: 'DELETE /api/v1/workflow-runs/{run_id}',
    reason:
      "Forgets a flow run: its payload key, workflow and row (#1057). A person's decision on the Workflows " +
      'page; the route refuses an agent-authored request.',
  },
  {
    operation: 'GET /api/v1/print/library/{file_id}/objects',
    reason:
      "The Arrange dialog's list of a library file's objects, with a count each (#1863). The arrange " +
      'tool names a whole file (`library_file_id`, part omitted), which places every object at its own count.',
  },
  {
    operation: 'GET /api/v1/models/{slug}/ui/{path}',
    reason:
      "Serves a template's own UI module and assets to the browser (#425). An agent reads the `ui` " +
      'declaration from get_model and has no page to mount a module in.',
  },
  {
    operation: 'GET /api/v1/models/{slug}/versions/{commit}/ui/{path}',
    reason: 'The same UI files at a pinned revision, for the browser (#425).',
  },
  {
    operation: 'PUT /api/v1/settings',
    reason:
      'The one write path for every stored setting (#322): the Bambuddy URL and API key and the Google Fonts ' +
      'key, which are entered in the Settings UI only and never pass through an agent (spec §8.6, credential ' +
      'leakage), and the runtime settings (render concurrency and timeouts, queue caps, upload and library ' +
      'limits, retention, log level), which decide how the server runs for everyone and are an operator ' +
      'decision made in Settings, and the asset allowlist fetch_asset is held to (#844), which an agent must ' +
      'not widen for itself. Reading them all, with their sources, is get_settings.',
  },
  {
    operation: 'DELETE /api/v1/settings/remembered',
    reason:
      "Forget all drops every model's and printer's remembered choices at once; a bulk reset of shared " +
      'preferences, confirmed in the Settings UI only. An agent forgets one entry through its own tool.',
  },
  {
    operation: 'DELETE /api/v1/settings/remembered/projects/{project_id}',
    reason:
      "Forgets the printer and nozzle a project last printed on (#599), Settings housekeeping: the next print " +
      'into that project records it again, so an agent has nothing to gain by clearing it. Reading it is ' +
      'get_remembered_choices.',
  },
  {
    operation: 'POST /api/v1/settings/register-sidebar',
    reason: "One-time setup that edits Bambuddy's own sidebar; an operator action in Settings, not an agent task.",
  },
  {
    operation: 'PUT /api/v1/outputs/{output_id}/thumbnail',
    reason:
      "Uploads the 3D viewer's canvas capture. An agent has no canvas; a browser_* tool driving the open tab " +
      'can (#254, #266).',
  },
  {
    operation: 'GET /api/v1/outputs/{output_id}/files/{name}',
    reason: 'binary download; the agent reads `bom` and `files` from GET /outputs/{id}',
  },
  {
    operation: 'POST /api/v1/outputs/{output_id}/backfill',
    reason:
      'Re-renders an output saved before Arrange so Arrange can use it (#902); the History and Print ' +
      'dialogs ask the user first, so the arrange tool names such outputs and leaves the re-render to the user.',
  },
  {
    operation: 'POST /api/v1/models/{slug}/inputs/migrate',
    reason:
      'the host migrates inputs as it opens a preset or output; the agent reads inputs already migrated',
  },
  // #274: template media. An agent reads `media` (ids, captions, order) from the model record.
  ...(
    [
      'GET /api/v1/models/{slug}/media/{item_id}',
      'GET /api/v1/models/{slug}/media/{item_id}/poster',
      'GET /api/v1/models/{slug}/media/{item_id}/thumbnail',
    ] as const
  ).map((operation) => ({
    operation,
    reason:
      'Serves an image or video file to the browser; an agent gets the item list from the model record ' +
      'and has no use for the bytes.',
  })),
  {
    operation: 'POST /api/v1/models/{slug}/media',
    reason: 'A multipart upload of an image or video from the user; an agent has no file to send.',
  },
  ...(
    [
      'PATCH /api/v1/models/{slug}/media/{item_id}',
      'PUT /api/v1/models/{slug}/media/order',
      'PUT /api/v1/models/{slug}/media/cover',
      'DELETE /api/v1/models/{slug}/media/{item_id}',
    ] as const
  ).map((operation) => ({
    operation,
    reason:
      "Captioning, reordering, choosing the cover of and removing a template's media happen on the edit page " +
      '(#279, #722); the plan adds no agent tools in the gallery epic (#273, decision 7).',
  })),
  // #185: the source editor's go-to-definition opens the file a definition is in. The
  // model-directory reader is also get_source_file's route (#252), so only the library one stays here.
  // #951: a model README's relative images, for the browser's Markdown view.
  {
    operation: 'GET /api/v1/models/{slug}/images/{path}',
    reason:
      "Serves the image files beside a model to the browser, so a README's relative `![](thumbnail.png)` " +
      'shows (#951). The bytes are only useful to an <img>; an agent reads the README itself through ' +
      'get_source_file and sees the image path in it.',
  },
  {
    operation: 'GET /api/v1/models/{slug}/libraries/{name}/files/{path}',
    reason:
      "Serves the file a go-to-definition lands in to the source editor's read-only view (#185). The " +
      'editor asks for the path openscad-lsp named; an agent has no definition to follow and reads a ' +
      "model's own files through get_source and get_source_file.",
  },
  {
    operation: 'GET /api/v1/analyzers',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'POST /api/v1/analyzers/run',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'POST /api/v1/analyzers/fixes/preview',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'POST /api/v1/analyzers/fixes/apply',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'GET /api/v1/analyzers/decisions',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'POST /api/v1/analyzers/decisions',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'DELETE /api/v1/analyzers/decisions/{decision_id}',
    reason: ANALYZERS_LATER,
  },
  ...(['POST /api/v1/print/outputs/{output_id}/check', 'POST /api/v1/print/library/{file_id}/check'] as const).map(
    (operation) => ({
      operation,
      reason:
        "The print dialog's check before Print (#755). An agent's print_output run makes the same refusals " +
        'itself, as a 422 with the same words, before anything is uploaded, so a separate check adds nothing.',
    }),
  ),
  ...(
    [
      'GET /api/v1/print/library/{file_id}/thumbnail',
      'GET /api/v1/print/library/{file_id}/plates/{index}/thumbnail',
    ] as const
  ).map((operation) => ({
    operation,
    reason: "Serves Bambuddy's image of a library file to the browser; an agent has no use for the bytes (#313).",
  })),
  {
    operation: 'PUT /api/v1/print/printers/{printer_id}/rack-algorithm',
    reason:
      "Remembers how the print dialog ranks a printer's nozzle rack (#836). It lands UI-first, " +
      'like the rest of the rack picker; an agent prints through print_output, which takes the ' +
      'remembered algorithm, and a tool for changing it is a follow-up.',
  },
  ...(
    [
      'POST /api/v1/print/outputs/{output_id}/preview-slice',
      'POST /api/v1/print/library/{file_id}/preview-slice',
      'GET /api/v1/print/outputs/{output_id}/preview-slices/{job_id}',
      'GET /api/v1/print/library/{file_id}/preview-slices/{job_id}',
    ] as const
  ).map((operation) => ({
    operation,
    reason:
      "The print dialog's background reslice (#2169), which shows grams, time and the nozzle plan beside " +
      'the choices while a person changes them. An agent prints through print_output, whose run slices ' +
      'anyway, and reads the plan from the print check.',
  })),
  {
    operation: 'POST /api/v1/print/printers/{printer_id}/trays/{ams_id}/{tray_id}/spool',
    reason:
      "Records in Bambuddy which spool is in an untagged tray (#2164), sent only on a person's own answer " +
      "to the print dialog's question; an agent cannot see which spool is physically in the tray.",
  },
  {
    operation: 'GET /api/v1/print/printers/{printer_id}/rack-usage',
    reason:
      "Settings' Hotend usage table (#1298). An agent already sees each rack position's prints, " +
      "print time and open picks in the print check's rack options, for the hotends that job could use. " +
      "It also answers each hotend's serial (#2170), which is for the Settings page and stays out of a model's context.",
  },
]

/**
 * Tools whose backend routes are in open PRs, so they are not in
 * backend/openapi.json on main yet. Listed so the gap is visible. An entry
 * must not outlive its route landing: test/coverage.test.ts fails as soon as
 * one of these operations appears in the spec, until it gets its tool (or a
 * NOT_A_TOOL entry) and leaves this list.
 */
export const PENDING_ROUTES: readonly { operation: string; pr: number; tool: string; reason: string }[] = [
  // Empty: #320's and #324's routes have merged and have their tools.
]

/**
 * The agent service's own routes that an agent needs a tool for (spec 2026-10-01 §6.6,
 * plan 5b Task 10): each names its tool, or gives the reason it has none.
 * test/coverage.test.ts checks every tool exists and every route is served. Only the
 * pending-input routes are listed so far; the agent's other routes predate the check.
 */
export const AGENT_ROUTES: readonly { route: string; tool?: string; reason?: string }[] = [
  { route: 'GET /api/v1/ai/pending-input', tool: 'pending_input_list' },
  { route: 'GET /api/v1/ai/sessions/{id}/pending-input', tool: 'sessions_pending_input' },
  {
    route: 'POST /api/v1/ai/pending-input/{request_id}',
    reason:
      'Browser-only for `answer` kinds (only the user answers a question); an `approval` is decided through ' +
      'sessions_approve / sessions_deny, which take every approval id, durable ones included.',
  },
  {
    route: 'GET /api/v1/ai/settings/session-mode',
    reason:
      "Settings → Assistant's default session mode (plan 5d), for the user. An agent picks its own " +
      "session's mode with sessions_start's `mode`, and sessions_start says when the default ran classic.",
  },
  {
    route: 'PUT /api/v1/ai/settings/session-mode',
    reason: 'Changes how every new session runs: an operator decision made in Settings, never by an agent.',
  },
]
