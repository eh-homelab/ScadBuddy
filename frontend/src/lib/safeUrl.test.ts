import { describe, expect, it } from 'vitest'
import { MAX_DATA_IMAGE_CHARS, safeHttpUrl, safeImageSrc } from './safeUrl'

describe('safeHttpUrl', () => {
  it('allows https and http', () => {
    expect(safeHttpUrl('https://example.com/model.scad')).toBe('https://example.com/model.scad')
    expect(safeHttpUrl('http://example.com/model.scad')).toBe('http://example.com/model.scad')
  })

  it('refuses script and data URLs, whatever their case', () => {
    expect(safeHttpUrl('javascript:alert(document.domain)')).toBeNull()
    expect(safeHttpUrl('JavaScript:alert(1)')).toBeNull()
    expect(safeHttpUrl('JAVASCRIPT:alert(1)')).toBeNull()
    expect(safeHttpUrl('  javascript:alert(1)')).toBeNull()
    expect(safeHttpUrl('data:text/html,<script>alert(1)</script>')).toBeNull()
  })

  it('refuses other schemes', () => {
    expect(safeHttpUrl('file:///etc/passwd')).toBeNull()
    expect(safeHttpUrl('vbscript:msgbox(1)')).toBeNull()
  })

  it('refuses what does not parse, and nothing at all', () => {
    expect(safeHttpUrl('not a url')).toBeNull()
    expect(safeHttpUrl('//example.com/relative')).toBeNull()
    expect(safeHttpUrl('')).toBeNull()
    expect(safeHttpUrl(null)).toBeNull()
    expect(safeHttpUrl(undefined)).toBeNull()
  })
})

describe('safeImageSrc (#820)', () => {
  it('allows each read-only image route, keeping the query', () => {
    for (const src of [
      '/api/v1/jobs/j1/views/iso.png?size=256',
      '/api/v1/jobs/j1/colours.png',
      '/api/v1/outputs/o1/thumbnail',
      '/api/v1/outputs/o1/views/top.png',
      '/api/v1/outputs/o1/plates/2/thumbnail',
      '/api/v1/models/my-box/thumbnail?v=3',
    ]) {
      expect(safeImageSrc(src)).toBe(src)
    }
  })

  it('normalises a path that stays on an image route', () => {
    expect(safeImageSrc('/api/v1/jobs/j1/./views/iso.png')).toBe('/api/v1/jobs/j1/views/iso.png')
  })

  it('refuses every other API route, including ones reached by ..', () => {
    expect(safeImageSrc('/api/v1/settings')).toBeNull()
    expect(safeImageSrc('/api/v1/prints/1/thumbnail')).toBeNull()
    expect(safeImageSrc('/api/v1/models/m/media/x')).toBeNull()
    expect(safeImageSrc('/api/v1/jobs/j1/preview.glb')).toBeNull()
    expect(safeImageSrc('/api/v1/outputs/o1/plates/x/thumbnail')).toBeNull()
    expect(safeImageSrc('/api/v1/models/m/../../settings')).toBeNull()
    expect(safeImageSrc('/api/v1/models/m/thumbnail/extra')).toBeNull()
  })

  it('refuses percent-encoded dot segments and slashes on an API path (#951)', () => {
    for (const src of [
      '/api/v1/models/m/%2e%2e/%2e%2e/settings',
      '/api/v1/models/%2E%2E/thumbnail',
      '/api/v1/models/.%2e/thumbnail',
      '/api/v1/models/..%2F..%2Fsettings/thumbnail',
      '/api/v1/models/a%2fb/thumbnail',
      '/api/v1/models/a%5Cb/thumbnail',
      '/api/v1/models/m/images/%2e%2e/x.png',
      '/api/v1/models/m/images/%2Ehidden.png',
      '/api/v1/models/%E0%A4%A/thumbnail',
    ]) {
      expect(safeImageSrc(src), src).toBeNull()
    }
    expect(safeImageSrc('/api/v1/models/m/images/my%20pic.png')).toBe('/api/v1/models/m/images/my%20pic.png')
    // A literal % in a file name is sent as %25, as the server decodes the path.
    expect(safeImageSrc('100%.png', { slug: 'm' })).toBe('/api/v1/models/m/images/100%25.png')
    expect(safeImageSrc('/api/v1/models/m/images/100%25.png')).toBe('/api/v1/models/m/images/100%25.png')
    expect(safeImageSrc('a%2e%2e.png', { slug: 'm' })).toBe('/api/v1/models/m/images/a%2e%2e.png')
    // Whatever a relative path resolves to, the same URL written out is allowed too.
    const resolved = safeImageSrc('a+b (1).png', { slug: 'm' })
    expect(resolved).toBe('/api/v1/models/m/images/a+b%20(1).png')
    expect(safeImageSrc(resolved)).toBe(resolved)
  })

  it('refuses other hosts and schemes, and origin-confusion tricks', () => {
    expect(safeImageSrc('https://evil.example/api/v1/models/m/thumbnail')).toBeNull()
    expect(safeImageSrc('//evil.example/api/v1/models/m/thumbnail')).toBeNull()
    expect(safeImageSrc('/api/v1\\evil.example/models/m/thumbnail')).toBeNull()
    expect(safeImageSrc('/api/v1/models/m/thumbnail\\..\\..\\settings')).toBeNull()
    expect(safeImageSrc('data:text/html;base64,PHNjcmlwdD4=')).toBeNull()
    expect(safeImageSrc('javascript:alert(1)')).toBeNull()
    expect(safeImageSrc('/assets/x.png')).toBeNull()
  })

  it('refuses nothing at all', () => {
    expect(safeImageSrc('')).toBeNull()
    expect(safeImageSrc(null)).toBeNull()
    expect(safeImageSrc(undefined)).toBeNull()
  })
})

describe('safeImageSrc', () => {
  it('allows an inline data: image of each raster type and SVG, whatever its case (#951)', () => {
    for (const type of ['png', 'jpeg', 'gif', 'webp', 'svg+xml']) {
      const src = `data:image/${type};base64,AAAA`
      expect(safeImageSrc(src)).toBe(src)
    }
    expect(safeImageSrc('DATA:IMAGE/PNG;BASE64,AAAA')).toBe('DATA:IMAGE/PNG;BASE64,AAAA')
    expect(safeImageSrc('data:image/svg+xml;charset=utf-8,%3Csvg%3E%3C/svg%3E')).toBe(
      'data:image/svg+xml;charset=utf-8,%3Csvg%3E%3C/svg%3E',
    )
  })

  it('allows a backslash inside an SVG data: image, where it is only text', () => {
    const src = "data:image/svg+xml;utf8,<svg><text>a\\b</text></svg>"
    expect(safeImageSrc(src)).toBe(src)
  })

  it('refuses every other data: type', () => {
    for (const src of [
      'data:text/html,<script>alert(1)</script>',
      'data:text/html;base64,PHNjcmlwdD4=',
      'data:application/octet-stream;base64,AAAA',
      'data:image/svg,<svg/>',
      'data:image/bmp;base64,AAAA',
      'data:image/png',
      'data:,hello',
      'data:image/pngx;base64,AAAA',
      ' data:image/png;base64,AAAA',
    ]) {
      expect(safeImageSrc(src), src).toBeNull()
    }
  })

  it('refuses a data: image over the size cap', () => {
    const head = 'data:image/png;base64,'
    expect(safeImageSrc(head + 'A'.repeat(MAX_DATA_IMAGE_CHARS - head.length))).not.toBeNull()
    expect(safeImageSrc(head + 'A'.repeat(MAX_DATA_IMAGE_CHARS - head.length + 1))).toBeNull()
  })

  it('without a base, never resolves a relative or filesystem path', () => {
    for (const src of ['thumbnail.png', './thumbnail.png', 'images/a.png', '/home/me/a.png', 'C:/a.png']) {
      expect(safeImageSrc(src), src).toBeNull()
    }
  })

  it('with a base, resolves a relative path to the model image route at its revision', () => {
    const base = { slug: 'chunky-name-sign', revision: 'abc1234' }
    expect(safeImageSrc('thumbnail.png', base)).toBe(
      '/api/v1/models/chunky-name-sign/images/thumbnail.png?commit=abc1234',
    )
    expect(safeImageSrc('images/arch-ring-stand.png', base)).toBe(
      '/api/v1/models/chunky-name-sign/images/images/arch-ring-stand.png?commit=abc1234',
    )
    expect(safeImageSrc('./room.png', base)).toBe('/api/v1/models/chunky-name-sign/images/room.png?commit=abc1234')
    expect(safeImageSrc('my%20pic.PNG', base)).toBe('/api/v1/models/chunky-name-sign/images/my%20pic.PNG?commit=abc1234')
    expect(safeImageSrc('a.png', { slug: 'demo' })).toBe('/api/v1/models/demo/images/a.png')
    expect(safeImageSrc('a.png', { slug: '_builtin:x y' })).toBe('/api/v1/models/_builtin%3Ax%20y/images/a.png')
  })

  it('with a base, still refuses what would climb out, hide, or not be an image', () => {
    const base = { slug: 'demo', revision: 'abc1234' }
    for (const src of [
      '../other/thumbnail.png',
      'images/../../x.png',
      '/etc/thumbnail.png',
      '//evil.example/x.png',
      'https://evil.example/x.png',
      'javascript:alert(1)',
      'file:///etc/x.png',
      'images\\a.png',
      '.renders/k/plate.png',
      'images/.hidden.png',
      'model.scad',
      'README.md',
      'thumbnail.png?x=1',
      'thumbnail.png#frag',
      '',
    ]) {
      expect(safeImageSrc(src, base), src).toBeNull()
    }
    // A model image route named outright is on the allowlist; another route is not.
    expect(safeImageSrc('/api/v1/models/demo/images/images/a.png?commit=abc1234')).toBe(
      '/api/v1/models/demo/images/images/a.png?commit=abc1234',
    )
    expect(safeImageSrc('/api/v1/models/demo/images/model.scad')).toBeNull()
    // Only the model image's extension is case-blind; every other route matches exactly.
    expect(safeImageSrc('/api/v1/models/demo/images/a.PNG')).toBe('/api/v1/models/demo/images/a.PNG')
    expect(safeImageSrc('/api/v1/jobs/j/views/a.PNG')).toBeNull()
    expect(safeImageSrc('/api/v1/jobs/j/COLOURS.png')).toBeNull()
    expect(safeImageSrc('/api/v1/models/demo/THUMBNAIL')).toBeNull()
    expect(safeImageSrc('/api/v1/models/demo/IMAGES/a.png')).toBeNull()
    expect(safeImageSrc('/api/v1/models/demo/files/a.png')).toBeNull()
    // An API path and a data: image are what they were without a base.
    expect(safeImageSrc('/api/v1/jobs/j/views/top.png', base)).toBe('/api/v1/jobs/j/views/top.png')
    expect(safeImageSrc('data:image/png;base64,AAAA', base)).toBe('data:image/png;base64,AAAA')
  })
})
