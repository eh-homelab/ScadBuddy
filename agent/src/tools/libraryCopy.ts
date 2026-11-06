import { z } from 'zod'
import { ok } from './call.js'
import { answered } from './command.js'
import { defineTool, json, type Tool } from './registry.js'

// Copies between Bambuddy's library and ScadBuddy's own (#5355). Routes:
// backend/scadbuddy/api/library_copy.py. The start answers 202 with the job's id (not an
// operation), so it is not `command()`: the id names the request, a second start of the
// same request joins the running copy, and `answered`'s re-send after a lost answer is
// harmless. A flow calls `copy_library_files` under a `tool` grant and follows it with
// `get_library_copy`.

const direction = z
  .enum(['bb_to_sb', 'sb_to_bb'])
  .describe("`bb_to_sb`: Bambuddy's library into ScadBuddy's own; `sb_to_bb`: the other way")

const id = z.number().int().positive()

export const libraryCopyTools: Tool[] = [
  defineTool({
    name: 'copy_library_files',
    label: 'Copy files between the two libraries',
    description:
      "Copy library files and whole folders (their subfolders too) from one library into the other, as a background job on the server: `bb_to_sb` reads Bambuddy's library (ids from list_library with `source: 'bambuddy'`) " +
      "into ScadBuddy's own, `sb_to_bb` the other way (ids from list_library). The copies go under `target_folder_id` in the target library (its top level without it); " +
      "each selected folder's path is kept and made there. A file whose SHA-256 the target already holds is skipped, so a repeat copies nothing twice; nothing at the target is " +
      'deleted or overwritten, and the source is only read. Answers the job\'s `copy_id` at once: follow it with get_library_copy. ' +
      'Refused (409) unless library_backend is `native` and Bambuddy is configured; 422 for an id in the wrong library.',
    input: z.object({
      direction,
      file_ids: z.array(id).max(5000).optional().describe('Files to copy, by their id in the source library'),
      folder_ids: z.array(id).max(500).optional().describe('Folders to copy whole, by their id in the source library'),
      target_folder_id: id.optional().describe('The folder in the target library the copies go under'),
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['POST /api/v1/print/library-copies'],
    summarize: ({ direction, file_ids = [], folder_ids = [] }) =>
      `Copy ${[file_ids.length && `${file_ids.length} file${file_ids.length === 1 ? '' : 's'}`, folder_ids.length && `${folder_ids.length} folder${folder_ids.length === 1 ? '' : 's'}`].filter(Boolean).join(' and ') || 'nothing'} ${direction === 'bb_to_sb' ? "from Bambuddy's library into ScadBuddy's" : "from ScadBuddy's library into Bambuddy's"}`,
    handler: async ({ direction, file_ids, folder_ids, target_folder_id }, ctx) =>
      json(
        await ok(
          answered(
            ctx,
            () =>
              ctx.backend.POST('/api/v1/print/library-copies', {
                body: { direction, file_ids: file_ids ?? [], folder_ids: folder_ids ?? [], target_folder_id: target_folder_id ?? null },
              }),
            'start a library copy',
          ),
          'start a library copy',
        ),
      ),
  }),

  defineTool({
    name: 'get_library_copy',
    label: 'Check a library copy',
    description:
      "A library copy's progress (copy_library_files): its counts (`total`, `pending`, `copied`, `skipped_same_hash`, `failed`), whether it still runs (`running`), " +
      'and up to 500 of its files with how each ended (`copied` with its `target_id`, `skipped_same_hash`, `failed` with its `reason`, or `pending`), failed ones first. ' +
      'Each file has `attempts`, the tries begun on it; a `pending` file with a `reason` is being retried, and the reason says why its last try did not end. ' +
      'Refused (404) for an id no copy has.',
    input: z.object({ copy_id: z.string().min(1).max(100) }),
    risk: 'read',
    routes: ['GET /api/v1/print/library-copies/{copy_id}'],
    handler: async ({ copy_id }, { backend }) =>
      json(await ok(backend.GET('/api/v1/print/library-copies/{copy_id}', { params: { path: { copy_id } } }), 'read a library copy')),
  }),
]
