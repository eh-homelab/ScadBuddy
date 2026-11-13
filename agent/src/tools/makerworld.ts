import { z } from 'zod'
import { ok } from './call.js'
import { command } from './command.js'
import { defineTool, json, type Tool } from './registry.js'

// MakerWorld through Bambuddy's own MakerWorld routes, which ScadBuddy's API calls with its
// own Bambuddy key (#6119, #1254; backend/scadbuddy/api/makerworld.py). This agent never
// reaches Bambuddy and never holds its key (#5295): the agent worker has no route to it.

const BAMBUDDY_DATA = 'MakerWorld data (design titles, descriptions, tags, profile names) that anyone who publishes on MakerWorld can write, read through Bambuddy'

const FOLDERS =
  "Two kinds of folder id: `folder_id` is a folder of the library list_library browses (on library_backend native, ScadBuddy's own: ids from 1000000000 up). " +
  "`bambuddy_folder_id` is a Bambuddy library folder, such as a print project's `folder_id` from list_print_projects or create_print_project: those are Bambuddy's, " +
  'never a native folder, and list_library refuses them (404) on native.'

export const makerworldTools: Tool[] = [
  defineTool({
    name: 'get_makerworld_status',
    label: 'Check MakerWorld import',
    description:
      'Whether Bambuddy can download from MakerWorld for ScadBuddy: `can_download` false means no Bambu Cloud sign-in in Bambuddy or, ' +
      "while Bambuddy's auth is on, an API key without cloud access; `sign_in_expired` means sign in again in Bambuddy. Check it before import_makerworld_model.",
    input: z.object({}),
    risk: 'read',
    bambuddyScope: ['Read Status'],
    routes: ['GET /api/v1/makerworld/status'],
    handler: async (_, { backend }) => json(await ok(backend.GET('/api/v1/makerworld/status'), 'read the MakerWorld status')),
  }),

  defineTool({
    name: 'resolve_makerworld_url',
    label: 'Look up a MakerWorld link',
    description:
      "A MakerWorld model link's design (`model_id`, MakerWorld's `design` record) and its print profiles (`instances`; each one's `profileId` is " +
      "import_makerworld_model's `profile_id`; `profile_id` is the one the link's #profileId- names, if any). Downloads nothing. " +
      '`already_imported_library_ids` are the library files an earlier import of this model left, in the library `library` names (on native, ' +
      "ScadBuddy's own files of the same bytes): the ids order_item_add and print_output take. `already_imported_bambuddy_ids` are Bambuddy's copies.",
    input: z.object({
      url: z.string().trim().min(1).max(2048).describe('A MakerWorld model link, e.g. https://makerworld.com/en/models/123#profileId-456'),
    }),
    risk: 'read',
    source: BAMBUDDY_DATA,
    bambuddyScope: ['Read Status', 'Manage Library'],
    routes: ['POST /api/v1/makerworld/resolve'],
    handler: async ({ url }, { backend }) =>
      json(await ok(backend.POST('/api/v1/makerworld/resolve', { body: { url } }), 'resolve the MakerWorld link')),
  }),

  defineTool({
    name: 'import_makerworld_model',
    label: 'Import a MakerWorld model',
    description:
      "Import a MakerWorld print profile into the library: Bambuddy downloads its 3MF (with its Bambu Cloud sign-in) into Bambuddy's library, " +
      "and on library_backend native ScadBuddy copies it into its own library. Answers `library_file_id`, the file order_item_add (`library_file_id`) " +
      "and print_output take, with its `folder_id` in that library (`library` says which), and Bambuddy's copy as `bambuddy_library_file_id`. " +
      'A repeat downloads nothing (`was_existing`; `bambuddy_was_existing` for Bambuddy\'s side, `copied` when ScadBuddy\'s library stored a new file). ' +
      'An import of a model and profile already imported, into another folder, makes no second file: it answers the earlier file in its earlier folder ' +
      "(Bambuddy reuses its import of the profile, and ScadBuddy's library reuses a file of the same bytes); move_library_file moves it. " +
      'The download can take minutes: an answer with `status: running` is still importing; follow it with ' +
      "get_operation until it ends, and do not import again meanwhile. Get `model_id` and `profile_id` from resolve_makerworld_url. " +
      FOLDERS +
      ' `folder_id` is refused (422) on library_backend bambuddy. A 409 for the API key scope can also be MakerWorld refusing the download (Bambuddy\'s reason is quoted).',
    input: z.object({
      model_id: z.number().int().positive().describe("The design's `model_id` from resolve_makerworld_url"),
      profile_id: z.number().int().positive().optional().describe("An instance's `profileId`; Bambuddy takes the design's first without one"),
      folder_id: z
        .number()
        .int()
        .positive()
        .optional()
        .describe("A folder of ScadBuddy's own library (list_library on native) for the file; a top-level `MakerWorld` folder without one"),
      bambuddy_folder_id: z
        .number()
        .int()
        .positive()
        .optional()
        .describe("A Bambuddy library folder for Bambuddy's copy, such as a print project's `folder_id`; Bambuddy's `MakerWorld` folder without one"),
    }),
    risk: 'outward',
    source: BAMBUDDY_DATA,
    bambuddyScope: ['Manage Library'],
    routes: ['POST /api/v1/makerworld/import'],
    summarize: ({ model_id, profile_id, folder_id, bambuddy_folder_id }) =>
      `Import MakerWorld model ${model_id}${profile_id === undefined ? '' : ` (profile ${profile_id})`} into the library` +
      (folder_id === undefined ? '' : `, folder ${folder_id}`) +
      (bambuddy_folder_id === undefined ? '' : `, Bambuddy folder ${bambuddy_folder_id}`),
    handler: async ({ model_id, profile_id, folder_id, bambuddy_folder_id }, ctx) =>
      json(
        await command(ctx, `import MakerWorld model ${model_id}`, (headers) =>
          ctx.backend.POST('/api/v1/makerworld/import', {
            body: { model_id, profile_id: profile_id ?? null, folder_id: folder_id ?? null, bambuddy_folder_id: bambuddy_folder_id ?? null },
            headers,
          }),
        ),
      ),
  }),
]
