import { describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { IMAGE_DATA_MAX } from '../src/sessions/images.js'
import { imageOrRef } from '../src/tools/imageResult.js'
import { DEFAULT_TOOL_IMAGE_INLINE_MAX } from '../src/tools/imageLimits.js'
import type { ToolContext } from '../src/tools/registry.js'
import { firstText, services } from './helpers/mcp.js'
import { createHash } from 'node:crypto'
import type { WorkspaceFiles as SessionFiles } from '../src/workspace/store.js'

// Tool images (#7745): inline up to the limit, a workspace pointer over it, a note for a type the model cannot view.

const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
const png = (size: number) => Buffer.concat([PNG, Buffer.alloc(size - PNG.length, 7)])
const REF = { name: 'plate 1.png', path: '/api/v1/prints/1/thumbnail' }

function ctx(limit?: number, withWorkspace = true): { ctx: ToolContext; ws: { written: Map<string, Buffer>; workspace: SessionFiles } } {
  const written = new Map<string, Buffer>()
  const ws = {
    written,
    workspace: {
      async write(_ws: unknown, path: string, bytes: Uint8Array, o?: { mediaType?: string }) {
        written.set(path, Buffer.from(bytes))
        return { path, mediaType: o?.mediaType ?? 'application/octet-stream', size: bytes.byteLength, sha256: createHash('sha256').update(bytes).digest('hex') }
      },
    } as unknown as SessionFiles,
  }
  return {
    ws,
    ctx: {
      ...services(),
      principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('read') },
      progress: async () => {},
      signal: new AbortController().signal,
      ...(limit === undefined ? {} : { toolImageInlineMaxBytes: async () => limit }),
      ...(withWorkspace ? { workspace: ws.workspace } : {}),
    } as ToolContext,
  }
}

describe('imageOrRef', () => {
  it('shows an image at the limit inline, typed by its signature', async () => {
    const { ctx: c } = ctx(1000)
    const result = await imageOrRef(png(1000), 'application/octet-stream', c, REF)
    expect(result.content).toEqual([{ type: 'image', mimeType: 'image/png', data: png(1000).toString('base64') }])
  })

  it('keeps an image over the limit in the workspace and answers a pointer', async () => {
    const { ctx: c, ws } = ctx(1000)
    const result = await imageOrRef(png(1001), 'image/png', c, REF)
    expect(result.content.some((x) => x.type === 'image')).toBe(false)
    const answer = firstText(result) as { workspace_file: { name: string; mediaType: string; size: number }; read_with: string }
    expect(answer.read_with).toBe('workspace_read')
    expect(answer.workspace_file).toMatchObject({ mediaType: 'image/png', size: 1001 })
    expect(answer.workspace_file.name).toMatch(/^images\/[0-9a-f]{12}-plate_1\.png$/)
    expect(ws.written.get(answer.workspace_file.name)?.length).toBe(1001)
  })

  it('uses the default limit when none is set', async () => {
    const { ctx: c } = ctx()
    expect((await imageOrRef(png(DEFAULT_TOOL_IMAGE_INLINE_MAX), 'image/png', c, REF)).content[0]).toMatchObject({ type: 'image' })
    expect((await imageOrRef(png(DEFAULT_TOOL_IMAGE_INLINE_MAX + 1), 'image/png', c, REF)).content[0]).toMatchObject({ type: 'text' })
  })

  it('says a kept image too large to view is too large, whatever the limit', async () => {
    const { ctx: c } = ctx(IMAGE_DATA_MAX)
    const answer = firstText(await imageOrRef(png(IMAGE_DATA_MAX), 'image/png', c, REF)) as { note: string }
    expect(answer.note).toMatch(/too large for you to view/)
  })

  it('answers a HEIC photo as a note, an SVG as its text', async () => {
    const { ctx: c } = ctx()
    expect(firstText(await imageOrRef(Buffer.from('....ftypheic'), 'image/heic', c, REF))).toMatchObject({ viewable: false, media_type: 'image/heic' })
    expect(await imageOrRef(Buffer.from('<svg/>'), 'image/svg+xml', c, REF)).toEqual({ content: [{ type: 'text', text: '<svg/>' }] })
  })

  it('answers a note, not a path, when there is no workspace', async () => {
    const { ctx: c } = ctx(1000, false)
    const answer = firstText(await imageOrRef(png(2000), 'image/png', c, REF)) as Record<string, unknown>
    expect(answer).toMatchObject({ inline: false, size_bytes: 2000 })
    expect(JSON.stringify(answer)).not.toContain('/api/v1')
  })
})
