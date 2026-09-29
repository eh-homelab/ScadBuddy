import { z } from 'zod'
import { binary } from './binary.js'
import { ok } from './call.js'
import { slug, VIEW } from './common.js'
import { defineTool, image, json, ToolError, type Tool } from './registry.js'

// Looking at a model while authoring it (issue #252): the language server's
// parse errors as data, and one picture per colour of a render. Routes:
// backend/scadbuddy/api/lsp.py and backend/scadbuddy/api/jobs.py.

const jobId = z
  .string()
  .regex(/^[0-9a-f]{32}$/, 'must be a render job id: 32 lowercase hex digits, as render_model returns it')
  .describe('Render job id, as render_model returns it')

/** The backend's `X-ScadBuddy-Colours` (api/jobs.py COLOURS_HEADER), kept to `#RRGGBB` values. */
function coloursOf(header: string | null): string[] {
  return (header ?? '').split(',').filter((c) => /^#[0-9A-Fa-f]{6}$/.test(c))
}

export const inspectTools: Tool[] = [
  defineTool({
    name: 'get_lsp_diagnostics',
    description:
      "The editor's language server (openscad-lsp) on a source: parse errors with line and column ranges, " +
      "in milliseconds and without running OpenSCAD. Give `source` to check text you have not saved, or " +
      "only `slug` to check the model's saved source. With `slug` the source is opened in the model's " +
      "directory, so a leading `include` of a missing file is reported. It does not know OpenSCAD's own " +
      "errors (unknown modules, bad arguments): check_source and get_render_diagnostics do. " +
      '`available: false` means no language server is installed.',
    input: z.object({
      slug: slug.optional(),
      source: z.string().max(1_000_000).optional(),
    }),
    risk: 'read',
    source: "the language server's messages about a model's OpenSCAD source",
    routes: ['POST /api/v1/lsp/diagnostics'],
    handler: async ({ slug, source }, { backend }) => {
      if (source === undefined && slug === undefined) throw new ToolError('give `source`, `slug`, or both')
      const text =
        source ??
        (await ok(
          backend.GET('/api/v1/models/{slug}/source', { params: { path: { slug: slug! } }, parseAs: 'text' }),
          `get source of ${slug}`,
        ))
      return json(
        await ok(
          backend.POST('/api/v1/lsp/diagnostics', { body: { source: text, slug: slug ?? null } }),
          'get language-server diagnostics',
        ),
      )
    },
  }),

  defineTool({
    name: 'get_render_colours',
    description:
      "A finished render drawn once per colour, as one PNG grid: on each tile that colour's parts are in " +
      'their colour and everything else is light grey, so you can see which colour goes where (a letter ' +
      'on the wrong extruder, a colour hidden inside another). The text names the tiles row by row, in ' +
      'extruder order. At most 16 colours.',
    input: z.object({
      job_id: jobId,
      view: VIEW.default('iso'),
      size: z.number().int().min(64).max(512).optional().describe('Edge of each tile in pixels (256 by default)'),
    }),
    risk: 'read',
    routes: ['GET /api/v1/jobs/{job_id}/colours.png'],
    handler: async ({ job_id, view, size }, ctx) => {
      const answered = await ctx.backend.GET('/api/v1/jobs/{job_id}/colours.png', {
        params: { path: { job_id }, query: { view, size } },
        parseAs: 'stream',
      })
      const colours = coloursOf(answered.response.headers.get('x-scadbuddy-colours'))
      const columns = Math.ceil(Math.sqrt(colours.length)) || 1
      const legend = {
        tiles: colours.map((colour, index) => ({
          colour,
          extruder_order: index + 1,
          row: Math.floor(index / columns) + 1,
          column: (index % columns) + 1,
        })),
        view,
      }
      const drawn = await binary(
        Promise.resolve(answered),
        `draw colours of ${job_id}`,
        ctx,
        {
          path: `/api/v1/jobs/${job_id}/colours.png?view=${view}${size ? `&size=${size}` : ''}`,
          name: `${job_id}-colours.png`,
          fallbackType: 'image/png',
        },
        (bytes, mimeType) => image(bytes, mimeType),
      )
      // The legend is the only way to read the grid, so it comes first whether the
      // image is inline or, too large for that, a link (#750 review).
      return drawn.isError ? drawn : { ...drawn, content: [...json(legend).content, ...drawn.content] }
    },
  }),
]
