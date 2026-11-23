import { z } from 'zod'
import type { components } from '../api/schema.js'
import { ok } from './call.js'
import { outputId, slug } from './common.js'
import { defineTool, json, type Tool, type ToolContext, ToolError } from './registry.js'
import {
  colourProblems,
  declaredSlots,
  defaultsFor,
  draftMaterials,
  fileColours,
  fileTitle,
  fileTitleColours,
  isSupportMaterial,
  LISTED_TYPES,
  platePictures,
  sellMedia,
  titleColours,
  withColourChoices,
  withoutWords,
} from './storefrontDraft.js'

// #6131 (design 2026-11-13 §6) — the assistant's way onto the public storefront: draft a
// listing from a saved output, a model or a library file (#6710) with the Storefront page's smart defaults
// (`storefrontDraft.ts`), unpublished (`write`), then publish it, which puts it in front of
// customers and so waits for a person's approval (`outward`). The rest of the listing routes
// (edit, order, unpublish, delete) stay the page's (`coverage.ts`).

type Job = Pick<components['schemas']['JobStatus'], 'started_at' | 'finished_at'>
type Value = boolean | number | string

/** How long a render may take before the storefront's preview and price give up (#6120). */
const SLOW_RENDER_SECONDS = 100

const value = z.union([z.string().max(10_000), z.number(), z.boolean()])

/** The output's job, for its render time: a job pruned since (404) is none, never a refusal. */
async function jobOf(id: string, { backend }: ToolContext): Promise<Job | null> {
  const { data, response } = await backend.GET('/api/v1/jobs/{job_id}', { params: { path: { job_id: id } } })
  if (response.status === 404) return null
  if (!response.ok || !data) throw new ToolError(`read render job ${id} failed (HTTP ${response.status})`, response.status)
  return data
}

function renderSeconds(job: Job | null): number | null {
  if (!job?.started_at || !job.finished_at) return null
  return (Date.parse(job.finished_at) - Date.parse(job.started_at)) / 1000
}

/**
 * #6710 (design 2026-11-21 §9.3) — a new listing of a library file, as the editor fills one in
 * from "Sell on storefront…": the file of the library that serves, its name for the title, the
 * colours nearest its parts' own with a support filament for each support part, and every
 * plate's picture. The body leaves the pin to publish (§4.1).
 */
async function fileDraft(fileId: number, title: string | undefined, { backend }: ToolContext): Promise<ReturnType<typeof json>> {
  const path = { params: { path: { file_id: fileId } } }
  const settings = await ok(backend.GET('/api/v1/settings'), 'read the settings')
  const entry = await ok(backend.GET('/api/v1/print/library/{file_id}', path), `read library file ${fileId}`)
  if (!LISTED_TYPES.includes((entry.file_type || '').toLowerCase())) {
    throw new ToolError(
      entry.sliced
        ? `${entry.filename} is a sliced file, fixed for one printer and one set of colours: a listing sells a 3MF or an STL`
        : `${entry.filename} is not a 3MF or an STL`,
    )
  }
  const options = await ok(backend.GET('/api/v1/print/library/{file_id}/filaments', path), `read the parts of ${entry.filename}`)
  const plates = await ok(backend.GET('/api/v1/print/library/{file_id}/plates', path), `read the plates of ${entry.filename}`)
  const book = await ok(backend.GET('/api/v1/orders/price-book'), 'read the price book')
  const filaments = book.filaments
  const slots = declaredSlots(options.slots ?? [])
  const colours = fileColours(slots, filaments)
  const fallback = fileTitle(entry.filename, filaments)
  const asked = (title ?? fallback).slice(0, 200)
  const colourWords = fileTitleColours(asked, colours, filaments)
  const notes: string[] = []
  if (colourWords.length > 0) notes.push(`Took ${colourWords.join(' and ')} out of the title: the customer chooses the colour.`)
  // What publish would refuse (§5.1 rows 7 and 8), said now so the person can fix the price book.
  const listed = filaments.filter((filament) => colours.includes(filament.id))
  if (!listed.some((filament) => !isSupportMaterial(filament.material))) {
    notes.push('The price book has no priced filament a customer can pick: publish refuses the listing until one is added and ticked on the page.')
  }
  if (slots.some((slot) => slot.support) && !listed.some((filament) => isSupportMaterial(filament.material))) {
    notes.push('The file has a support part and the price book has no priced support filament: publish refuses it (listing_file_support_unset) until one is added and ticked on the page.')
  }
  const made = await ok(
    backend.POST('/api/v1/storefront/listings', {
      body: {
        source: 'library_file',
        library_file: { id: entry.id, library: settings.library_backend, repin: false },
        title: withoutWords(asked, colourWords) || fallback,
        description: '',
        params: {},
        fixed: {},
        colours,
        media_ids: platePictures(plates),
        // As for a design (#6258, #6302 review): the materials of the filaments it offers.
        materials: draftMaterials(colours, filaments),
      },
    }),
    `draft a storefront listing of ${entry.filename}`,
  )
  return json({
    listing_id: made.id,
    title: made.title,
    published: made.published,
    offered: [],
    colour_problems: [],
    notes,
    page: `/storefront/listings/${encodeURIComponent(made.id)}`,
  })
}

export const storefrontTools: Tool[] = [
  defineTool({
    name: 'storefront_listing_draft',
    label: 'Draft a storefront listing',
    description:
      'Draft a public storefront listing (#6131), unpublished, as the Storefront page\'s "Sell on storefront" fills one ' +
      'in: from a saved output (`output_id`: its design version, values and name) or from a model (`slug`, with ' +
      '`values` for the parameters to set; the template defaults otherwise, at its current version). Free text is ' +
      "the customer's to type and every colour is the customer's to choose, from the price book's filaments of the " +
      "design's material (any material when no colour matches one); everything else is locked at its value. `lock` " +
      'locks named parameters at their value, colours included, only when the person asked for that. Support ' +
      'settings (a parameter that names support or interface) are never offered, and a support colour is never a ' +
      "colour on offer. The model's " +
      'images are its pictures. Or from a library file (`library_file_id`, #6710: a 3MF or an STL of the library ' +
      "that serves, sold as it is): the customer picks each part's colour, from the price book's filaments of the " +
      "material nearest the file's own colours; a support part is locked to a priced support filament; the title " +
      "is the file's name; every plate's picture is a picture; publish pins the file's version then. " +
      'A colour the customer can choose never goes in the title: it is taken out. ' +
      'Answers the listing id, `colour_problems` (colours no filament has: publish refuses them until the person ' +
      'picks a filament on the page or adds one to the price book), `notes` (a version taken instead, a slow ' +
      'render, a filament the price book lacks) and `page`, the link that opens it in the editor. Nothing is on the store until ' +
      'storefront_listing_publish, which a person approves.',
    input: z.object({
      output_id: outputId.optional().describe('The saved output to sell, as list_outputs returns it'),
      library_file_id: z
        .number()
        .int()
        .min(1)
        .optional()
        .describe('A 3MF or STL of the library that serves to sell as it is (#6710), as list_library or search_library returns it'),
      slug: slug.optional().describe('The model to sell, when there is no saved output'),
      values: z.record(z.string(), value).optional().describe("With `slug`: parameter values to set; the rest take the template's defaults"),
      title: z.string().min(1).max(200).optional().describe("The product's title; the output's name, else the model's, by default"),
      lock: z
        .array(z.string().min(1).max(200))
        .max(200)
        .optional()
        .describe('Parameters the customer may not change, locked at their value: only those the person asked to lock'),
    }),
    risk: 'write',
    routes: [
      'GET /api/v1/outputs/{output_id}',
      'GET /api/v1/jobs/{job_id}',
      'GET /api/v1/models/{slug}',
      'GET /api/v1/models/{slug}/versions',
      'GET /api/v1/models/{slug}/versions/{commit}/schema',
      'GET /api/v1/orders/price-book',
      'GET /api/v1/settings',
      'GET /api/v1/print/library/{file_id}',
      'GET /api/v1/print/library/{file_id}/filaments',
      'GET /api/v1/print/library/{file_id}/plates',
      'POST /api/v1/storefront/listings',
    ],
    summarize: ({ output_id, slug: model, library_file_id }) =>
      `Draft a storefront listing of ${output_id ? `output ${output_id}` : library_file_id !== undefined ? `library file ${library_file_id}` : `model ${model ?? '?'}`}`,
    handler: async ({ output_id, slug: model, library_file_id, values, title, lock }, ctx) => {
      const { backend } = ctx
      if ([output_id, model, library_file_id].filter((source) => source !== undefined).length !== 1) {
        throw new ToolError('name exactly one of output_id, slug and library_file_id')
      }
      if (model === undefined && values !== undefined) throw new ToolError('values go with slug; an output or a file brings its own')
      if (library_file_id !== undefined) {
        if (lock !== undefined) throw new ToolError('lock goes with a design; a file listing has no parameters to lock')
        return fileDraft(library_file_id, title, ctx)
      }
      const output = output_id ? await ok(backend.GET('/api/v1/outputs/{output_id}', { params: { path: { output_id } } }), `read output ${output_id}`) : null
      const job = output ? await jobOf(output.job_id, ctx) : null
      const name = output ? output.slug : (model as string)
      const record = await ok(backend.GET('/api/v1/models/{slug}', { params: { path: { slug: name } } }), `read model ${name}`)
      const versions = await ok(backend.GET('/api/v1/models/{slug}/versions', { params: { path: { slug: name } } }), `list the versions of ${name}`)
      const current = versions.find((v) => v.current) ?? versions[0]
      const pinned = output?.model_version ? versions.find((v) => v.commit === output.model_version) : undefined
      const commit = pinned?.commit ?? current?.commit
      if (!commit) throw new ToolError(`${name} has no version in its history to sell`)
      const notes: string[] = []
      if (output && !pinned) notes.push(`The output was made from a version not in the design's history; the listing uses the current version (${current?.short ?? commit.slice(0, 7)}).`)
      const schema = await ok(
        backend.GET('/api/v1/models/{slug}/versions/{commit}/schema', { params: { path: { slug: name, commit } } }),
        `read the schema of ${name} at ${commit.slice(0, 7)}`,
      )
      const book = await ok(backend.GET('/api/v1/orders/price-book'), 'read the price book')
      const filaments = book.filaments
      // A name the schema does not have would be dropped and leave the template's default, so
      // it is refused, as `lock` refuses one.
      for (const name of Object.keys(values ?? {})) {
        if (!(schema.parameters ?? []).some((p) => p.name === name)) throw new ToolError(`${name} is not a parameter of ${record.name}`)
      }
      const given = (output?.params ?? values ?? {}) as Record<string, Value>
      const listing = defaultsFor(schema, filaments, given)
      for (const name of lock ?? []) {
        const param = (schema.parameters ?? []).find((p) => p.name === name)
        if (!param) throw new ToolError(`${name} is not a parameter of ${record.name}`)
        const locked = given[name] ?? param.initial
        delete listing.params[name]
        if (locked !== undefined && locked !== null) listing.fixed[name] = locked
      }
      const took = renderSeconds(job)
      if (took !== null && took > SLOW_RENDER_SECONDS) {
        notes.push(`This design took ${Math.round(took)} s to render; the store's preview and price give up after about ${SLOW_RENDER_SECONDS} s (#6120).`)
      }
      const fallback = record.name.slice(0, 200)
      const asked = (title ?? output?.name ?? fallback).slice(0, 200)
      const colourWords = titleColours(asked, listing, schema, filaments)
      const shownTitle = withoutWords(asked, colourWords) || fallback
      if (colourWords.length > 0) notes.push(`Took ${colourWords.join(' and ')} out of the title: the customer chooses the colour.`)
      const made = await ok(
        backend.POST('/api/v1/storefront/listings', {
          body: {
            slug: name,
            commit,
            title: shownTitle,
            description: '',
            ...withColourChoices(listing, schema, filaments),
            colours: listing.colours,
            media_ids: sellMedia(record.media ?? []),
            // #6258, #6302 review: the materials of the filaments it offers, so a design of a material
            // the shop's default (`storefront_materials`) leaves out still publishes. The listing
            // editor's "Materials allowed" changes them later, and an empty list means the shop's
            // default (Settings → Storefront pricing → Storefront materials).
            materials: draftMaterials(listing.colours, filaments),
          },
        }),
        `draft a storefront listing of ${name}`,
      )
      return json({
        listing_id: made.id,
        title: made.title,
        published: made.published,
        offered: Object.keys(made.params ?? {}),
        colour_problems: colourProblems(listing, schema, filaments),
        notes,
        page: `/storefront/listings/${encodeURIComponent(made.id)}`,
      })
    },
  }),
  defineTool({
    name: 'storefront_listing_publish',
    label: 'Publish a storefront listing',
    description:
      "Publish a storefront listing (#6131): put it on the public store, where customers can order it. A person approves " +
      "it first. The backend checks it against its template (or its library file) and the price book: 422 " +
      "`listing_colour_unknown` names a colour no listing filament has (pick one on the Storefront page), a " +
      "`listing_file_*` code a file it cannot sell or a part no colour on offer fits (#6710), 409 " +
      "`storefront_needs_s3` when the store is not S3. A file listing's first publish pins the file's version.",
    input: z.object({ listing_id: z.string().min(1).max(100).describe('The listing id storefront_listing_draft answered') }),
    risk: 'outward',
    routes: ['POST /api/v1/storefront/listings/{listing_id}/publish'],
    summarize: ({ listing_id }) => `Publish storefront listing ${listing_id} to the public store`,
    handler: async ({ listing_id }, { backend }) => {
      const published = await ok(
        backend.POST('/api/v1/storefront/listings/{listing_id}/publish', { params: { path: { listing_id } } }),
        `publish storefront listing ${listing_id}`,
      )
      return json({ listing_id: published.id, title: published.title, published: published.published, published_at: published.published_at })
    },
  }),
]
