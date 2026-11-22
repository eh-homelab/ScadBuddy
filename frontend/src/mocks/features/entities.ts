import { HttpResponse, http } from 'msw'
import type { EntityAttachment, EntityLink, EntityLinkInput, EntityLinkPatch, EntityAttachmentPatch } from '../../components/entities/api'
import { ORDER_KINDS, PRINT_ARCHIVE, entityParts } from '../../components/entities/api'
import { THUMBNAILED } from '../../components/entities/attachmentMedia'
import { parts, sha256, sniff } from './orders/attachments'
import { BROWSER, newId, now, problem, state as orders } from './orders/state'

/**
 * #6950 — any entity's files and links (`backend/scadbuddy/api/entity_attachments.py`,
 * `entity_links.py`). An order's entities list the order mock's files and refuse writes
 * (`use_order_route`); a print archive's files are seeded, read only, and never served.
 * The mock keeps no entity table: every `kind:ref` exists.
 */

const PROBLEMS = 'https://scadbuddy.dev/problems/entities/'
const ROLE = /^[a-z_]{1,32}$/
const KIND = /^[a-z_]{1,32}$/

function fresh() {
  return {
    attachments: new Map<string, EntityAttachment[]>(),
    files: new Map<string, Blob>(),
    links: new Map<string, EntityLink[]>(),
    /** Each upload's multipart fields, as the route received them. */
    uploads: [] as { entity: string; filename: string; role: string; caption: string }[],
    /** The next upload's 413 detail, once. */
    refuseNextUpload: null as string | null,
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
]
