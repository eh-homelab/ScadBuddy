import { z } from 'zod'
import type { BackendClient } from '../api/backend.js'
import { readCapped } from './binary.js'
import { ok } from './call.js'
import { command } from './command.js'
import { filamentPaths } from './filamentLinks.js'
import { inventoryPaths } from './inventoryLinks.js'
import { defineTool, image, json, ToolError } from './registry.js'
import { WorkspaceError, workspaceOf } from '../workspace/store.js'
import { IMAGE_BYTES_MAX, IMAGE_MEDIA_TYPES } from '../sessions/images.js'
import { FROM_WORKSPACE_ONLY, uploadInit, uploadName, withSent, workspaceFile, workspacePathInput, workspaceShaInput } from './workspaceUploads.js'

// Files and links on any entity (#6950, phase 1): the generic `/entities/{ref}` routes
// (backend/scadbuddy/api/entity_attachments.py, entity_links.py). Lists and a file's bytes are `read`;
// every file or link change is `outward`, so a session parks it for a person. An agent adds a file
// from its workspace (entity_attachment_add); an order's files go through order_attach.

const entity = z
  .string()
  .min(1)
  .max(200)
  .describe('The entity, `kind:ref`: `spool:<id>`, `printer:<id>`, `spool_type:<id>`, `supplier:<id>`, `invoice:<id>`, `location:<id>`, `item:<id>`, `lot:<id>`, `unit:<id>`, `customer:<id>`, `order:<id>`, `insert_type:<id>` (or another filament catalog\'s row: `material`, `subtype`, `brand`, `category`, `effect`, `insert_material`, `colour`, `location_kind`)')
/** An order's files change only on its order page (order_attachment_update/_delete). */
const fileOwner = z
  .string()
  .min(1)
  .max(200)
  .describe("The entity, `kind:ref`: `spool:<id>`, `printer:<id>`, `spool_type:<id>`, `supplier:<id>`, `invoice:<id>`, `location:<id>`, `item:<id>`, `lot:<id>`, `unit:<id>`, `customer:<id>`, `insert_type:<id>` (or another filament catalog's row: `material`, `subtype`, `brand`, `category`, `effect`, `insert_material`, `colour`, `location_kind`). Not an order's: change those with order_attachment_update/order_attachment_delete")
const attachmentId = z.string().regex(/^[A-Za-z0-9_-]{1,100}$/).describe("An attachment's id, as entity_attachment_list gives it")
const linkId = z.string().min(1).max(100).describe("A link's id, as entity_link_list gives it")
const role = z.string().regex(/^[a-z_]{1,32}$/).describe('Open text: `reference`, `issue`, `finished`, `packed`, `receipt`, `invoice`, `manual`, `photo`, `label`, `other`')
const caption = z.string().max(1000)
const linkKind = z.string().regex(/^[a-z_]{1,32}$/).describe('Open text, `other` when left out: `support`, `datasheet`, `store`, ...')

const deleted = (what: string) => json({ deleted: what })
/** The most of a file entity_attachment_get reads: the workspace's default file cap. */
const FILE_READ_MAX = 25 * 1024 * 1024

// Properties (#7026, spec §6.3a): an entity's key-value properties and a kind's property schema
// (backend/scadbuddy/api/entity_properties.py). Reads are `read`; a property or schema write is
// `write`, the same as filament_type_upsert and filament_spool_upsert, which take `properties` too.

const PROPERTY_KEY = /^[a-z][a-z0-9_]{0,63}$/
// The backend counts code points, and refuses U+0000.
const codePoints = (max: number) => (s: string) => [...s].length <= max
const noNul = (s: string) => !s.includes('\u0000')
const propertyString = (max: number) =>
  z.string().refine(codePoints(max), `up to ${max} characters`).refine(noNul, 'no U+0000')
const propertyValue = z.union([propertyString(2000), z.number().finite(), z.boolean(), z.array(propertyString(500)).max(50)])

/** A properties patch: each key to its new value, `null` removes it. */
export const propertySet = z
  .record(z.string().regex(PROPERTY_KEY, 'a key is `^[a-z][a-z0-9_]{0,63}$`'), propertyValue.nullable())
  .refine((set) => Object.keys(set).length > 0 && Object.keys(set).length <= 200, 'give 1 to 200 keys')

export type PropertySet = z.infer<typeof propertySet>

/** The rules every property write follows, for a tool's description. */
export const PROPERTY_RULES =
  'A key is lower case, `^[a-z][a-z0-9_]{0,63}$`; a value is a string (up to 2000 characters), a number, a ' +
  'boolean or a list of up to 50 strings (each up to 500), and `null` removes the key; an entity has up to 200 ' +
  'keys. The whole set applies or none of it (422 `bad_property_value` names the key). A rename is one set: the ' +
  'old key `null` and the new key its value. Suggested keys, which the kind\'s schema documents and checks: on a ' +
  'spool type `mpn`, `gtin` and `asin` (the last two lists), `manufacturer_url`, `empty_spool_weight_g`, `td`; on a spool ' +
  "`lot_number`; on an inventory item the keys its kind's schema types (entity_property_schema_get lists them). " +
  'Any other key is kept as given.'

const code = (k: string) => `\`${k}\``
/** The keywords the backend allows (entities/properties.py TOP_KEYWORDS, KEY_KEYWORDS): `type` is described apart. */
export const SCHEMA_TOP_KEYWORDS = ['type: "object"', 'properties', 'additionalProperties: true', '$schema', '$comment', 'title', 'description', 'examples']
export const SCHEMA_KEY_KEYWORDS = [
  '$comment', 'title', 'description', 'default', 'examples', 'deprecated', 'readOnly', 'writeOnly', 'enum', 'const',
  'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf', 'minLength', 'maxLength', 'format',
  'minItems', 'maxItems', 'uniqueItems',
]

const propertyKind = z.string().regex(/^[a-z_]{1,32}$/).describe('An entity kind: `spool_type`, `spool`, `printer`, `supplier`, `insert_type`, ...')

/**
 * Sets `set` on `ref` and answers its properties. With `saved` (the type or spool whose own write
 * succeeded first), a refusal says it was saved and the properties were not, with the backend's
 * reason, and points at entity_property_set so a retry does not repeat that write.
 */
export async function setPropertiesAfter(backend: BackendClient, ref: string, set: PropertySet, saved?: string) {
  try {
    const { properties } = await ok(backend.PATCH('/api/v1/entities/{ref}/properties', { params: { path: { ref } }, body: { set } }), `set the properties of ${ref}`)
    return properties
  } catch (e) {
    if (saved === undefined) throw e
    throw propertiesNotSet(`${saved} was saved; its properties were not (set them with entity_property_set on ${ref}, not this tool again)`, e)
  }
}

/** A failed property write after its row was saved, led by what was saved and what is left to set. */
export function propertiesNotSet(lead: string, e: unknown): ToolError {
  if (e instanceof ToolError) return new ToolError(`${lead}: ${e.summary}`, e.status, e.untrusted, e.problem)
  return new ToolError(`${lead}: ${e instanceof Error ? e.message : String(e)}`)
}

/** For an upsert's `summarize`: the properties it sets and removes, if any. */
export function propertyChanges(set: PropertySet | undefined): string {
  if (set === undefined) return ''
  const keys = Object.keys(set)
  const removed = keys.filter((key) => set[key] === null)
  const setKeys = keys.filter((key) => set[key] !== null)
  const parts = [setKeys.length ? `set ${setKeys.join(', ')}` : '', removed.length ? `remove ${removed.join(', ')}` : ''].filter(Boolean)
  return `; properties: ${parts.join('; ')}`
}

/** The catalog of each filament catalog row's entity kind (backend/scadbuddy/entities/registry.py `KINDS`). */
const CATALOG_OF: ReadonlyMap<string, string> = new Map([
  ['material', 'materials'],
  ['subtype', 'subtypes'],
  ['brand', 'brands'],
  ['category', 'categories'],
  ['effect', 'effects'],
  ['insert_material', 'insert-materials'],
  ['insert_type', 'insert-types'],
  ['colour', 'colours'],
  ['location_kind', 'location-kinds'],
])

/** The page of an entity that has one (the filament and inventory kinds; a catalog row's is its catalog); others have none. */
function uiPath(ref: string): string | undefined {
  const at = ref.indexOf(':')
  const kind = ref.slice(0, at)
  const id = ref.slice(at + 1)
  if (at < 1 || id === '') return undefined
  switch (kind) {
    case 'spool':
      return filamentPaths.spool(id)
    case 'spool_type':
      return filamentPaths.type(id)
    case 'supplier':
      return filamentPaths.supplier(id)
    case 'invoice':
      return filamentPaths.invoice(id)
    case 'location':
      return filamentPaths.location(id)
    case 'item':
      return inventoryPaths.item(id)
    case 'lot':
      return inventoryPaths.lot(id)
    case 'unit':
      return inventoryPaths.unit(id)
    default: {
      const catalog = CATALOG_OF.get(kind)
      return catalog === undefined ? undefined : filamentPaths.catalog(catalog)
    }
  }
}

/** A 404 `entity_not_found` named for the entity the call was about. */
async function onEntity<T>(ref: string, pending: Promise<{ data?: T; error?: unknown; response: Response }>, what: string): Promise<T> {
  try {
    return await ok(pending, what)
  } catch (e) {
    if (e instanceof ToolError && e.problem === 'entity_not_found') {
      throw new ToolError(`${what} failed: no entity ${ref} (HTTP 404, entity_not_found)`, 404, e.untrusted, e.problem)
    }
    throw e
  }
}

const withPage = (ref: string, answer: object) => {
  const page = uiPath(ref)
  return json(page ? { ...answer, entity_ui_path: page } : answer)
}

const changes = (fields: Record<string, unknown>) => Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== undefined))

export const entityTools = [
  defineTool({
    name: 'entity_attachment_list',
    label: "List an entity's files",
    description:
      "The files kept on an entity (a spool's label photo, a printer's manual), oldest first. An order's files are the ones order_get lists. Answers `entity_ui_path` when the entity has a page.",
    input: z.object({ entity }),
    risk: 'read',
    routes: ['GET /api/v1/entities/{ref}/attachments'],
    handler: async ({ entity: ref }, { backend }) =>
      withPage(ref, await onEntity(ref, backend.GET('/api/v1/entities/{ref}/attachments', { params: { path: { ref } } }), `list the files of ${ref}`)),
  }),
  defineTool({
    name: 'entity_attachment_get',
    label: "See an entity's file",
    description:
      "One file kept on an entity, as entity_attachment_list lists it: a PNG, JPEG, GIF or WebP photo of up to " +
      'about 3.75 MB as an image you can see (5 MB as base64, the most one image may be); a larger one, or any ' +
      'other image type (HEIC, AVIF, TIFF, ...), only as a note that you cannot view it; an SVG, and any other file (a PDF ' +
      'manual, a receipt) kept in your workspace and answered as `workspace_file`: workspace_read reads a text ' +
      'file, and a binary one (a PDF) can be attached or uploaded from there but not read. `thumbnail` asks for a small WebP copy of a PNG, JPEG or WebP photo (404 for anything else): use ' +
      'it for a photo too large to view, or when a glance is enough.',
    input: z.object({
      entity,
      attachment_id: attachmentId,
      thumbnail: z.boolean().optional().describe('A small WebP copy of a photo instead of the file itself'),
    }),
    risk: 'read',
    routes: [
      'GET /api/v1/entities/{ref}/attachments',
      'GET /api/v1/entities/{ref}/attachments/{attachment_id}/content',
      'GET /api/v1/entities/{ref}/attachments/{attachment_id}/thumbnail',
    ],
    handler: async ({ entity: ref, attachment_id, thumbnail }, ctx) => {
      const params = { params: { path: { ref, attachment_id } }, parseAs: 'stream' as const }
      const what = `get file ${attachment_id} of ${ref}`
      // The file's own name and type: the content route sends any type it does not serve inline
      // as application/octet-stream (backend api/attachment_serving.py), so its header cannot say.
      const { attachments } = await onEntity(ref, ctx.backend.GET('/api/v1/entities/{ref}/attachments', { params: { path: { ref } } }), `list the files of ${ref}`)
      const record = attachments.find((a) => a.id === attachment_id)
      if (!record) throw new ToolError(`${what} failed: ${ref} has no file ${attachment_id}; entity_attachment_list lists them`, 404)
      const result = await (thumbnail
        ? ctx.backend.GET('/api/v1/entities/{ref}/attachments/{attachment_id}/thumbnail', params)
        : ctx.backend.GET('/api/v1/entities/{ref}/attachments/{attachment_id}/content', params))
      const stream = (await ok(Promise.resolve(result), what)) as ReadableStream<Uint8Array> | null
      const mimeType = thumbnail ? 'image/webp' : record.media_type
      const length = result.response.headers.get('content-length')
      const read = await readCapped(stream, length !== null && /^\d+$/.test(length) ? Number(length) : undefined, FILE_READ_MAX)
      // Never a backend path: a model given one guessed a URL for it (#7727).
      if (!read.inline) {
        const thumbable = !thumbnail && ['image/png', 'image/jpeg', 'image/webp'].includes(mimeType)
        return json({
          viewable: false,
          media_type: mimeType,
          size_bytes: read.size ?? null,
          note: `Larger than ${FILE_READ_MAX} bytes: too large to read here.` + (thumbable ? ' Call again with `thumbnail: true` for a small copy.' : ''),
        })
      }
      const bytes = read.bytes
      if ((IMAGE_MEDIA_TYPES as readonly string[]).includes(mimeType)) {
        // The model refuses an image over IMAGE_DATA_MAX as base64, as workspace_read guards.
        if (bytes.byteLength <= IMAGE_BYTES_MAX) return image(bytes, mimeType)
        return json({
          viewable: false,
          media_type: mimeType,
          size_bytes: bytes.byteLength,
          note:
            'A photo too large for you to view (over 5 MB as base64).' +
            (mimeType === 'image/gif' ? '' : ' Call again with `thumbnail: true` for a small copy.'),
        })
      }
      // Any other image type (HEIC, AVIF, TIFF, ...) is kept as an image, but an image block of it is refused.
      if (mimeType.startsWith('image/') && mimeType !== 'image/svg+xml') {
        return json({
          viewable: false,
          media_type: mimeType,
          size_bytes: bytes.byteLength,
          note: `A ${mimeType} photo, which you cannot view: only PNG, JPEG, GIF and WebP can be shown to you. Ask the person for a JPEG or PNG copy.`,
        })
      }
      // Any other file (a PDF manual, a receipt) goes to the workspace as a pointer, which a session turn
      // carries (a `resource` block reaches it only as "[resource]", temporal/toolActivities.ts callBlocks).
      if (!ctx.workspace) {
        return json({ media_type: mimeType, size_bytes: bytes.byteLength, note: 'There is no workspace to keep it in here.' })
      }
      try {
        const name = record.filename.replace(/[^A-Za-z0-9_.-]/g, '_').replace(/^\.+/, '').slice(0, 100) || 'file'
        const info = await ctx.workspace.write(workspaceOf(ctx), `attachments/${attachment_id}-${name}`, new Uint8Array(bytes), {
          mediaType: mimeType,
          source: { tool: 'entity_attachment_get', entity: ref, attachment_id },
        })
        return json({
          workspace_file: { name: info.path, mediaType: info.mediaType, size: info.size, sha256: info.sha256 },
          note: 'Kept in your workspace: workspace_read reads it when it is text; a binary file can be attached or uploaded from there.',
        })
      } catch (err) {
        if (err instanceof WorkspaceError) return json({ media_type: mimeType, size_bytes: bytes.byteLength, note: `Not kept: ${err.message}` })
        throw err
      }
    },
  }),
  defineTool({
    name: 'entity_attachment_add',
    label: 'Attach a file to an entity',
    description:
      "Attach a file from your workspace to an entity: a spool's label photo, a printer's manual, a receipt. Any " +
      "file; what it is (`kind`, `media_type`) comes from its bytes. `role` says why it is kept (`other` when left " +
      "out). An order's files go through order_attach (409 `use_order_route`). Outward: a person approves it." +
      FROM_WORKSPACE_ONLY,
    input: z
      .object({
        entity: fileOwner.describe(
          "The entity, `kind:ref`: `spool:<id>`, `printer:<id>`, `spool_type:<id>`, `supplier:<id>`, `invoice:<id>`, `location:<id>`, `item:<id>`, `lot:<id>`, `unit:<id>`, `customer:<id>`, `insert_type:<id>` (or another filament catalog's row). Not an order's: attach those with order_attach",
        ),
        workspace_path: workspacePathInput,
        workspace_sha256: workspaceShaInput,
        filename: z.string().min(1).max(255).optional().describe("e.g. \"label.jpg\"; the workspace file's own name by default"),
        role: role.optional(),
        caption: caption.optional(),
      })
      .strict(),
    risk: 'outward',
    routes: ['POST /api/v1/entities/{ref}/attachments'],
    summarize: ({ entity: ref, filename, workspace_path }) => `Attach ${filename ?? workspace_path} to ${ref}`,
    handler: async ({ entity: ref, workspace_path, workspace_sha256, filename, role, caption }, ctx) => {
      const info = await workspaceFile(ctx, { path: workspace_path, sha256: workspace_sha256 }, 'entity_attachment_add')
      const name = filename ?? uploadName(info.path)
      const fields = {
        ...(role === undefined ? {} : { role }),
        ...(caption === undefined ? {} : { caption }),
      }
      const part = { name: 'file', filename: name, type: info.mediaType }
      const what = `attach ${name} to ${ref}`
      const answer = await command(ctx, what, (headers) =>
        ctx.backend.POST('/api/v1/entities/{ref}/attachments', {
          params: { path: { ref } },
          body: { file: name, role: role ?? null, caption: caption ?? null },
          ...uploadInit(ctx, info, part, headers, fields),
        }),
      )
      return json(withSent(answer, info))
    },
  }),
  defineTool({
    name: 'entity_attachment_update',
    label: "Edit an entity's file",
    description:
      "Caption or re-role a file kept on an entity. An order's files go through order_attachment_update (409 `use_order_route`). Outward: a person approves it.",
    input: z.object({ entity: fileOwner, attachment_id: attachmentId, caption: caption.optional(), role: role.optional() }),
    risk: 'outward',
    routes: ['PATCH /api/v1/entities/{ref}/attachments/{attachment_id}'],
    summarize: ({ entity: ref, attachment_id }) => `Edit file ${attachment_id} of ${ref}`,
    handler: async ({ entity: ref, attachment_id, caption, role }, { backend }) => {
      const body = changes({ caption, role })
      if (Object.keys(body).length === 0) throw new ToolError('nothing to change: give `caption` or `role`')
      return json(
        await onEntity(
          ref,
          backend.PATCH('/api/v1/entities/{ref}/attachments/{attachment_id}', { params: { path: { ref, attachment_id } }, body }),
          `edit file ${attachment_id} of ${ref}`,
        ),
      )
    },
  }),
  defineTool({
    name: 'entity_attachment_delete',
    label: "Remove an entity's file",
    description: "Remove a file kept on an entity. An order's files go through order_attachment_delete. Outward: no history brings the file back.",
    input: z.object({ entity: fileOwner, attachment_id: attachmentId }),
    risk: 'outward',
    routes: ['DELETE /api/v1/entities/{ref}/attachments/{attachment_id}'],
    summarize: ({ entity: ref, attachment_id }) => `Remove file ${attachment_id} from ${ref}`,
    handler: async ({ entity: ref, attachment_id }, { backend }) => {
      await onEntity(
        ref,
        backend.DELETE('/api/v1/entities/{ref}/attachments/{attachment_id}', { params: { path: { ref, attachment_id } } }),
        `remove file ${attachment_id} from ${ref}`,
      )
      return deleted(attachment_id)
    },
  }),
  defineTool({
    name: 'entity_link_list',
    label: "List an entity's links",
    description: 'The web links kept on an entity (a support page, a datasheet, a store listing), by position. Answers `entity_ui_path` when the entity has a page.',
    input: z.object({ entity }),
    risk: 'read',
    routes: ['GET /api/v1/entities/{ref}/links'],
    handler: async ({ entity: ref }, { backend }) =>
      withPage(ref, await onEntity(ref, backend.GET('/api/v1/entities/{ref}/links', { params: { path: { ref } } }), `list the links of ${ref}`)),
  }),
  defineTool({
    name: 'entity_link_add',
    label: 'Add a link to an entity',
    description: 'Add a web link to an entity. `url` is http or https with a host (422 `bad_link_url`). Outward: a person approves it.',
    input: z.object({
      entity,
      url: z.string().min(1).max(2000),
      kind: linkKind.optional(),
      title: z.string().max(200).optional(),
      note: z.string().max(1000).optional(),
    }),
    risk: 'outward',
    routes: ['POST /api/v1/entities/{ref}/links'],
    summarize: ({ entity: ref, url }) => `Add the link ${url} to ${ref}`,
    handler: async ({ entity: ref, url, kind, title, note }, { backend }) =>
      json(
        await onEntity(
          ref,
          backend.POST('/api/v1/entities/{ref}/links', { params: { path: { ref } }, body: { url, ...changes({ kind, title, note }) } }),
          `add a link to ${ref}`,
        ),
      ),
  }),
  defineTool({
    name: 'entity_link_update',
    label: "Edit an entity's link",
    description: 'Change a link kept on an entity; a field left out stays. Outward: a person approves it.',
    input: z.object({
      entity,
      link_id: linkId,
      url: z.string().min(1).max(2000).optional(),
      kind: linkKind.optional(),
      title: z.string().max(200).optional(),
      note: z.string().max(1000).optional(),
      position: z.number().int().min(0).optional(),
    }),
    risk: 'outward',
    routes: ['PATCH /api/v1/entities/{ref}/links/{link_id}'],
    summarize: ({ entity: ref, link_id }) => `Edit link ${link_id} of ${ref}`,
    handler: async ({ entity: ref, link_id, url, kind, title, note, position }, { backend }) => {
      const body = changes({ url, kind, title, note, position })
      if (Object.keys(body).length === 0) throw new ToolError('nothing to change: give `url`, `kind`, `title`, `note` or `position`')
      return json(
        await onEntity(ref, backend.PATCH('/api/v1/entities/{ref}/links/{link_id}', { params: { path: { ref, link_id } }, body }), `edit link ${link_id} of ${ref}`),
      )
    },
  }),
  defineTool({
    name: 'entity_link_delete',
    label: "Remove an entity's link",
    description: 'Remove a link kept on an entity. Outward: a person approves it.',
    input: z.object({ entity, link_id: linkId }),
    risk: 'outward',
    routes: ['DELETE /api/v1/entities/{ref}/links/{link_id}'],
    summarize: ({ entity: ref, link_id }) => `Remove link ${link_id} from ${ref}`,
    handler: async ({ entity: ref, link_id }, { backend }) => {
      await onEntity(ref, backend.DELETE('/api/v1/entities/{ref}/links/{link_id}', { params: { path: { ref, link_id } } }), `remove link ${link_id} from ${ref}`)
      return deleted(link_id)
    },
  }),
  defineTool({
    name: 'entity_property_list',
    label: "List an entity's properties",
    description:
      "The key-value properties kept on an entity (a spool type's GTIN or MPN, a spool's lot), by key, each with its " +
      '`value` and `source`. A print archive has none (422 `unknown_entity_kind`). Answers `entity_ui_path` when the entity has a page.',
    input: z.object({ entity }),
    risk: 'read',
    routes: ['GET /api/v1/entities/{ref}/properties'],
    handler: async ({ entity: ref }, { backend }) =>
      withPage(ref, await onEntity(ref, backend.GET('/api/v1/entities/{ref}/properties', { params: { path: { ref } } }), `list the properties of ${ref}`)),
  }),
  defineTool({
    name: 'entity_property_set',
    label: "Set an entity's properties",
    description: `Set or remove properties on an entity; keys left out stay. ${PROPERTY_RULES} Answers the entity's properties. Writes ScadBuddy only.`,
    input: z.object({ entity, set: propertySet.describe('Each key to its new value; `null` removes the key') }),
    risk: 'write',
    routes: ['PATCH /api/v1/entities/{ref}/properties'],
    summarize: ({ entity: ref, set }) => `Change the properties of ${ref}${propertyChanges(set)}`,
    handler: async ({ entity: ref, set }, { backend }) =>
      withPage(
        ref,
        await onEntity(ref, backend.PATCH('/api/v1/entities/{ref}/properties', { params: { path: { ref } }, body: { set } }), `set the properties of ${ref}`),
      ),
  }),
  defineTool({
    name: 'entity_property_schema_get',
    label: "Read a kind's property schema",
    description:
      "A kind's property schema: a JSON Schema (Draft 2020-12) object whose `properties` document and type the keys the " +
      'kind knows (their titles and help), and `keys_in_use`, each key its entities have with a count. The schema never ' +
      "refuses a key it does not know. Give `kind` for a kind's schema, or `entity` (`kind:ref`) for the schema that " +
      "entity's properties follow. An item's schema is its item kind's `attribute_schema` merged down the kind tree, " +
      "read with `entity: \"item:<id>\"` or inventory_kind_list; `kind: \"item\"` is 409 `schema_owned_by_item_kind`, and " +
      'inventory_kind_upsert changes it.',
    input: z.object({ kind: propertyKind.optional(), entity: entity.optional().describe("An entity, `kind:ref`, for the schema its own properties follow") }),
    risk: 'read',
    routes: ['GET /api/v1/entity-kinds/{kind}/property-schema', 'GET /api/v1/entities/{ref}/property-schema'],
    handler: async ({ kind, entity: ref }, { backend }) => {
      if (kind === undefined && ref === undefined) throw new ToolError('give one of `kind` or `entity`')
      if (kind !== undefined && ref !== undefined) throw new ToolError('give `kind` or `entity`, not both')
      if (ref !== undefined) {
        return withPage(
          ref,
          await onEntity(ref, backend.GET('/api/v1/entities/{ref}/property-schema', { params: { path: { ref } } }), `read the property schema of ${ref}`),
        )
      }
      return json(await ok(backend.GET('/api/v1/entity-kinds/{kind}/property-schema', { params: { path: { kind: kind! } } }), `read the ${kind} property schema`))
    },
  }),
  defineTool({
    name: 'entity_property_schema_set',
    label: "Replace a kind's property schema",
    description:
      "Replace a kind's property schema whole (read it with entity_property_schema_get first and send it back changed). " +
      'A JSON Schema (Draft 2020-12) object of plain keywords only: at the top ' + SCHEMA_TOP_KEYWORDS.map(code).join(', ') +
      '; per key a `type` (a scalar or `array`, or a list of them with `"null"`) with ' + SCHEMA_KEY_KEYWORDS.map(code).join(', ') +
      '; `format` is never `regex`; `items` (string-typed only, with the same keywords but `items`). An `enum` or `const` must fit its key, and no bound may cross its pair or a cap. ' +
      'Any other keyword is 422 `bad_property_schema`, naming it. An inventory item kind\'s schema is its ' +
      '`attribute_schema`, which inventory_kind_upsert changes (`kind: "item"` is 409 `schema_owned_by_item_kind`). Writes ' +
      'ScadBuddy only.',
    input: z.object({ kind: propertyKind, schema: z.record(z.string(), z.unknown()).describe('The whole schema') }),
    risk: 'write',
    routes: ['PUT /api/v1/entity-kinds/{kind}/property-schema'],
    summarize: ({ kind }) => `Replace the ${kind} property schema`,
    handler: async ({ kind, schema }, { backend }) =>
      json(await ok(backend.PUT('/api/v1/entity-kinds/{kind}/property-schema', { params: { path: { kind } }, body: { schema } }), `set the ${kind} property schema`)),
  }),
]
