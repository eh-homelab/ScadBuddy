import { createHash, randomBytes } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { type Principal, tiersUpTo } from '../src/auth/principal.js'
import { TabHub, type TabConnection } from '../src/bridge/hub.js'
import { type AgentFrame, IMAGE_CHUNK_MAX } from '../src/bridge/protocol.js'
import { MEMORY_TAB_IMAGES_MAX, MemoryTabImageStore, TAB_IMAGE_TTL_MS, type TabImageStore } from '../src/bridge/tabImages.js'
import { sessionImage } from '../src/sessions/blobs.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type ToolContext } from '../src/tools/registry.js'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import { services } from './helpers/mcp.js'

// browser_screenshot (#254): the tab sends its image as an `image` frame and binary
// frames (bridge/protocol.ts), the hub checks, names (sha256) and stores it before the
// `result` that holds it resolves the call, naming it there (bridge/hub.ts), and the tool
// takes it by name as MCP image content. No image crosses a JSON frame as base64 (#2479).
// test/bridge.e2e.test.ts runs the tab's own link.ts against the real socket.

const TAB = 'tab-aaaaaaaaaaaaaaaaaaaaaa'
const browser: Principal = { id: 'browser', kind: 'browser', tiers: tiersUpTo('outward') }
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
const screenshot = ALL_TOOLS.find((t) => t.name === 'browser_screenshot')!

/** A PNG-signed run of bytes, `size` long, and its bridge name. */
function png(size: number): { bytes: Buffer; name: string } {
  const bytes = Buffer.concat([PNG_SIGNATURE, randomBytes(size - PNG_SIGNATURE.length)])
  return { bytes, name: `${createHash('sha256').update(bytes).digest('hex')}.png` }
}

type Sent = { header?: Record<string, unknown>; chunks?: Buffer[]; result: unknown }

/** A tab on `hub` that answers each call with what `answer` gives, frame by frame. */
async function tab(hub: TabHub, answer: (id: string) => Sent) {
  const frames: AgentFrame[] = []
  const conn: TabConnection = hub.open((frame) => {
    frames.push(frame)
    if (frame.type !== 'call') return
    const sent = answer(frame.id)
    queueMicrotask(() => {
      if (sent.header) void conn.receive(JSON.stringify({ v: 1, type: 'image', id: frame.id, ...sent.header }))
      for (const chunk of sent.chunks ?? []) void conn.receive(chunk)
      void conn.receive(JSON.stringify({ v: 1, type: 'result', id: frame.id, outcome: sent.result }))
    })
  })
  await conn.receive(JSON.stringify({ v: 1, type: 'hello', tabId: TAB, route: '/', live: ['screenshot'] }))
  hub.pairSession('s1', TAB)
  return { conn, errors: () => frames.flatMap((f) => (f.type === 'error' ? [f.message] : [])) }
}

/** The last text of a result: after the envelope's preamble and the image, the rest of the answer. */
function lastJson(result: { content: { type: string; text?: string }[] }): unknown {
  const texts = result.content.filter((c) => c.type === 'text')
  return JSON.parse(unwrapUntrusted(texts.at(-1)!.text!))
}

function ctx(hub: TabHub): ToolContext {
  return { ...services({ browser: hub.forSession('s1') }), principal: browser, progress: async () => {}, signal: new AbortController().signal }
}

/** The answer a tab gives for `image`, its bytes split into frames of `chunk`. */
function sending(image: { bytes: Buffer; name: string }, chunk = IMAGE_CHUNK_MAX): Sent {
  const chunks: Buffer[] = []
  for (let at = 0; at < image.bytes.length; at += chunk) chunks.push(image.bytes.subarray(at, at + chunk))
  return {
    header: { mediaType: 'image/png', size: image.bytes.length },
    chunks,
    result: { ok: true, result: { image: { mediaType: 'image/png' }, width: 800, height: 600, hidden: 1 } },
  }
}

describe('browser_screenshot (#254)', () => {
  it('returns the image the tab sent in binary frames, and the rest of its answer as JSON', async () => {
    const hub = new TabHub()
    const image = png(IMAGE_CHUNK_MAX * 2 + 100)
    const t = await tab(hub, () => sending(image))
    const result = await runTool(screenshot, {}, ctx(hub))
    expect(result.isError).toBeFalsy()
    const shown = result.content.find((c) => c.type === 'image') as { data: string; mimeType: string }
    expect(shown.mimeType).toBe('image/png')
    expect(Buffer.from(shown.data, 'base64').equals(image.bytes)).toBe(true)
    expect(lastJson(result)).toEqual({ width: 800, height: 600, hidden: 1 })
    expect(t.errors()).toEqual([])
    // Named here by its sha256, and taken by the tool's read (bridge/tabImages.ts).
    expect(await hub.image(image.name)).toBeUndefined()
  })

  it('stores the image before the answer resolves, so a store that fails fails the call', async () => {
    const failing: TabImageStore = {
      put: () => Promise.reject(new Error('pool down')),
      take: () => Promise.resolve(undefined),
    }
    const logged: string[] = []
    const hub = new TabHub({ images: failing, log: (m) => logged.push(m) })
    const image = png(1000)
    await tab(hub, () => sending(image))
    const result = await runTool(screenshot, {}, ctx(hub))
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result.content)).toMatch(/could not be stored; try again/)
    expect(logged.join('\n')).toMatch(/pool down/)
  })

  it('refuses bytes that are not of the type their image frame said, and fails the call that holds them', async () => {
    const hub = new TabHub()
    const image = png(1000)
    const t = await tab(hub, () => ({ ...sending(image), header: { mediaType: 'image/jpeg', size: image.bytes.length } }))
    const result = await runTool(screenshot, {}, ctx(hub))
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result.content)).toMatch(/holds an image whose bytes did not arrive/)
    expect(t.errors()).toEqual(['the image is not of the type its image frame said; dropped'])
    expect(await hub.image(image.name)).toBeUndefined()
  })

  it('names the image itself: a name the tab puts in its answer is replaced', async () => {
    const stored: string[] = []
    const memory = new MemoryTabImageStore()
    const hub = new TabHub({ images: { put: (i) => (stored.push(i.name), memory.put(i)), take: (n) => memory.take(n) } })
    const image = png(1000)
    const sent = sending(image)
    await tab(hub, () => ({ ...sent, result: { ok: true, result: { image: { name: png(10).name, mediaType: 'image/png' } } } }))
    const result = await runTool(screenshot, {}, ctx(hub))
    expect(result.isError).toBeFalsy()
    expect(stored).toEqual([image.name])
  })

  it('refuses bytes past the size the image frame said', async () => {
    const hub = new TabHub()
    const image = png(1000)
    const t = await tab(hub, () => ({ ...sending(image), header: { mediaType: 'image/png', size: 500 } }))
    const result = await runTool(screenshot, {}, ctx(hub))
    expect(result.isError).toBe(true)
    expect(t.errors()).toEqual(['the image is larger than its image frame said; dropped'])
  })

  it('refuses bytes with no image frame, and an image frame for no waiting call', async () => {
    const hub = new TabHub()
    const { conn, errors } = await tab(hub, () => ({ result: { ok: true, result: {} } }))
    await conn.receive(PNG_SIGNATURE)
    await conn.receive(JSON.stringify({ v: 1, type: 'image', id: 'nobody', mediaType: 'image/png', size: 10 }))
    await conn.receive({ not: 'bytes' })
    expect(errors()).toEqual([
      'image bytes arrived with no image frame before them',
      'no call nobody is waiting',
      'frames must be JSON text or image bytes',
    ])
  })

  it('answers an answer with no image as an error, and an image no longer held as one to take again', async () => {
    const hub = new TabHub()
    await tab(hub, () => ({ result: { ok: true, result: { width: 1 } } }))
    expect(JSON.stringify((await runTool(screenshot, {}, ctx(hub))).content)).toMatch(/without an image/)

    const forgetful: TabImageStore = { put: () => Promise.resolve(), take: () => Promise.resolve(undefined) }
    const later = new TabHub({ images: forgetful })
    await tab(later, () => sending(png(100)))
    const result = await runTool(screenshot, {}, ctx(later))
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result.content)).toMatch(/no longer held; take it again/)
  })
})

describe('MemoryTabImageStore', () => {
  it(`keeps an image for ${TAB_IMAGE_TTL_MS / 60_000} minutes and at most ${MEMORY_TAB_IMAGES_MAX} at once`, async () => {
    let now = 0
    const store = new MemoryTabImageStore(() => now)
    const first = sessionImage('image/png', png(100).bytes)
    await store.put(first)
    expect((await store.take(first.name))?.bytes.equals(first.bytes)).toBe(true)
    // Taken: a second read finds nothing.
    expect(await store.take(first.name)).toBeUndefined()
    await store.put(first)
    now = TAB_IMAGE_TTL_MS + 1
    expect(await store.take(first.name)).toBeUndefined()

    const many = Array.from({ length: MEMORY_TAB_IMAGES_MAX + 1 }, () => sessionImage('image/png', png(100).bytes))
    for (const image of many) await store.put(image)
    expect(await store.take(many[0]!.name)).toBeUndefined()
    expect(await store.take(many.at(-1)!.name)).toBeDefined()
  })
})
