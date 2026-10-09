import { createHash } from 'node:crypto'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it } from 'vitest'
import { BLOB_NAME, imageOfBlock, RESULT_IMAGES_MAX } from '../src/sessions/blobs.js'
import { IMAGE_DATA_MAX } from '../src/sessions/images.js'
import { event, type ServerEvent } from '../src/sessions/protocol.js'
import { SdkEventMapper, TITLE_MAX, type TitleResolver } from '../src/sessions/sdkEvents.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { defaultTitle, type ToolServices } from '../src/tools/registry.js'
import { condense } from '../src/tools/sessions.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'

// #782: a tool call's title, and the images a tool result carries, as the
// session's events carry them to the panel (design 2026-10-08 §3.1, §3.2).

const S = '11111111-2222-4333-8444-555555555555'
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0x4a, 0x46, 0x49, 0x46, 0, 1]).toString('base64')
const sha = (b64: string) => createHash('sha256').update(Buffer.from(b64, 'base64')).digest('hex')

const assistant = (content: unknown[]) =>
  ({ type: 'assistant', message: { id: 'msg_1', content }, parent_tool_use_id: null, session_id: S, uuid: 'a' }) as unknown as SDKMessage
const result = (content: unknown, isError = false) =>
  ({
    type: 'user',
    message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'toolu_1', content, ...(isError ? { is_error: true } : {}) }] },
    parent_tool_use_id: null,
    session_id: S,
  }) as unknown as SDKMessage
const image = (data: string, mediaType = 'image/png') => ({ type: 'image', source: { type: 'base64', media_type: mediaType, data } })
// What Claude Code adds after each MCP image (measured on SDK 0.3.289).
const sourceLine = (ext = 'png') =>
  ({ type: 'text', text: `[Image: source: /tmp/claude-resume-fa89/projects/-w/${S}/tool-results/mcp-scadbuddy-blob-1791507694153-zfopia.${ext}]` })

function resultOf(events: ServerEvent[]) {
  const r = events.find((e) => e.type === 'tool.result')
  if (r?.type !== 'tool.result') throw new Error('no tool.result')
  return r
}

describe('tool.result images (#782)', () => {
  it('carries an image block as a named ref, its bytes for the store, and drops Claude Code’s path line', () => {
    const mapper = new SdkEventMapper(S, () => 'read')
    const r = resultOf(mapper.map(result([{ type: 'text', text: 'Rendered preview' }, image(PNG), sourceLine()])))
    expect(r.summary).toBe('Rendered preview')
    expect(r.images).toEqual([{ name: `${sha(PNG)}.png`, mediaType: 'image/png' }])
    expect(r.images![0]!.name).toMatch(BLOB_NAME)
    const taken = mapper.takeImages()
    expect(taken.map((i) => [i.name, i.mediaType, i.bytes.equals(Buffer.from(PNG, 'base64'))])).toEqual([
      [`${sha(PNG)}.png`, 'image/png', true],
    ])
    // Taken once.
    expect(mapper.takeImages()).toEqual([])
  })

  it('takes the type Claude Code re-encoded to, and an image sent as an embedded resource', () => {
    const mapper = new SdkEventMapper(S, () => 'read')
    const r = resultOf(
      mapper.map(result([{ type: 'text', text: '[Resource from scadbuddy at scadbuddy://x.png] ' }, image(JPEG, 'image/jpeg'), sourceLine()])),
    )
    expect(r.summary).toBe('')
    expect(r.images).toEqual([{ name: `${sha(JPEG)}.jpg`, mediaType: 'image/jpeg' }])
  })

  it('names the same image once, and keeps the same summary as before for a result without images', () => {
    const mapper = new SdkEventMapper(S, () => 'read')
    expect(resultOf(mapper.map(result([image(PNG), image(PNG)]))).images).toHaveLength(1)
    expect(resultOf(mapper.map(result('plain text'))).images).toBeUndefined()
    expect(resultOf(mapper.map(result([{ type: 'text', text: 'found 3' }]))).summary).toBe('found 3')
  })

  it('leaves an image the panel may not show as [image], with Claude Code’s line', () => {
    const svg = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>').toString('base64')
    const lying = image(JPEG, 'image/png')
    const notBase64 = image('!!!!', 'image/png')
    const url = { type: 'image', source: { type: 'url', url: 'https://example.com/x.png' } }
    const huge = image(PNG.slice(0, 12) + 'A'.repeat(IMAGE_DATA_MAX), 'image/png')
    for (const block of [image(svg, 'image/svg+xml'), lying, notBase64, url, huge]) {
      const mapper = new SdkEventMapper(S, () => 'read')
      const r = resultOf(mapper.map(result([block, sourceLine()])))
      expect(r.images).toBeUndefined()
      expect(r.summary).toMatch(/^\[image\]\n\[Image: source: /)
      expect(mapper.takeImages()).toEqual([])
    }
  })

  it(`keeps at most ${RESULT_IMAGES_MAX} images of one result`, () => {
    const mapper = new SdkEventMapper(S, () => 'read')
    // Distinct images: a PNG signature and a different byte each.
    const pngs = Array.from({ length: RESULT_IMAGES_MAX + 1 }, (_, i) =>
      Buffer.concat([Buffer.from(PNG, 'base64'), Buffer.from([i, 0, 0])]).toString('base64'),
    )
    const r = resultOf(mapper.map(result(pngs.map((p) => image(p)))))
    expect(r.images).toHaveLength(RESULT_IMAGES_MAX)
    expect(r.summary).toBe('[image]')
  })

  it('refuses what is not an image block', () => {
    expect(imageOfBlock(null)).toBeUndefined()
    expect(imageOfBlock({ type: 'text', text: PNG })).toBeUndefined()
    expect(imageOfBlock({ type: 'image' })).toBeUndefined()
    expect(imageOfBlock(image(''))).toBeUndefined()
  })

  it('shows a result’s images to an MCP reader as a count', () => {
    const rows = [{ seq: 1, event: event({ type: 'tool.result', sessionId: S, id: 't', ok: true, summary: '', images: [{ name: `${sha(PNG)}.png`, mediaType: 'image/png' }] }) }]
    expect(condense(rows)).toEqual([{ seq: 1, type: 'tool.result', id: 't', ok: true, summary: '', images: 1 }])
  })
})

describe('tool.call titles (#782)', () => {
  const call = (name: string, input: Record<string, unknown> = {}) => assistant([{ type: 'tool_use', id: `toolu_${name}`, name, input }])
  const titleOf = (titleOf: TitleResolver, name = 'mcp__scadbuddy__render_model', input: Record<string, unknown> = { slug: 'cable-clip' }) => {
    const e = new SdkEventMapper(S, () => 'read', titleOf).map(call(name, input))[0]
    if (e?.type !== 'tool.call') throw new Error('no tool.call')
    return e.title
  }

  it('carries the resolver’s title, one line and capped', () => {
    expect(titleOf((_n, input) => `Render ${String(input.slug)}`)).toBe('Render cable-clip')
    expect(titleOf(() => '  Render\n  cable-clip ')).toBe('Render cable-clip')
    const long = titleOf(() => 'x'.repeat(TITLE_MAX * 2))!
    expect(long).toHaveLength(TITLE_MAX)
    expect(long.endsWith('…')).toBe(true)
  })

  it('has no title for a tool the resolver does not know, an empty one, or one that throws', () => {
    expect(titleOf(() => undefined)).toBeUndefined()
    expect(titleOf(() => '   ')).toBeUndefined()
    expect(
      titleOf(() => {
        throw new Error('bad input')
      }),
    ).toBeUndefined()
    // No resolver at all (a mapper built without one).
    const e = new SdkEventMapper(S, () => 'read').map(call('Agent'))[0]
    expect(e).toMatchObject({ type: 'tool.call', name: 'Agent' })
    expect(e && 'title' in e).toBe(false)
  })

  it('gives the approval gate’s call its title too', () => {
    const mapper = new SdkEventMapper(S, () => 'outward', () => 'Print cable-clip')
    expect(mapper.call('toolu_9', 'mcp__scadbuddy__print_output', {})).toMatchObject({ title: 'Print cable-clip' })
  })

  it('produces events the panel’s own schema accepts, title and images kept', async () => {
    const mapper = new SdkEventMapper(S, () => 'read', () => 'Get preview')
    await expectPanelAccepts([
      ...mapper.map(call('mcp__scadbuddy__get_render_preview')),
      ...mapper.map(result([image(PNG), sourceLine()])),
    ])
  })
})

describe('tool titles from the registry (#782)', () => {
  const { titleOf } = harnessTools({} as ToolServices)

  it('uses the title a tool declares', () => {
    expect(titleOf('mcp__scadbuddy__render_model', { slug: 'cable-clip' })).toBe('Render cable-clip')
    expect(titleOf('mcp__scadbuddy__save_preset', { slug: 'keyring', name: 'Bubbly keyring' })).toBe('Save preset → Bubbly keyring (keyring)')
  })

  it('gives every other registry tool its name in words, with its slug', () => {
    expect(titleOf('mcp__scadbuddy__get_readme', { slug: 'cable-clip' })).toBe('Get readme → cable-clip')
    expect(defaultTitle('list_fonts', {})).toBe('List fonts')
    // Arguments that do not parse fall back to the default, never throw.
    expect(titleOf('mcp__scadbuddy__render_model', { slug: 42 })).toBe('Render model')
  })

  it('has none for a name outside the registry', () => {
    expect(titleOf('mcp__hindsight__recall', {})).toBeUndefined()
    expect(titleOf('Agent', {})).toBeUndefined()
    expect(titleOf('render_model', {})).toBeUndefined()
  })

  it('gives every registry tool a title for empty arguments without throwing', () => {
    for (const tool of ALL_TOOLS) expect(tool.title({})).toEqual(expect.any(String))
  })
})
