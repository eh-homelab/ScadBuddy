import {
  IMAGE_DATA_MAX,
  IMAGE_EDGE,
  PREVIEW_EDGE,
  composerImages,
  prepareImage,
  type ImageCodec,
} from './images'

// #1866 — what the composer makes of a pasted, dropped or picked image.

const PNG_HEAD = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
const JPEG_HEAD = [0xff, 0xd8, 0xff, 0xe0]

function bytes(head: number[], size: number): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(new ArrayBuffer(size))
  out.set(head)
  return out
}

/** A codec that records what it was asked to draw, and draws a JPEG or PNG of `size(w, h)` bytes. */
function fakeCodec(width: number, height: number, size = (w: number, h: number, _type: string) => w * h) {
  const drawn: { width: number; height: number; type: string }[] = []
  const codec: ImageCodec = {
    open: () =>
      Promise.resolve({
        width,
        height,
        encode: (w, h, type) => {
          drawn.push({ width: w, height: h, type })
          return Promise.resolve(new Blob([bytes(type === 'image/png' ? PNG_HEAD : JPEG_HEAD, size(w, h, type))], { type }))
        },
        close: () => {},
      }),
  }
  return { codec, drawn }
}

const file = (head: number[], size: number, type: string, name = 'shot.png') => new File([bytes(head, size)], name, { type })

describe('prepareImage (#1866)', () => {
  it('sends a small image as it is, with a JPEG preview at most PREVIEW_EDGE on a side', async () => {
    const { codec, drawn } = fakeCodec(800, 400)
    const image = await prepareImage(file(PNG_HEAD, 1000, 'image/png'), codec)
    expect(image.mediaType).toBe('image/png')
    expect(atob(image.data).length).toBe(1000)
    expect(image.preview.mediaType).toBe('image/jpeg')
    expect(drawn).toEqual([{ width: PREVIEW_EDGE, height: PREVIEW_EDGE / 2, type: 'image/jpeg' }])
  })

  it('scales an image larger than IMAGE_EDGE down, keeping its type', async () => {
    const { codec, drawn } = fakeCodec(4000, 2000, () => 5000)
    const image = await prepareImage(file(PNG_HEAD, 9000, 'image/png'), codec)
    expect(drawn[0]).toEqual({ width: IMAGE_EDGE, height: IMAGE_EDGE / 2, type: 'image/png' })
    expect(image.mediaType).toBe('image/png')
    expect(atob(image.data).length).toBe(5000)
  })

  it('falls back to a JPEG when the scaled image is still too large', async () => {
    const tooBig = (IMAGE_DATA_MAX / 4) * 3 + 10
    const { codec, drawn } = fakeCodec(4000, 2000, (_w, _h, type) => (type === 'image/png' ? tooBig : 3000))
    const image = await prepareImage(file(PNG_HEAD, tooBig, 'image/png'), codec)
    expect(drawn.map((d) => d.type)).toEqual(['image/png', 'image/jpeg', 'image/jpeg'])
    expect(image.mediaType).toBe('image/jpeg')
  })

  it('refuses a type the model cannot read, before decoding it', async () => {
    const { codec, drawn } = fakeCodec(10, 10)
    await expect(prepareImage(new File(['<svg/>'], 'logo.svg', { type: 'image/svg+xml' }), codec)).rejects.toThrow(
      'logo.svg is not a PNG, JPEG, GIF or WebP image.',
    )
    expect(drawn).toEqual([])
  })

  it('says so when an image cannot be read', async () => {
    const codec: ImageCodec = { open: () => Promise.reject(new Error('decode failed')) }
    await expect(prepareImage(file(PNG_HEAD, 10, 'image/png', 'broken.png'), codec)).rejects.toThrow(
      'broken.png could not be read as an image.',
    )
  })
})

describe('composerImages (#1866)', () => {
  it('takes the images a paste or drop carries, and leaves text and other files', () => {
    const png = file(PNG_HEAD, 10, 'image/png')
    const data = {
      files: [png, new File(['hi'], 'notes.txt', { type: 'text/plain' }), new File(['v'], 'clip.mp4', { type: 'video/mp4' })],
      items: [],
    } as unknown as DataTransfer
    expect(composerImages(data)).toEqual([png])
    expect(composerImages({ files: [], items: [] } as unknown as DataTransfer)).toEqual([])
    expect(composerImages(null)).toEqual([])
  })
})
