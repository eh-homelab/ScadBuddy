import { randomUUID } from 'node:crypto'
import { z } from 'zod'
import { command, isRunning } from './command.js'
import { binary } from './binary.js'
import { ok } from './call.js'
import { slug } from './common.js'
import { blob, defineTool, image, json, ToolError, type Tool } from './registry.js'

// Print history (issue #308): ScadBuddy's own prints, each a Bambuddy archive linked
// to the output that printed it. Routes: backend/scadbuddy/api/print_history.py.

const archiveId = z.number().int().min(1).describe("A Bambuddy print archive's id, as list_prints returns it")
/** The route's default page (`api/print_imports.py` `list_auto_imports`). */
const AUTO_IMPORTS_PAGE = 50
const day = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'must be a day, YYYY-MM-DD')

export const printHistoryTools: Tool[] = [
  defineTool({
    name: 'list_prints',
    label: 'List prints',
    description:
      "ScadBuddy's print history, newest first: each print's status, printer, times, filament, cover image and the " +
      'parameters that differ from the template defaults; a Bambuddy library file\'s print names the file ' +
      '(`library_file_name`); a print imported onto a model (import_print, or the print watcher: list_auto_imports) ' +
      'names that model (`slug`). A LAN-only printer\'s print the watcher recorded has no archive and is not here: ' +
      'list_lan_auto_imports. Filter by template (`slug`) or by one library file (`library_file_id`, not both), ' +
      'Bambuddy `status` (completed, failed, printing, … or deleted_in_bambuddy), `printer_id`, start day ' +
      '(`from`/`to`, inclusive) and text `q` (output, library file or print name, parameter values). Pass ' +
      '`next_cursor` back as `cursor` for the next page: with a narrow filter a page can be short or empty and ' +
      'still have one, so keep going until it is null.',
    input: z.object({
      slug: slug.optional(),
      library_file_id: z.number().int().min(1).optional(),
      status: z.string().min(1).max(64).optional(),
      printer_id: z.number().int().optional(),
      from: day.optional(),
      to: day.optional(),
      q: z.string().min(1).max(200).optional(),
      limit: z.number().int().min(1).max(100).optional(),
      cursor: z.string().regex(/^[1-9][0-9]{0,17}$/, 'must be a next_cursor from list_prints').optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/prints'],
    handler: async (query, { backend }) =>
      json(await ok(backend.GET('/api/v1/prints', { params: { query } }), 'list prints')),
  }),

  defineTool({
    name: 'get_print',
    label: "Read a print's details",
    description:
      'One print: the output it came from (template, revision, exact parameters; for an imported print, `imported`: ' +
      'the model and the source its 3MF came from), its files, photos, timelapse, ' +
      'outcome (status, failure reason, times, filament, cost), runs, the slicing (printer model, bed, layers, ' +
      'nozzle, temperatures, filament per slot), the slicer settings per filament slot the ScadBuddy run set ' +
      '(`part_settings`, #5627: only the request\'s; pass them to print_output again to print with them. A ' +
      'template\'s own part_print_settings, #5626, are not listed and apply again at the reprint) and Bambuddy\'s ' +
      'record of it, ' +
      'with every other archive field. ' +
      '`printer_media: true` also lists the ' +
      "recordings on the printer, which asks the printer.",
    input: z.object({ archive_id: archiveId, printer_media: z.boolean().optional() }),
    risk: 'read',
    routes: ['GET /api/v1/prints/{archive_id}'],
    handler: async ({ archive_id, printer_media }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/prints/{archive_id}', {
            params: { path: { archive_id }, query: printer_media === undefined ? {} : { printer_media } },
          }),
          `get print ${archive_id}`,
        ),
      ),
  }),

  // ── outward (#311): the print detail page's "Print again" and "Pull timelapse" ──
  defineTool({
    name: 'print_again',
    label: 'Print an earlier print again',
    description:
      "Queue a print again on Bambuddy: the same sliced file, on the printer and plate it printed on, with Bambuddy's " +
      'default options. Refused when the archive was deleted in Bambuddy or has no known printer.',
    input: z.object({ archive_id: archiveId }),
    risk: 'outward',
    bambuddyScope: ['Read Status', 'Manage Queue'],
    routes: ['POST /api/v1/prints/{archive_id}/reprint'],
    summarize: ({ archive_id }) => `Queue print ${archive_id} again on its printer`,
    handler: async ({ archive_id }, ctx) =>
      json(
        await command(ctx, `print ${archive_id} again`, (headers) =>
          ctx.backend.POST('/api/v1/prints/{archive_id}/reprint', { params: { path: { archive_id } }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'pull_print_timelapse',
    label: "Pull a print's timelapse",
    description:
      "Download a timelapse still on the printer and attach it to the print. `filename` is a `remote_files[].name` " +
      'of kind `timelapse` from get_print with `printer_media: true`. It is fetched over FTP from the printer. ' +
      'Refused when the archive was deleted in Bambuddy.',
    input: z.object({
      archive_id: archiveId,
      filename: z.string().min(1).max(255).regex(/^[^/\\]+$/, 'must be a bare file name from printer_media'),
    }),
    risk: 'outward',
    bambuddyScope: ['Read Status', 'Manage Archives'],
    routes: ['POST /api/v1/prints/{archive_id}/timelapse/pull'],
    summarize: ({ archive_id, filename }) => `Pull timelapse ${filename} from the printer onto print ${archive_id}`,
    handler: async ({ archive_id, filename }, ctx) => {
      const pulled = await command(ctx, `pull timelapse ${filename} onto print ${archive_id}`, (headers) =>
        ctx.backend.POST('/api/v1/prints/{archive_id}/timelapse/pull', {
          params: { path: { archive_id } },
          body: { filename },
          headers,
        }),
      )
      return json(isRunning(pulled) ? pulled : { attached: filename })
    },
  }),

  // ── #4698: import a print ScadBuddy did not start (backend/scadbuddy/api/print_imports.py) ──
  defineTool({
    name: 'get_print_sources',
    label: "Find a print's 3MF to import it",
    description:
      'For a Bambuddy archive ScadBuddy did not start (list_print_archives rows with `scadbuddy_print: false`; one ' +
      'still printing too): every place its 3MF can be, each with its name, size, date and whether it is sliced. ' +
      '`archive` is the sliced file Bambuddy keeps, `archive_source` the slicer project attached to the archive, ' +
      '`library` a Bambuddy library file with the same name (`matched_by: hash` when its bytes are the printed ' +
      'file), `printer` the printer\'s storage. A source that cannot be used says why (`reason`). `printer: true` ' +
      'also reads the printer over FTP, which reaches the printer, so that call waits for a person\'s approval. ' +
      'Also `linked` (what the archive is a print of already) and `suggested_models` from the job\'s name. Show ' +
      'the sources to the user and let them choose; then import_print.',
    input: z.object({
      archive_id: archiveId.describe("A Bambuddy archive's id, as list_print_archives returns it"),
      printer: z.boolean().optional(),
    }),
    risk: 'read',
    // Control Printer only for `printer: true`, which reads the printer's storage over FTP.
    bambuddyScope: ['Read Status', 'Control Printer'],
    routes: ['GET /api/v1/print-imports/{archive_id}/sources'],
    summarize: ({ archive_id, printer }) =>
      printer ? `Read the printer's storage for the 3MF of print ${archive_id}` : `List the sources of print ${archive_id}`,
    handler: async ({ archive_id, printer }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/print-imports/{archive_id}/sources', {
            params: { path: { archive_id }, query: printer === undefined ? {} : { printer } },
          }),
          `list the sources of print ${archive_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'list_auto_imports',
    label: 'List the prints the print watcher imported',
    description:
      'The prints the print watcher (#4699) imported, ScadBuddy not having started them, newest archive first: each ' +
      'it linked to a model (`state: linked`, with `matched_by`: an earlier import of the same file, or the job\'s ' +
      'name), and each it put in the Unsorted imports inbox (`state: inbox`, with `reason` and the models the job\'s ' +
      'name suggests), and each a person or the assistant dismissed from it (`state: dismissed`). `state: inbox` is ' +
      'the Unsorted imports list. To link an inbox print, show the user the suggestions, then import_print; to ' +
      'take one out of the inbox, dismiss_auto_import. The prints on printers with no Bambuddy link have no ' +
      'archive and are list_lan_auto_imports\'s. Answers `items`, ' +
      'and `next_cursor` when the page was full: pass it back as `cursor` for the next page.',
    input: z.object({
      state: z.enum(['linked', 'inbox', 'dismissed']).optional(),
      limit: z.number().int().min(1).max(200).optional(),
      cursor: z.string().regex(/^[1-9][0-9]{0,17}$/, 'must be a next_cursor from list_auto_imports').optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/print-imports/auto'],
    summarize: ({ state }) => (state === 'inbox' ? 'List the Unsorted imports inbox' : 'List the prints the print watcher imported'),
    handler: async ({ state, limit, cursor }, { backend }) => {
      const size = limit ?? AUTO_IMPORTS_PAGE
      const items = await ok(
        backend.GET('/api/v1/print-imports/auto', {
          params: { query: { ...(state === undefined ? {} : { state }), limit: size, ...(cursor === undefined ? {} : { before: Number(cursor) }) } },
        }),
        'list the prints the print watcher imported',
      )
      // The route pages by the last archive read (`before`); a full page may have more after it.
      const last = items.at(-1)
      return json({ items, ...(items.length === size && last ? { next_cursor: String(last.archive_id) } : {}) })
    },
  }),

  defineTool({
    name: 'dismiss_auto_import',
    label: 'Dismiss a print from Unsorted imports',
    description:
      'Take a print out of the Unsorted imports inbox (list_auto_imports `state: inbox`): its row becomes ' +
      '`dismissed`, and the print watcher never takes the archive again. It reaches no printer and no Bambuddy; ' +
      'import_print can still link the print later. Dismiss only a print the user said is not worth keeping. A ' +
      'print dismissed before gives the same answer; a print linked to a model, an output or a library file is ' +
      'refused (409), and an archive the watcher never took is 404.',
    input: z.object({ archive_id: archiveId.describe("The inbox print's archive id, as list_auto_imports returns it") }),
    risk: 'write',
    routes: ['POST /api/v1/print-imports/auto/{archive_id}/dismiss'],
    summarize: ({ archive_id }) => `Dismiss print ${archive_id} from Unsorted imports`,
    handler: async ({ archive_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/print-imports/auto/{archive_id}/dismiss', { params: { path: { archive_id } } }),
          `dismiss print ${archive_id} from Unsorted imports`,
        ),
      ),
  }),

  // ── #6041: the prints the watcher recorded on LAN-only printers (no Bambuddy archive) ──
  defineTool({
    name: 'list_lan_auto_imports',
    label: "List the LAN-only printers' watched prints",
    description:
      'The prints the print watcher (#4699) recorded on printers with no Bambuddy link, newest record first. Such a ' +
      'print has no Bambuddy archive: each record has an `id` of its own, apart from the archive ids of ' +
      'list_auto_imports, and names its printer (`printer_id`, `printer_name`) and job (`subtask_name`). `state` ' +
      'is `linked` (with `slug` and `matched_by`: an earlier import of the same file, the job\'s name, or `import`, ' +
      'a link), `inbox` (Unsorted imports, with `reason` and the models the job\'s name suggests) or `dismissed`. ' +
      '`slug` keeps only the records linked to that model; such a print is not in list_prints. To link an inbox ' +
      'record, show the user the suggestions, then link_lan_auto_import (not import_print: there is no archive); ' +
      'to take one out of the inbox, dismiss_lan_auto_import. Answers `items`, and `next_cursor` when the page ' +
      'was full: pass it back as `cursor` for the next page.',
    input: z.object({
      state: z.enum(['linked', 'inbox', 'dismissed']).optional(),
      slug: slug.optional(),
      limit: z.number().int().min(1).max(200).optional(),
      cursor: z.string().regex(/^[1-9][0-9]{0,17}$/, 'must be a next_cursor from list_lan_auto_imports').optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/print-imports/auto/lan'],
    summarize: ({ state }) =>
      state === 'inbox'
        ? "List the LAN-only printers' Unsorted imports"
        : 'List the prints the print watcher recorded on LAN-only printers',
    handler: async ({ state, slug: model, limit, cursor }, { backend }) => {
      const size = limit ?? AUTO_IMPORTS_PAGE
      const items = await ok(
        backend.GET('/api/v1/print-imports/auto/lan', {
          params: {
            query: {
              ...(state === undefined ? {} : { state }),
              ...(model === undefined ? {} : { slug: model }),
              limit: size,
              ...(cursor === undefined ? {} : { before: Number(cursor) }),
            },
          },
        }),
        'list the prints the print watcher recorded on LAN-only printers',
      )
      // The route pages by the last record read (`before`); a full page may have more after it.
      const last = items.at(-1)
      return json({ items, ...(items.length === size && last?.id ? { next_cursor: String(last.id) } : {}) })
    },
  }),

  defineTool({
    name: 'dismiss_lan_auto_import',
    label: "Dismiss a LAN-only printer's unsorted print",
    description:
      "Take a LAN-only printer's print out of the Unsorted imports inbox (list_lan_auto_imports `state: inbox`): " +
      'its record becomes `dismissed`. It reaches no printer and no Bambuddy; link_lan_auto_import can still link ' +
      'it later. Dismiss only a print the user said is not worth keeping. A print dismissed before gives the same ' +
      'answer; a print linked to a model that is there is refused (409), and no such record is 404.',
    input: z.object({ id: z.number().int().min(1).describe("The record's id, as list_lan_auto_imports returns it") }),
    risk: 'write',
    routes: ['POST /api/v1/print-imports/auto/lan/{record_id}/dismiss'],
    summarize: ({ id }) => `Dismiss LAN print ${id} from Unsorted imports`,
    handler: async ({ id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/print-imports/auto/lan/{record_id}/dismiss', { params: { path: { record_id: id } } }),
          `dismiss LAN print ${id} from Unsorted imports`,
        ),
      ),
  }),

  defineTool({
    name: 'link_lan_auto_import',
    label: "Link a LAN-only printer's print to a model",
    description:
      "Link a LAN-only printer's print (list_lan_auto_imports) to the model `slug`: its record becomes `linked`, " +
      'with `matched_by: import`. Its one source is the printer file the print watcher kept in ScadBuddy\'s own ' +
      'library; nothing is read or copied, and no printer or Bambuddy is reached. An inbox or a dismissed print ' +
      'can be linked; one linked to `slug` already gives the same answer. Refused (409) when the watcher kept no ' +
      '3MF for it, or when it is linked to another model that is there; 404 for no such record or model. The ' +
      "linked print is not on the model's Prints tab (list_prints): it is in Activity and list_lan_auto_imports " +
      '`slug`. Link only to the model the user chose.',
    input: z.object({
      id: z.number().int().min(1).describe("The record's id, as list_lan_auto_imports returns it"),
      slug,
    }),
    risk: 'write',
    routes: ['POST /api/v1/print-imports/auto/lan/{record_id}/link'],
    summarize: ({ id, slug: model }) => `Link LAN print ${id} to ${model}`,
    handler: async ({ id, slug: model }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/print-imports/auto/lan/{record_id}/link', {
            params: { path: { record_id: id } },
            body: { slug: model },
          }),
          `link LAN print ${id} to ${model}`,
        ),
      ),
  }),

  defineTool({
    name: 'import_print',
    label: 'Import a print onto a model',
    description:
      'Make a Bambuddy archive ScadBuddy did not start a ScadBuddy print of a model, so get_print, its photos, ' +
      'timelapse, attachments, workspace_save print_image/print_timelapse and Print again work, and it shows on ' +
      "the model's Prints tab. `source` is one get_print_sources lists as available, the one the user chose: " +
      '`{kind: archive}`, `{kind: archive_source}`, `{kind: library, library_file_id}` or `{kind: printer, path}`. ' +
      'Link it to a model that exists (`slug`), or make a new mesh model from the 3MF (`new_model: {name}`; a ' +
      'sliced file has no meshes, so choose an unsliced one). A `printer` source reads the file over FTP and keeps ' +
      "it in Bambuddy's library inbox, so that call waits for a person's approval. Importing the same archive onto " +
      'the same model again answers the first import; another model is refused, naming the owner.',
    input: z.object({
      archive_id: archiveId.describe("A Bambuddy archive's id, as list_print_archives returns it"),
      source: z.object({
        kind: z.enum(['archive', 'archive_source', 'library', 'printer']),
        library_file_id: z.number().int().min(1).optional(),
        path: z.string().min(1).max(1024).optional(),
      }),
      slug: slug.optional(),
      new_model: z
        .object({ name: z.string().min(1).max(200), description: z.string().max(10_000).default('') })
        .optional(),
    }),
    risk: 'write',
    // Only a `printer` source needs the last two: read over FTP, kept in the library inbox.
    bambuddyScope: ['Read Status', 'Control Printer', 'Manage Library'],
    routes: ['POST /api/v1/print-imports/{archive_id}'],
    summarize: ({ archive_id, source, slug: model, new_model }) =>
      `Import print ${archive_id} from its ${source.kind} file onto ${model ? `model ${model}` : `a new model "${new_model?.name ?? ''}"`}`,
    handler: async ({ archive_id, ...body }, ctx) => {
      if ((body.slug === undefined) === (body.new_model === undefined)) throw new ToolError('give slug or new_model, not both')
      // One key for both sends below, so the second finds the model the first made.
      const keyed = { ...ctx, callKey: ctx.callKey ?? randomUUID() }
      const send = () =>
        command(keyed, `import print ${archive_id}`, (headers) =>
          ctx.backend.POST('/api/v1/print-imports/{archive_id}', { params: { path: { archive_id } }, body, headers }),
        )
      const first = await send()
      if (isRunning(first)) {
        return json({
          ...first,
          next:
            `The new model for print ${archive_id} is still being made. Follow it with get_operation; once it is done, ` +
            `call import_print with the new model's slug.`,
        })
      }
      // A route that answered at once answers the import. A slow new model answers 202 with the
      // `mesh_model_create` operation, which `command` followed to its end: its result is the model
      // (no `archive_id`). The same request with the same key then reuses that operation's model
      // and links the print (test/printImports.test.ts, "links a new model once its operation is done").
      if ('archive_id' in (first as object)) return json(first)
      const linked = await send()
      if (isRunning(linked) || !('archive_id' in (linked as object))) {
        throw new ToolError(`the new model for print ${archive_id} was made, but the print was not linked: call import_print with its slug`)
      }
      return json(linked)
    },
  }),

  // ── read (#1053): a Bambuddy write still running when its tool returned ──
  defineTool({
    name: 'get_operation',
    label: 'Check a Bambuddy write',
    description:
      "Read a Bambuddy write (a send, a reprint, project filing, a timelapse pull) that was still running when its " +
      'tool returned: its status, and its result or error once it ended.',
    input: z.object({ operation_id: z.string().regex(/^[0-9a-f]{32}$/).describe('The operation id the tool named') }),
    risk: 'read',
    routes: ['GET /api/v1/operations/{operation_id}'],
    handler: async ({ operation_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/operations/{operation_id}', { params: { path: { operation_id } } }),
          `get operation ${operation_id}`,
        ),
      ),
  }),
]

// Print media (issue #307): a Bambuddy archive's images, timelapse and files,
// through ScadBuddy's proxy so the API key stays server-side. Routes:
// backend/scadbuddy/api/prints.py.

/** backend/scadbuddy/api/prints.py `PHOTO_NAME`. */
const photoName = z
  .string()
  .regex(/^[A-Za-z0-9_-]+\.(jpg|jpeg|png|webp)$/, 'must be a photo file name from the archive, e.g. finish_<ts>_<hex>.jpg')

export const printMediaTools: Tool[] = [
  defineTool({
    name: 'get_print_image',
    label: "See a print's picture",
    description:
      "A print's image: with `photo` one of its archive photos (the finish photo is named finish_<ts>_<hex>.jpg), " +
      'with `plate` the slicer image of that plate (1-based), otherwise its thumbnail.',
    input: z.object({ archive_id: archiveId, photo: photoName.optional(), plate: z.number().int().min(1).optional() }),
    risk: 'read',
    routes: [
      'GET /api/v1/prints/{archive_id}/photos/{filename}',
      'GET /api/v1/prints/{archive_id}/plates/{index}/thumbnail',
      'GET /api/v1/prints/{archive_id}/thumbnail',
    ],
    handler: async ({ archive_id, photo, plate }, ctx) => {
      if (photo !== undefined && plate !== undefined) throw new ToolError('give a photo or a plate, not both')
      if (photo !== undefined) {
        return binary(
          ctx.backend.GET('/api/v1/prints/{archive_id}/photos/{filename}', {
            params: { path: { archive_id, filename: photo } },
            parseAs: 'stream',
          }),
          `get photo ${photo} of print ${archive_id}`,
          ctx,
          { path: `/api/v1/prints/${archive_id}/photos/${photo}`, name: photo, fallbackType: 'image/jpeg' },
          image,
        )
      }
      if (plate !== undefined) {
        return binary(
          ctx.backend.GET('/api/v1/prints/{archive_id}/plates/{index}/thumbnail', {
            params: { path: { archive_id, index: plate } },
            parseAs: 'stream',
          }),
          `get plate ${plate} image of print ${archive_id}`,
          ctx,
          {
            path: `/api/v1/prints/${archive_id}/plates/${plate}/thumbnail`,
            name: `print-${archive_id}-plate-${plate}.png`,
            fallbackType: 'image/png',
          },
          image,
        )
      }
      return binary(
        ctx.backend.GET('/api/v1/prints/{archive_id}/thumbnail', { params: { path: { archive_id } }, parseAs: 'stream' }),
        `get thumbnail of print ${archive_id}`,
        ctx,
        { path: `/api/v1/prints/${archive_id}/thumbnail`, name: `print-${archive_id}.png`, fallbackType: 'image/png' },
        image,
      )
    },
  }),

  defineTool({
    name: 'get_print_timelapse',
    label: "Get a print's timelapse",
    description:
      "A print's timelapse video. Usually too large to inline, so it comes back as a link to fetch (it supports Range).",
    input: z.object({ archive_id: archiveId }),
    risk: 'read',
    routes: ['GET /api/v1/prints/{archive_id}/timelapse'],
    handler: async ({ archive_id }, ctx) =>
      binary(
        ctx.backend.GET('/api/v1/prints/{archive_id}/timelapse', { params: { path: { archive_id } }, parseAs: 'stream' }),
        `get timelapse of print ${archive_id}`,
        ctx,
        { path: `/api/v1/prints/${archive_id}/timelapse`, name: `print-${archive_id}.mp4`, fallbackType: 'video/mp4' },
        (bytes, mimeType) => blob(`scadbuddy://prints/${archive_id}/timelapse`, bytes, mimeType),
      ),
  }),

  defineTool({
    name: 'download_print_file',
    label: "Download a print's file",
    description:
      "A print's `sliced` file (what the printer ran) or its `source` slicer project 3MF, embedded as a resource; " +
      'when it is too large to inline, a link to fetch it instead.',
    input: z.object({ archive_id: archiveId, file: z.enum(['sliced', 'source']) }),
    risk: 'read',
    routes: ['GET /api/v1/prints/{archive_id}/files/sliced', 'GET /api/v1/prints/{archive_id}/files/source'],
    handler: async ({ archive_id, file }, ctx) =>
      binary(
        file === 'sliced'
          ? ctx.backend.GET('/api/v1/prints/{archive_id}/files/sliced', { params: { path: { archive_id } }, parseAs: 'stream' })
          : ctx.backend.GET('/api/v1/prints/{archive_id}/files/source', { params: { path: { archive_id } }, parseAs: 'stream' }),
        `download the ${file} file of print ${archive_id}`,
        ctx,
        {
          path: `/api/v1/prints/${archive_id}/files/${file}`,
          name: `print-${archive_id}-${file}.3mf`,
          fallbackType: 'model/3mf',
        },
        (bytes, mimeType) => blob(`scadbuddy://prints/${archive_id}/files/${file}`, bytes, mimeType),
      ),
  }),
]
