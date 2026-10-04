import { z } from 'zod'
import { command } from './command.js'
import { binary } from './binary.js'
import { ok } from './call.js'
import { slug } from './common.js'
import { blob, defineTool, image, json, ToolError, type Tool } from './registry.js'

// Print history (issue #308): ScadBuddy's own prints, each a Bambuddy archive linked
// to the output that printed it. Routes: backend/scadbuddy/api/print_history.py.

const archiveId = z.number().int().min(1).describe("A Bambuddy print archive's id, as list_prints returns it")
const day = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'must be a day, YYYY-MM-DD')

export const printHistoryTools: Tool[] = [
  defineTool({
    name: 'list_prints',
    description:
      "ScadBuddy's print history, newest first: each print's status, printer, times, filament, cover image and the " +
      'parameters that differ from the template defaults. Filter by template (`slug`), Bambuddy `status` ' +
      '(completed, failed, printing, … or deleted_in_bambuddy), `printer_id`, start day (`from`/`to`, inclusive) and ' +
      'text `q` (output or print name, parameter values). Pass `next_cursor` back as `cursor` for the next page: ' +
      'with a narrow filter a page can be short or empty and still have one, so keep going until it is null.',
    input: z.object({
      slug: slug.optional(),
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
    description:
      'One print: the output it came from (template, revision, exact parameters), its files, photos, timelapse, ' +
      'outcome (status, failure reason, times, filament, cost) and runs. `printer_media: true` also lists the ' +
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
      await command(ctx, `pull timelapse ${filename} onto print ${archive_id}`, (headers) =>
        ctx.backend.POST('/api/v1/prints/{archive_id}/timelapse/pull', {
          params: { path: { archive_id } },
          body: { filename },
          headers,
        }),
      )
      return json({ attached: filename })
    },
  }),

  // ── read (#1053): a Bambuddy write still running when its tool returned ──
  defineTool({
    name: 'get_operation',
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
