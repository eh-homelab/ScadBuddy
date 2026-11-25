import { z } from 'zod'
import type { BackendClient } from '../api/backend.js'
import { ok } from './call.js'
import { filamentPaths } from './filamentLinks.js'
import { defineTool, json, ToolError } from './registry.js'

// Files and links on any entity (#6950, phase 1): the generic `/entities/{ref}` routes
// (backend/scadbuddy/api/entity_attachments.py, entity_links.py). Lists are `read`; every
// file or link change is `outward`, so a session parks it for a person. An agent upload is out of scope:
// a file reaches an entity through the UI (an order's through order_attach).

const entity = z
  .string()
  .min(1)
  .max(200)
  .describe('The entity, `kind:ref`: `spool:<id>`, `printer:<id>`, `spool_type:<id>`, `supplier:<id>`, `invoice:<id>`, `location:<id>`, `part:<id>`, `customer:<id>`, `order:<id>`')
/** An order's files change only on its order page (order_attachment_update/_delete). */
const fileOwner = z
  .string()
  .min(1)
  .max(200)
  .describe("The entity, `kind:ref`: `spool:<id>`, `printer:<id>`, `spool_type:<id>`, `supplier:<id>`, `invoice:<id>`, `location:<id>`, `part:<id>`, `customer:<id>`. Not an order's: change those with order_attachment_update/order_attachment_delete")
const attachmentId = z.string().min(1).max(100).describe("An attachment's id, as entity_attachment_list gives it")
const linkId = z.string().min(1).max(100).describe("A link's id, as entity_link_list gives it")
const role = z.string().regex(/^[a-z_]{1,32}$/).describe('Open text: `reference`, `issue`, `finished`, `packed`, `receipt`, `invoice`, `manual`, `photo`, `label`, `other`')
const caption = z.string().max(1000)
const linkKind = z.string().regex(/^[a-z_]{1,32}$/).describe('Open text, `other` when left out: `support`, `datasheet`, `store`, ...')

const deleted = (what: string) => json({ deleted: what })

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
  '`lot_number` (entity_property_schema_get lists them). Any other key is kept as given.'

const code = (k: string) => `\`${k}\``
/** The keywords the backend allows (entities/properties.py TOP_KEYWORDS, KEY_KEYWORDS): `type` is described apart. */
export const SCHEMA_TOP_KEYWORDS = ['type: "object"', 'properties', 'additionalProperties: true', '$schema', '$comment', 'title', 'description', 'examples']
export const SCHEMA_KEY_KEYWORDS = [
  '$comment', 'title', 'description', 'default', 'examples', 'deprecated', 'readOnly', 'writeOnly', 'enum', 'const',
  'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf', 'minLength', 'maxLength', 'format',
  'minItems', 'maxItems', 'uniqueItems',
]

const propertyKind = z.string().regex(/^[a-z_]{1,32}$/).describe('An entity kind: `spool_type`, `spool`, `printer`, `supplier`, ...')

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

/** The page of an entity that has one (the filament kinds); others have none. */
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
    default:
      return undefined
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
      "refuses a key it does not know. An item's schema is its item kind's `attribute_schema`, not a schema of the kind " +
      '`item` (409 `schema_owned_by_item_kind`).',
    input: z.object({ kind: propertyKind }),
    risk: 'read',
    routes: ['GET /api/v1/entity-kinds/{kind}/property-schema'],
    handler: async ({ kind }, { backend }) =>
      json(await ok(backend.GET('/api/v1/entity-kinds/{kind}/property-schema', { params: { path: { kind } } }), `read the ${kind} property schema`)),
  }),
  defineTool({
    name: 'entity_property_schema_set',
    label: "Replace a kind's property schema",
    description:
      "Replace a kind's property schema whole (read it with entity_property_schema_get first and send it back changed). " +
      'A JSON Schema (Draft 2020-12) object of plain keywords only: at the top ' + SCHEMA_TOP_KEYWORDS.map(code).join(', ') +
      '; per key a `type` (a scalar or `array`, or a list of them with `"null"`) with ' + SCHEMA_KEY_KEYWORDS.map(code).join(', ') +
      '; `format` is never `regex`; `items` (string-typed only, with the same keywords but `items`). An `enum` or `const` must fit its key, and no bound may cross its pair or a cap. ' +
      'Any other keyword is 422 `bad_property_schema`, naming it. Writes ' +
      'ScadBuddy only.',
    input: z.object({ kind: propertyKind, schema: z.record(z.string(), z.unknown()).describe('The whole schema') }),
    risk: 'write',
    routes: ['PUT /api/v1/entity-kinds/{kind}/property-schema'],
    summarize: ({ kind }) => `Replace the ${kind} property schema`,
    handler: async ({ kind, schema }, { backend }) =>
      json(await ok(backend.PUT('/api/v1/entity-kinds/{kind}/property-schema', { params: { path: { kind } }, body: { schema } }), `set the ${kind} property schema`)),
  }),
]
