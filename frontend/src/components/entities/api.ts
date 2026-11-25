import { API_BASE, request, uploadForm } from '../../api/client'
import type { components } from '../../api/schema'
import { propertyTexts } from '../../lib/filamentSearch'

/**
 * #6950 — the files and links of any entity (`kind:ref`, such as `spool:<id>`):
 * `backend/scadbuddy/api/entity_attachments.py` and `entity_links.py`.
 */

type Schemas = components['schemas']
export type EntityAttachment = Schemas['EntityAttachment']
export type EntityAttachmentPatch = Schemas['EntityAttachmentPatch']
export type EntityLink = Schemas['EntityLink']
export type EntityLinkInput = Schemas['LinkInput']
export type EntityLinkPatch = Schemas['LinkPatch']
export type EntityProperty = Schemas['EntityProperty']
export type PropertyValue = EntityProperty['value']
export type KindPropertySchema = Schemas['KindPropertySchema']

/** The kinds whose files are written on the order routes (409 `use_order_route`). */
export const ORDER_KINDS = new Set(['order', 'order_item', 'order_item_fulfilment', 'order_item_revision'])
/** A print archive's files are Bambuddy's: listed, never written or served here. */
export const PRINT_ARCHIVE = 'print_archive'

/** The entity's kind and ref, split at the first colon. */
export function entityParts(entity: string): { kind: string; ref: string } {
  const at = entity.indexOf(':')
  return at < 0 ? { kind: entity, ref: '' } : { kind: entity.slice(0, at), ref: entity.slice(at + 1) }
}

const base = (entity: string) => `/entities/${encodeURIComponent(entity)}`

export const entityApi = {
  attachments: (entity: string) =>
    request<{ attachments: EntityAttachment[] }>(`${base(entity)}/attachments`).then((body) => body.attachments),
  /** An upload is a command: followed through its operation to the attachment. */
  addAttachment: (entity: string, file: File, fields: { role?: string; caption?: string }, onProgress?: (fraction: number) => void) =>
    uploadForm<EntityAttachment>(
      `${base(entity)}/attachments`,
      () => {
        const body = new FormData()
        body.append('file', file)
        if (fields.role) body.append('role', fields.role)
        if (fields.caption) body.append('caption', fields.caption)
        return body
      },
      onProgress,
    ),
  updateAttachment: (entity: string, id: string, body: EntityAttachmentPatch) =>
    request<EntityAttachment>(`${base(entity)}/attachments/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteAttachment: (entity: string, id: string) =>
    request<void>(`${base(entity)}/attachments/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  /** The file itself: inline for a photo, video or PDF; with `download`, always a download. */
  attachmentUrl: (entity: string, id: string, download = false) =>
    `${API_BASE}${base(entity)}/attachments/${encodeURIComponent(id)}/content${download ? '?download=true' : ''}`,
  /** A PNG, JPEG or WebP photo shrunk to a WebP; 404 for anything else. */
  attachmentThumbnailUrl: (entity: string, id: string) => `${API_BASE}${base(entity)}/attachments/${encodeURIComponent(id)}/thumbnail`,

  links: (entity: string) => request<{ links: EntityLink[] }>(`${base(entity)}/links`).then((body) => body.links),
  addLink: (entity: string, body: EntityLinkInput) => request<EntityLink>(`${base(entity)}/links`, { method: 'POST', body: JSON.stringify(body) }),
  updateLink: (entity: string, id: string, body: EntityLinkPatch) =>
    request<EntityLink>(`${base(entity)}/links/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteLink: (entity: string, id: string) => request<void>(`${base(entity)}/links/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /** #7026 — key-value properties: `backend/scadbuddy/api/entity_properties.py`. */
  properties: (entity: string) =>
    request<{ properties: EntityProperty[] }>(`${base(entity)}/properties`).then((body) => body.properties),
  /** A key set to `null` is removed; the whole patch applies or none of it. */
  setProperties: (entity: string, set: Record<string, PropertyValue | null>) =>
    request<{ properties: EntityProperty[] }>(`${base(entity)}/properties`, { method: 'PATCH', body: JSON.stringify({ set }) }).then(
      (body) => body.properties,
    ),
  /** Every entity of the kind that has properties: its ref to its keys and values. */
  kindProperties: (kind: string) =>
    request<{ properties: Record<string, Record<string, PropertyValue>> }>(`/entity-kinds/${encodeURIComponent(kind)}/properties`).then(
      (body) => body.properties,
    ),
  propertySchema: (kind: string) => request<KindPropertySchema>(`/entity-kinds/${encodeURIComponent(kind)}/property-schema`),
  /** #7067 — the schema that types one entity's properties: an item's is its item kind's, merged down the kind tree. */
  entityPropertySchema: (entity: string) => request<KindPropertySchema>(`${base(entity)}/property-schema`),
}

/** #7026 — every entity of `kind`'s property values as search text (`propertyTexts`), by ref. A failed read throws. */
export async function readKindPropertyTexts(kind: string): Promise<Map<string, string[]>> {
  const found = await entityApi.kindProperties(kind)
  return new Map(Object.entries(found).map(([ref, values]) => [ref, propertyTexts(values)]))
}

/** `readKindPropertyTexts`, but a failed read gives an empty map: the filament search still runs on everything else. */
export async function loadKindPropertyTexts(kind: string): Promise<Map<string, string[]>> {
  try {
    return await readKindPropertyTexts(kind)
  } catch {
    return new Map()
  }
}
