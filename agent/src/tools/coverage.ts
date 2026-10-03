import type { Operation } from './registry.js'

// The openapi coverage allowlist (spec §5.1: "A CI check fails when an
// operation in backend/openapi.json has neither a tool nor an explicit
// allowlist entry"). test/coverage.test.ts enforces it.

const ANALYZERS_LATER =
  'The print analyzers (#284) land API-first; their agent tools are a follow-up (#368 wraps them). ' +
  'Recording a decision or applying a fix writes only to ScadBuddy (write tier) and sends nothing; ' +
  'a send that consumes accepted diffs must go through the outward approval flow (AI spec §8.2).'

const LIBRARY_PRINT_LATER =
  'Printing a file already in Bambuddy\'s library (#313) lands UI-first; an agent tool for it is a ' +
  'follow-up (spec 2026-09-28 §6). A run slices and queues a real print, so the tool must go through ' +
  'the outward approval flow (AI spec §8.2) when it is written.'

/** Backend operations deliberately left without a tool, each with the reason. */
export const NOT_A_TOOL: readonly { operation: Operation; reason: string }[] = [
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
    operation: 'POST /api/v1/outputs/arrange',
    reason:
      "Arrange needs objects, a printer and spools chosen in the History or Print dialog; the agent's print " +
      'tools do not pick spools yet.',
  },
  {
    operation: 'POST /api/v1/outputs/{output_id}/backfill',
    reason:
      'Re-renders an output saved before Arrange so Arrange can use it (#902); the History and Print ' +
      'dialogs ask the user first. The agent has no Arrange tool (above), so it has nothing to backfill for.',
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
  // #169: the library upgrade flow's building blocks land API-first; its design is pending.
  ...(['GET /api/v1/libraries/{name}/users', 'POST /api/v1/models/{slug}/libraries/{name}/check'] as const).map(
    (operation) => ({
      operation,
      reason:
        'Building blocks of the library upgrade flow (#169), whose product design is not decided; its tools ' +
        "come with it. The check clones from the model's pinned URL, so its tier follows repin_library's.",
    }),
  ),
  ...(
    [
      'GET /api/v1/print/library',
      'GET /api/v1/print/library/{file_id}/plates',
      'GET /api/v1/print/library/{file_id}/choices',
      'PUT /api/v1/print/library/{file_id}/choices',
      'GET /api/v1/print/library/{file_id}/filaments',
      'POST /api/v1/print/library/{file_id}/run',
    ] as const
  ).map((operation) => ({ operation, reason: LIBRARY_PRINT_LATER })),
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
