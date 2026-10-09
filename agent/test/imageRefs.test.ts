import { describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { IMAGE_DATA_MAX, IMAGES_MAX, PREVIEW_DATA_MAX } from '../src/sessions/images.js'
import { ImageRefsSchema, NO_PREVIEW, resolveImageRefs } from '../src/tools/imageRefs.js'
import { ToolError } from '../src/tools/registry.js'
import { BACKEND } from './helpers/mcp.js'

// #1894: sessions_send takes images by reference; the agent fetches each from
// the backend when the turn starts and checks it as it checks the panel's.

const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4])
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 5, 6, 7, 8])
const WEBP = Buffer.concat([Buffer.from('RIFF'), Buffer.from([0, 0, 0, 0]), Buffer.from('WEBPVP8 ')])
const SVG = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>')

function padded(head: Buffer, size: number): Buffer {
  const out = Buffer.alloc(size)
  head.copy(out)
  return out
}

function backend(routes: Record<string, { body: Buffer; type: string } | number>) {
  const seen: string[] = []
  const client = createBackendClient(BACKEND, (request) => {
    const url = new URL((request as Request).url)
    const key = decodeURIComponent(url.pathname) + url.search
    seen.push(key)
    const route = routes[key]
    if (route === undefined) return Promise.resolve(Response.json({ detail: `nothing at ${key}` }, { status: 404 }))
    if (typeof route === 'number') return Promise.resolve(Response.json({ detail: 'no' }, { status: route }))
    return Promise.resolve(new Response(new Uint8Array(route.body), { headers: { 'content-type': route.type } }))
  })
  return { client, seen }
}

const OUT = 'a'.repeat(32)
const ASSET = 'c'.repeat(64)

describe('image references (#1894)', () => {
  it('resolve each kind from its backend route, small stills as their own preview', async () => {
    const { client, seen } = backend({
      [`/api/v1/outputs/${OUT}/thumbnail`]: { body: PNG, type: 'image/png' },
      [`/api/v1/outputs/${OUT}/plates/2/thumbnail`]: { body: PNG, type: 'image/png' },
      [`/api/v1/models/keychain/thumbnail`]: { body: JPEG, type: 'image/jpeg' },
      [`/api/v1/models/keychain/assets/${ASSET}/content`]: { body: PNG, type: 'image/png' },
      [`/api/v1/prints/7/thumbnail`]: { body: PNG, type: 'image/png' },
    })
    const refs = ImageRefsSchema.parse([
      { kind: 'output_thumbnail', output_id: OUT },
      { kind: 'output_thumbnail', output_id: OUT, plate: 2 },
      { kind: 'model_thumbnail', slug: 'keychain' },
      { kind: 'asset', slug: 'keychain', asset_id: ASSET },
    ])
    const images = await resolveImageRefs(refs, client)
    expect(images.map((i) => i.mediaType)).toEqual(['image/png', 'image/png', 'image/jpeg', 'image/png'])
    expect(images[0]!.data).toBe(PNG.toString('base64'))
    expect(images[0]!.preview).toEqual({ mediaType: 'image/png', data: PNG.toString('base64') })
    expect(images[2]!.preview).toEqual({ mediaType: 'image/jpeg', data: JPEG.toString('base64') })
    expect(seen).toHaveLength(4)

    const [print] = await resolveImageRefs(ImageRefsSchema.parse([{ kind: 'print_thumbnail', archive_id: 7 }]), client)
    expect(print!.mediaType).toBe('image/png')
  })

  it("previews a view by the backend's own small drawing of it", async () => {
    const big = padded(PNG, (PREVIEW_DATA_MAX / 4) * 3 + 100)
    const { client, seen } = backend({
      [`/api/v1/outputs/${OUT}/views/top.png?size=1024`]: { body: big, type: 'image/png' },
      [`/api/v1/outputs/${OUT}/views/top.png?size=128`]: { body: PNG, type: 'image/png' },
    })
    const [image] = await resolveImageRefs(
      ImageRefsSchema.parse([{ kind: 'output_view', output_id: OUT, view: 'top', size: 1024 }]),
      client,
    )
    expect(image!.data).toBe(big.toString('base64'))
    expect(image!.preview.data).toBe(PNG.toString('base64'))
    expect(seen).toEqual([`/api/v1/outputs/${OUT}/views/top.png?size=1024`, `/api/v1/outputs/${OUT}/views/top.png?size=128`])
  })

  it('previews a media item by its thumbnail', async () => {
    const big = padded(JPEG, (PREVIEW_DATA_MAX / 4) * 3 + 100)
    const { client } = backend({
      ['/api/v1/models/keychain/media/photo-1']: { body: big, type: 'image/jpeg' },
      ['/api/v1/models/keychain/media/photo-1/thumbnail']: { body: WEBP, type: 'image/webp' },
    })
    const [image] = await resolveImageRefs(
      ImageRefsSchema.parse([{ kind: 'model_media', slug: 'keychain', item_id: 'photo-1' }]),
      client,
    )
    expect(image!.mediaType).toBe('image/jpeg')
    expect(image!.preview).toEqual({ mediaType: 'image/webp', data: WEBP.toString('base64') })
  })

  it('gives a large image with no small copy the placeholder preview', async () => {
    const big = padded(PNG, (PREVIEW_DATA_MAX / 4) * 3 + 100)
    const { client } = backend({ [`/api/v1/outputs/${OUT}/thumbnail`]: { body: big, type: 'image/png' } })
    const [image] = await resolveImageRefs(ImageRefsSchema.parse([{ kind: 'output_thumbnail', output_id: OUT }]), client)
    expect(image!.preview).toEqual(NO_PREVIEW)
  })

  it('refuses what is not one of the four image types, by its bytes, without quoting them', async () => {
    const { client } = backend({
      [`/api/v1/models/keychain/assets/${ASSET}/content`]: { body: SVG, type: 'image/svg+xml' },
      [`/api/v1/outputs/${OUT}/thumbnail`]: { body: Buffer.from('not a png at all'), type: 'image/png' },
    })
    const svg = resolveImageRefs(ImageRefsSchema.parse([{ kind: 'asset', slug: 'keychain', asset_id: ASSET }]), client)
    await expect(svg).rejects.toThrow(ToolError)
    await expect(svg).rejects.toThrow(/images\[0\].*not a PNG, JPEG, GIF or WebP/)
    const liar = resolveImageRefs(ImageRefsSchema.parse([{ kind: 'output_thumbnail', output_id: OUT }]), client)
    await expect(liar).rejects.toThrow(/images\[0\]/)
    await expect(liar).rejects.not.toThrow(/not a png at all|bm90IGEg/)
  })

  it('refuses an image over the per-image cap without reading it whole', async () => {
    const huge = padded(PNG, (IMAGE_DATA_MAX / 4) * 3 + 3)
    const { client } = backend({ [`/api/v1/outputs/${OUT}/thumbnail`]: { body: huge, type: 'image/png' } })
    await expect(
      resolveImageRefs(ImageRefsSchema.parse([{ kind: 'output_thumbnail', output_id: OUT }]), client),
    ).rejects.toThrow(/images\[0\].*larger than/)
  })

  it('names the reference the backend could not find', async () => {
    const { client } = backend({})
    await expect(
      resolveImageRefs(ImageRefsSchema.parse([{ kind: 'model_thumbnail', slug: 'nope' }]), client),
    ).rejects.toThrow(/images\[0\].*HTTP 404/)
  })

  it('takes at most IMAGES_MAX references, and no inline bytes', () => {
    const one = { kind: 'model_thumbnail', slug: 'keychain' }
    expect(() => ImageRefsSchema.parse(Array(IMAGES_MAX + 1).fill(one))).toThrow()
    expect(() => ImageRefsSchema.parse([])).toThrow()
    expect(() => ImageRefsSchema.parse([{ mediaType: 'image/png', data: PNG.toString('base64') }])).toThrow()
    expect(() => ImageRefsSchema.parse([{ ...one, data: 'AAAA' }])).toThrow()
    expect(() => ImageRefsSchema.parse([{ kind: 'url', url: 'https://example.com/a.png' }])).toThrow()
  })
})
