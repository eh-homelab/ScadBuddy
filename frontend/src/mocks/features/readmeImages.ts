/**
 * #5475 — `POST /models/{slug}/images`, an image for a template's README, as
 * `backend/scadbuddy/api/readme_images.py` takes it: typed by its first bytes (PNG,
 * JPEG, WebP or GIF), at most 10 MiB, its name made plain with the type's extension, a
 * taken name numbered, never written over, and one revision. `GET
 * /models/{slug}/images/{path}` serves what was stored here, so a README preview shows it.
 *
 * Under vitest a multipart body with a file part cannot reach a handler (jsdom's `File`
 * and Node's undici disagree, `handlers.test.ts`), so a unit test spies on
 * `api.addReadmeImage` and calls `storeMockReadmeImage`, the handler's own logic.
 */
import { HttpResponse, http } from 'msw'
import type { ReadmeImage } from '../../api/types'
import { mockModels, problem, refuseBuiltin, reviseModel } from '../handlers'

/** `MAX_MODEL_IMAGE_BYTES` in `api/lsp.py`. */
export const MAX_README_IMAGE_BYTES = 10 * 1024 * 1024

const stored = new Map<string, { bytes: Uint8Array; type: string }>()

export function reset(): void {
  stored.clear()
}

/** The type the bytes start with, as `library/readme_images.py` `sniff` reads them. */
function sniff(bytes: Uint8Array): [string, string] | null {
  const starts = (...head: number[]) => head.every((value, index) => bytes[index] === value)
  const text = (from: number, to: number) => String.fromCharCode(...bytes.slice(from, to))
  if (starts(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)) return ['png', 'image/png']
  if (starts(0xff, 0xd8, 0xff)) return ['jpg', 'image/jpeg']
  if (text(0, 4) === 'RIFF' && text(8, 12) === 'WEBP') return ['webp', 'image/webp']
  if (text(0, 6) === 'GIF87a' || text(0, 6) === 'GIF89a') return ['gif', 'image/gif']
  return null
}

/** `stored_name`: no folder, no extension, lower case, plain characters. */
function storedName(name: string, extension: string): string {
  const base = name.split(/[/\\]/).pop() ?? ''
  const stem = base.includes('.') ? base.slice(0, base.lastIndexOf('.')) : base
  const plain = stem
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^[-_]+|[-_]+$/g, '')
    .slice(0, 64)
    .replace(/^[-_]+|[-_]+$/g, '')
  return `${plain || 'image'}.${extension}`
}

/** What the route answers for these bytes: the stored image, or the refusal as `[status, detail]`. */
export function storeMockReadmeImage(slug: string, fileName: string, bytes: Uint8Array): ReadmeImage | [number, string] {
  if (slug.startsWith('builtin:')) return [403, `'${slug}' is a built-in template and is read-only`]
  if (!mockModels().some((m) => m.slug === slug)) return [404, `no model named '${slug}'`]
  if (bytes.length > MAX_README_IMAGE_BYTES) {
    return [422, `the image is too large: an image is at most ${MAX_README_IMAGE_BYTES} bytes (10 MiB)`]
  }
  const type = sniff(bytes)
  if (!type) return [422, 'the file is not a PNG, JPEG, WebP or GIF image']
  const name = storedName(fileName, type[0])
  const dot = name.lastIndexOf('.')
  let path = `images/${name}`
  for (let number = 2; stored.has(`${slug}/${path}`); number += 1) {
    path = `images/${name.slice(0, dot)}-${number}${name.slice(dot)}`
  }
  const model = reviseModel(slug, `Add an image to the ${slug} README`, [{ status: 'A', path }], {})
  if (!model) return [404, `no model named '${slug}'`]
  stored.set(`${slug}/${path}`, { bytes, type: type[1] })
  return { path, model }
}

export const handlers = [
  http.post('/api/v1/models/:slug/images', async ({ params, request }) => {
    const slug = String(params['slug'])
    const refused = refuseBuiltin(slug)
    if (refused) return refused
    const upload = (await request.formData()).get('file')
    if (upload === null || typeof upload === 'string') {
      return problem(422, 'Unprocessable Content', 'the upload needs a file part')
    }
    const answer = storeMockReadmeImage(slug, upload.name, new Uint8Array(await upload.arrayBuffer()))
    if (Array.isArray(answer)) return problem(answer[0], answer[0] === 422 ? 'Unprocessable Content' : 'Error', answer[1])
    return HttpResponse.json(answer)
  }),

  http.get('/api/v1/models/:slug/images/*', ({ params }) => {
    const rest = params[0]
    const path = Array.isArray(rest) ? rest.join('/') : String(rest)
    const image = stored.get(`${String(params['slug'])}/${path}`)
    if (!image) return problem(404, 'Not Found', `no file '${path}'`)
    return new HttpResponse(image.bytes.slice().buffer, { headers: { 'Content-Type': image.type } })
  }),
]
