import { describe, expect, it } from 'vitest'
import { storeMockReadmeImage } from './readmeImages'

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3])
const GIF = new TextEncoder().encode('GIF89a....')

describe('mock POST /models/:slug/images (#5475), as readme_images.py takes it', () => {
  it('stores an image under a plain name, numbers a taken one, and serves it', async () => {
    const first = storeMockReadmeImage('name-keychain', '../Front View.PNG', PNG)
    const second = storeMockReadmeImage('name-keychain', 'front view.png', PNG)
    const typed = storeMockReadmeImage('name-keychain', 'front view.png', GIF)

    expect(first).toMatchObject({ path: 'images/front-view.png', model: { slug: 'name-keychain' } })
    expect(second).toMatchObject({ path: 'images/front-view-2.png' })
    // The extension is the bytes' type, never the name's.
    expect(typed).toMatchObject({ path: 'images/front-view.gif' })
    const served = await fetch('/api/v1/models/name-keychain/images/images/front-view.png')
    expect(served.headers.get('Content-Type')).toBe('image/png')
    expect(new Uint8Array(await served.arrayBuffer())).toEqual(PNG)
  })

  it('refuses what is not an image, a built-in template and a missing model', () => {
    expect(storeMockReadmeImage('name-keychain', 'a.svg', new TextEncoder().encode('<svg/>'))).toEqual([
      422,
      'the file is not a PNG, JPEG, WebP or GIF image',
    ])
    expect(storeMockReadmeImage('builtin:keychain', 'a.png', PNG)).toEqual([403, expect.stringContaining('read-only')])
    expect(storeMockReadmeImage('no-such-model', 'a.png', PNG)).toEqual([404, "no model named 'no-such-model'"])
  })
})
