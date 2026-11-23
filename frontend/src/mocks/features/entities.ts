import { HttpResponse, http } from 'msw'
import type { EntityAttachment, EntityLink, EntityLinkInput, EntityLinkPatch, EntityAttachmentPatch, EntityProperty, PropertyValue } from '../../components/entities/api'
import { ORDER_KINDS, PRINT_ARCHIVE, entityParts } from '../../components/entities/api'
import { THUMBNAILED } from '../../components/entities/attachmentMedia'
import { parts, sha256, sniff } from './orders/attachments'
import { BROWSER, newId, now, problem, state as orders } from './orders/state'

/**
 * #6950 — any entity's files and links (`backend/scadbuddy/api/entity_attachments.py`,
 * `entity_links.py`), and their properties (#7026, `entity_properties.py`). An order's entities list the order mock's files and refuse writes
 * (`use_order_route`); a print archive's files are seeded, read only, and never served.
 * The mock keeps no entity table: every `kind:ref` exists.
 */

const PROBLEMS = 'https://scadbuddy.dev/problems/entities/'
const ROLE = /^[a-z_]{1,32}$/
const KIND = /^[a-z_]{1,32}$/
const PROPERTY_KEY = /^[a-z][a-z0-9_]{0,63}$/
const MAX_KEYS = 200

/** The seeded schemas of `20261123T0017Z_entity_properties.sql`. */
function seededSchemas(): Record<string, Record<string, unknown>> {
  return {
    spool_type: {
      type: 'object',
      properties: {
        mpn: { type: 'string', description: 'Manufacturer part number' },
        gtin: { type: 'array', items: { type: 'string' }, description: 'GTIN / EAN / UPC barcodes (spool and refill)' },
        asin: { type: 'array', items: { type: 'string' }, description: 'Amazon ASINs as `marketplace:ASIN`, such as `us:B0CXYZ1234`' },
        manufacturer_url: { type: 'string', description: "The manufacturer's product page" },
        empty_spool_weight_g: { type: 'number', minimum: 0, description: 'Weight of the empty spool in grams' },
        td: { type: 'number', minimum: 0, description: 'Transmission distance (HueForge TD)' },
      },
    },
    spool: { type: 'object', properties: { lot_number: { type: 'string', description: 'Lot or batch number printed on the label' } } },
  }
}

function fresh() {
  return {
    attachments: new Map<string, EntityAttachment[]>(),
    files: new Map<string, Blob>(),
    links: new Map<string, EntityLink[]>(),
    /** Each upload's multipart fields, as the route received them. */
    uploads: [] as { entity: string; filename: string; role: string; caption: string }[],
    /** The next upload's 413 detail, once. */
    refuseNextUpload: null as string | null,
    /** Each entity's properties, by key. */
    properties: new Map<string, EntityProperty[]>(),
    /** Each PATCH's `set`, as the route received it. */
    propertyPatches: [] as { entity: string; set: Record<string, unknown> }[],
    schemas: seededSchemas(),
  }
}

export const entityMock = fresh()

export function reset(): void {
  Object.assign(entityMock, fresh())
}

const coded = (request: Request, status: number, code: string, detail: string) => problem(request, status, detail, PROBLEMS + code, { code })

function useOrderRoute(request: Request, kind: string) {
  return coded(request, 409, 'use_order_route', `an order's files are changed on its own routes (\`/orders/{order_id}/…/attachments\`), where the order's rules apply; '${kind}' is one of them`)
}

/** A seeded file, as an upload (or Bambuddy's print archive) makes one. */
export function seedEntityAttachment(entity: string, fields: Partial<EntityAttachment> = {}): EntityAttachment {
  const attachment: EntityAttachment = {
    id: newId('eatt'),
    entity,
    kind: 'image',
    role: 'other',
    filename: 'file.png',
    media_type: 'image/png',
    size_bytes: 12,
    sha256: 'a'.repeat(64),
    caption: '',
    created_at: now(),
    created_by: BROWSER,
    source: 'store',
    ...fields,
  }
  entityMock.attachments.set(entity, [...(entityMock.attachments.get(entity) ?? []), attachment])
  return attachment
}

export function seedEntityLink(entity: string, fields: Partial<EntityLink> & { url: string }): EntityLink {
  const list = entityMock.links.get(entity) ?? []
  const link: EntityLink = {
    id: crypto.randomUUID(),
    entity,
    kind: 'other',
    title: '',
    note: '',
    position: list.length === 0 ? 0 : Math.max(...list.map((l) => l.position)) + 1,
    source: 'manual',
    created_at: now(),
    created_by: BROWSER,
    ...fields,
  }
  entityMock.links.set(entity, [...list, link])
  return link
}

export function seedEntityProperties(entity: string, values: Record<string, PropertyValue>): void {
  const list = (entityMock.properties.get(entity) ?? []).filter((p) => !(p.key in values))
  for (const [key, value] of Object.entries(values)) list.push({ key, value, source: 'manual', updated_at: now(), updated_by: BROWSER })
  entityMock.properties.set(entity, list.sort((a, b) => a.key.localeCompare(b.key)))
}

const PROPERTY_TYPES: Record<string, (value: unknown) => boolean> = {
  string: (value) => typeof value === 'string',
  number: (value) => typeof value === 'number',
  integer: (value) => Number.isInteger(value),
  boolean: (value) => typeof value === 'boolean',
  array: (value) => Array.isArray(value),
}

/** A string's length as Python counts it: in code points, not UTF-16 units. */
const characters = (text: string) => [...text].length

/** `entities/properties.py`'s checks, in short: the reason a value is refused, else null. */
function propertyRefusal(kind: string, key: string, value: unknown): string | null {
  if (!PROPERTY_KEY.test(key)) return 'a key is `^[a-z][a-z0-9_]{0,63}$`'
  // Postgres's jsonb holds no U+0000, so the backend refuses it in any string.
  if ([value].flat().some((text) => typeof text === 'string' && text.includes('\u0000'))) return 'a string may not hold U+0000'
  const shaped =
    (typeof value === 'string' && characters(value) <= 2000) ||
    (typeof value === 'number' && Number.isFinite(value)) ||
    typeof value === 'boolean' ||
    (Array.isArray(value) && value.length <= 50 && value.every((item) => typeof item === 'string' && characters(item) <= 500))
  if (!shaped) return 'a value is a string (up to 2000 characters), a finite number, a boolean or a list of up to 50 strings of up to 500 characters'
  const known = (entityMock.schemas[kind]?.properties as Record<string, { type?: string | string[] }> | undefined)?.[key]
  const types = known?.type === undefined ? [] : ([] as string[]).concat(known.type).filter((type) => PROPERTY_TYPES[type])
  if (types.length && !types.some((type) => PROPERTY_TYPES[type]!(value))) {
    return `must be ${types.map((type) => (type === 'array' || type === 'integer' ? `an ${type}` : `a ${type}`)).join(' or ')}`
  }
  return null
}

function keysInUse(kind: string) {
  const counts = new Map<string, number>()
  for (const [entity, list] of entityMock.properties) {
    if (entityParts(entity).kind !== kind) continue
    for (const p of list) counts.set(p.key, (counts.get(p.key) ?? 0) + 1)
  }
  return [...counts].sort(([a], [b]) => a.localeCompare(b)).map(([key, count]) => ({ key, count }))
}

const noProperties = (request: Request, kind: string) => coded(request, 422, 'unknown_entity_kind', `no entity kind '${kind}'`)

/** `entities/links.py` `is_web_url`. */
function webUrl(url: string): boolean {
  try {
    const parsed = new URL(url)
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:') && parsed.hostname !== '' && url.length <= 2000
  } catch {
    return false
  }
}

const badUrl = (request: Request) => coded(request, 422, 'bad_link_url', 'a link is an `http` or `https` URL with a host, up to 2000 characters')

function listed(entity: string): EntityAttachment[] {
  const { kind } = entityParts(entity)
  if (ORDER_KINDS.has(kind)) {
    return orders.attachments
      .filter((a) => a.subject === entity)
      .map((a) => ({
        id: a.id,
        entity,
        kind: a.kind,
        role: a.role,
        filename: a.filename,
        media_type: a.media_type,
        size_bytes: a.size_bytes,
        sha256: a.sha256,
        caption: a.caption,
        created_at: a.created_at,
        created_by: a.created_by,
        source: 'store' as const,
      }))
  }
  return entityMock.attachments.get(entity) ?? []
}

function found(request: Request, entity: string, id: string): EntityAttachment | Response {
  const attachment = listed(entity).find((a) => a.id === id && a.source === 'store')
  return attachment ?? coded(request, 404, 'attachment_not_found', `${entity} has no attachment '${id}'`)
}

/** Links by position, then age, as `EntityLinkStore.list`. */
const ordered = (entity: string) =>
  [...(entityMock.links.get(entity) ?? [])].sort((a, b) => a.position - b.position || a.created_at.localeCompare(b.created_at))

/** Makes room at `position`, as the store's `position + 1` update. */
function shift(entity: string, position: number, except?: string) {
  for (const link of entityMock.links.get(entity) ?? []) if (link.position >= position && link.id !== except) link.position += 1
}

const refOf = (params: Record<string, unknown>) => decodeURIComponent(String(params.ref))


// The backend's keyword allowlist (entities/properties.py TOP_KEYWORDS, KEY_KEYWORDS):
// a schema documents keys and never refuses one.
const TOP_KEYWORDS = new Set(['type', 'properties', 'additionalProperties', '$schema', '$comment', 'title', 'description', 'examples'])
const KEY_KEYWORDS = new Set([
  'type', 'title', 'description', '$comment', 'default', 'examples', 'deprecated', 'readOnly', 'writeOnly', 'enum', 'const',
  'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf', 'minLength', 'maxLength', 'format', 'items',
  'minItems', 'maxItems', 'uniqueItems',
])
// ITEM_KEYWORDS, KEY_TYPES, ITEM_TYPES, DRAFT_2020_12: a list holds no list, and strings only.
const ITEM_KEYWORDS = new Set([...KEY_KEYWORDS].filter((k) => k !== 'items'))
const KEY_TYPES = new Set(['string', 'number', 'integer', 'boolean', 'array'])
const ITEM_TYPES = new Set(['string'])
const DRAFT_2020_12 = 'https://json-schema.org/draft/2020-12/schema'

/** `_check_key`'s keyword and type checks (not its enum, const and bounds checks). */
function keyProblem(sub: unknown, where: string, item: boolean): string | null {
  if (!sub || typeof sub !== 'object' || Array.isArray(sub)) return `the subschema ${where} is a JSON object`
  const node = sub as Record<string, unknown>
  for (const k of Object.keys(node)) if (!(item ? ITEM_KEYWORDS : KEY_KEYWORDS).has(k)) return `keyword '${k}' is not allowed ${where}`
  const types = item ? ITEM_TYPES : KEY_TYPES
  if ('type' in node) {
    const named = ([] as unknown[]).concat(node.type)
    if (!named.some((t) => t !== 'null') || !named.every((t) => t === 'null' || types.has(t as string))) {
      return `the type ${where} is one of ${[...types].sort().join(', ')}, or a list of them and "null"`
    }
  }
  if ('items' in node) return keyProblem(node.items, `in the items of ${where.replace(/^in /, '')}`, true)
  return null
}

function schemaProblem(schema: Record<string, unknown> | undefined): string | null {
  if (!schema || typeof schema !== 'object') return 'a schema is a JSON object'
  for (const k of Object.keys(schema)) if (!TOP_KEYWORDS.has(k)) return `keyword '${k}' is not allowed at the top level`
  if (schema.type !== 'object') return 'a schema\'s "type" is "object"'
  if ('additionalProperties' in schema && schema.additionalProperties !== true) return '"additionalProperties" may only be true'
  if ('$schema' in schema && schema.$schema !== DRAFT_2020_12) return `"$schema" may only be "${DRAFT_2020_12}"`
  for (const [key, sub] of Object.entries((schema.properties ?? {}) as Record<string, unknown>)) {
    const problem = keyProblem(sub, `in property '${key}'`, false)
    if (problem) return problem
  }
  return null
}

export const handlers = [
  http.get('/api/v1/entities/:ref/attachments', ({ params }) => HttpResponse.json({ attachments: listed(refOf(params)) })),
  http.post('/api/v1/entities/:ref/attachments', async ({ request, params }) => {
    const entity = refOf(params)
    const { kind } = entityParts(entity)
    if (ORDER_KINDS.has(kind)) return useOrderRoute(request, kind)
    if (kind === PRINT_ARCHIVE) return coded(request, 422, 'unknown_entity_kind', `no entity kind '${kind}'`)
    if (entityMock.refuseNextUpload !== null) {
      const detail = entityMock.refuseNextUpload
      entityMock.refuseNextUpload = null
      return problem(request, 413, detail)
    }
    const sent = await parts(request)
    const text = (name: string) => new TextDecoder().decode(sent.find((p) => p.name === name)?.bytes ?? new Uint8Array())
    const part = sent.find((p) => p.name === 'file')
    if (!part || part.bytes.length === 0) return problem(request, 422, 'the upload has no `file` part, or it is empty')
    const role = text('role').trim() || 'other'
    if (!ROLE.test(role)) return coded(request, 422, 'bad_role', 'a role is 1 to 32 lower-case letters or underscores, such as `receipt`')
    const filename = part.filename?.split(/[\\/]/).pop() || 'file'
    const caption = text('caption').trim()
    entityMock.uploads.push({ entity, filename, role, caption })
    const file = new Blob([part.bytes])
    const [sniffed, mediaType] = sniff(part.bytes.slice(0, 16), filename)
    const attachment = seedEntityAttachment(entity, { kind: sniffed, media_type: mediaType, role, filename, caption, size_bytes: file.size, sha256: await sha256(file) })
    entityMock.files.set(attachment.id, file)
    return HttpResponse.json(attachment)
  }),
  http.get('/api/v1/entities/:ref/attachments/:id/content', ({ request, params }) => {
    const attachment = found(request, refOf(params), String(params.id))
    if (attachment instanceof Response) return attachment
    const download = new URL(request.url).searchParams.get('download') === 'true'
    const file = entityMock.files.get(attachment.id) ?? orders.files[attachment.id] ?? new Blob([])
    return new HttpResponse(file, { headers: { 'Content-Type': download ? 'application/octet-stream' : attachment.media_type } })
  }),
  http.get('/api/v1/entities/:ref/attachments/:id/thumbnail', ({ request, params }) => {
    const attachment = found(request, refOf(params), String(params.id))
    if (attachment instanceof Response) return attachment
    if (!THUMBNAILED.has(attachment.media_type)) return problem(request, 404, 'only a PNG, JPEG or WebP photo has a thumbnail')
    const file = entityMock.files.get(attachment.id) ?? orders.files[attachment.id] ?? new Blob([])
    return new HttpResponse(file, { headers: { 'Content-Type': attachment.media_type } })
  }),
  http.patch('/api/v1/entities/:ref/attachments/:id', async ({ request, params }) => {
    const entity = refOf(params)
    const { kind } = entityParts(entity)
    if (ORDER_KINDS.has(kind)) return useOrderRoute(request, kind)
    const body = (await request.json()) as EntityAttachmentPatch
    if (body.role != null && !ROLE.test(body.role)) return coded(request, 422, 'bad_role', 'a role is 1 to 32 lower-case letters or underscores, such as `receipt`')
    const attachment = found(request, entity, String(params.id))
    if (attachment instanceof Response) return attachment
    if (body.role != null) attachment.role = body.role
    if (body.caption != null) attachment.caption = body.caption.trim()
    return HttpResponse.json(attachment)
  }),
  http.delete('/api/v1/entities/:ref/attachments/:id', ({ request, params }) => {
    const entity = refOf(params)
    const { kind } = entityParts(entity)
    if (ORDER_KINDS.has(kind)) return useOrderRoute(request, kind)
    const attachment = found(request, entity, String(params.id))
    if (attachment instanceof Response) return attachment
    entityMock.attachments.set(entity, listed(entity).filter((a) => a.id !== attachment.id))
    entityMock.files.delete(attachment.id)
    return new HttpResponse(null, { status: 204 })
  }),

  http.get('/api/v1/entities/:ref/links', ({ params }) => HttpResponse.json({ links: ordered(refOf(params)) })),
  http.post('/api/v1/entities/:ref/links', async ({ request, params }) => {
    const entity = refOf(params)
    const body = (await request.json()) as EntityLinkInput
    if (!webUrl(body.url)) return badUrl(request)
    if (body.kind != null && !KIND.test(body.kind)) return problem(request, 422, 'kind: not a link kind')
    if (body.position != null) shift(entity, body.position)
    const { position, ...rest } = body
    const link = seedEntityLink(entity, { ...rest, ...(position != null ? { position } : {}) })
    return HttpResponse.json(link)
  }),
  http.patch('/api/v1/entities/:ref/links/:id', async ({ request, params }) => {
    const entity = refOf(params)
    const body = (await request.json()) as EntityLinkPatch
    if (body.url != null && !webUrl(body.url)) return badUrl(request)
    const link = (entityMock.links.get(entity) ?? []).find((l) => l.id === params.id)
    if (!link) return coded(request, 404, 'link_not_found', `${entity} has no link '${String(params.id)}'`)
    if (body.position != null) shift(entity, body.position, link.id)
    Object.assign(link, Object.fromEntries(Object.entries(body).filter(([, value]) => value != null)), { source: 'manual' })
    return HttpResponse.json(link)
  }),
  http.delete('/api/v1/entities/:ref/links/:id', ({ request, params }) => {
    const entity = refOf(params)
    const list = entityMock.links.get(entity) ?? []
    if (!list.some((l) => l.id === params.id)) return coded(request, 404, 'link_not_found', `${entity} has no link '${String(params.id)}'`)
    entityMock.links.set(entity, list.filter((l) => l.id !== params.id))
    return new HttpResponse(null, { status: 204 })
  }),

  http.get('/api/v1/entities/:ref/properties', ({ request, params }) => {
    const entity = refOf(params)
    if (entityParts(entity).kind === PRINT_ARCHIVE) return noProperties(request, PRINT_ARCHIVE)
    return HttpResponse.json({ properties: entityMock.properties.get(entity) ?? [] })
  }),
  http.patch('/api/v1/entities/:ref/properties', async ({ request, params }) => {
    const entity = refOf(params)
    const { kind } = entityParts(entity)
    if (kind === PRINT_ARCHIVE) return noProperties(request, kind)
    const body = (await request.json()) as { set?: Record<string, unknown> }
    const set = body.set
    if (!set || typeof set !== 'object' || Object.keys(set).length === 0 || Object.keys(body).some((k) => k !== 'set')) {
      return problem(request, 422, 'set: the patch sets or removes at least one key')
    }
    if (Object.keys(set).length > MAX_KEYS) return problem(request, 422, `set: the patch names at most ${MAX_KEYS} keys`)
    entityMock.propertyPatches.push({ entity, set })
    for (const [key, value] of Object.entries(set)) {
      if (value === null) continue
      const reason = propertyRefusal(kind, key, value)
      if (reason) return coded(request, 422, 'bad_property_value', `property '${key}': ${reason}`)
    }
    const before = entityMock.properties.get(entity) ?? []
    const kept = before.filter((p) => !(p.key in set))
    for (const [key, value] of Object.entries(set)) {
      if (value !== null) kept.push({ key, value: value as PropertyValue, source: 'manual', updated_at: now(), updated_by: BROWSER })
    }
    // `MAX_KEYS`: an entity already past it (a printer merge) can still remove and change keys.
    if (kept.length > Math.max(MAX_KEYS, before.length)) {
      return coded(request, 422, 'bad_property_value', `property '*': an entity has at most ${MAX_KEYS} keys`)
    }
    entityMock.properties.set(entity, kept.sort((a, b) => a.key.localeCompare(b.key)))
    return HttpResponse.json({ properties: entityMock.properties.get(entity) })
  }),
  http.get('/api/v1/entity-kinds/:kind/properties', ({ request, params }) => {
    const kind = String(params.kind)
    if (kind === PRINT_ARCHIVE) return noProperties(request, kind)
    const found: Record<string, Record<string, PropertyValue>> = {}
    for (const [entity, list] of entityMock.properties) {
      const parts = entityParts(entity)
      if (parts.kind === kind && list.length) found[parts.ref] = Object.fromEntries(list.map((p) => [p.key, p.value]))
    }
    return HttpResponse.json({ properties: found })
  }),
  http.get('/api/v1/entity-kinds/:kind/property-schema', ({ request, params }) => {
    const kind = String(params.kind)
    if (kind === PRINT_ARCHIVE) return noProperties(request, kind)
    return HttpResponse.json({ kind, schema: entityMock.schemas[kind] ?? { type: 'object' }, keys_in_use: keysInUse(kind) })
  }),
  http.put('/api/v1/entity-kinds/:kind/property-schema', async ({ request, params }) => {
    const kind = String(params.kind)
    if (kind === PRINT_ARCHIVE) return noProperties(request, kind)
    const body = (await request.json()) as { schema?: Record<string, unknown> }
    const schema = body.schema
    const problem = schemaProblem(schema)
    if (problem || !schema) return coded(request, 422, 'bad_property_schema', problem ?? 'a property schema is an object schema')
    entityMock.schemas[kind] = schema
    return HttpResponse.json({ kind, schema, keys_in_use: keysInUse(kind) })
  }),
]
