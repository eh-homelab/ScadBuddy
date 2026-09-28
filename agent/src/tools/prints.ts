import { z } from 'zod'
import { binary } from './binary.js'
import { blob, defineTool, image, ToolError, type Tool } from './registry.js'

// Print media (issue #307): a Bambuddy archive's images, timelapse and files,
// through ScadBuddy's proxy so the API key stays server-side. Routes:
// backend/scadbuddy/api/prints.py.

const archiveId = z.number().int().min(1).describe("A Bambuddy print archive's id")
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
