import { z } from 'zod'
import { ok } from './call.js'
import { filamentPaths } from './filamentLinks.js'
import { defineTool, json, ToolError } from './registry.js'

// Files and links on any entity (#6950, phase 1): the generic `/entities/{ref}` routes
// (backend/scadbuddy/api/entity_attachments.py, entity_links.py). Lists are `read`; every
// change is `outward`, so a session parks it for a person. An agent upload is out of scope:
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
]
