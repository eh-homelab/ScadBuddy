import { describe, expect, it } from 'vitest'
import { safeHttpUrl, safeImageSrc } from './safeUrl'

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

  it('refuses other hosts and schemes, and origin-confusion tricks', () => {
    expect(safeImageSrc('https://evil.example/api/v1/models/m/thumbnail')).toBeNull()
    expect(safeImageSrc('//evil.example/api/v1/models/m/thumbnail')).toBeNull()
    expect(safeImageSrc('/api/v1\\evil.example/models/m/thumbnail')).toBeNull()
    expect(safeImageSrc('/api/v1/models/m/thumbnail\\..\\..\\settings')).toBeNull()
    expect(safeImageSrc('data:image/png;base64,AAAA')).toBeNull()
    expect(safeImageSrc('javascript:alert(1)')).toBeNull()
    expect(safeImageSrc('/assets/x.png')).toBeNull()
  })

  it('refuses nothing at all', () => {
    expect(safeImageSrc('')).toBeNull()
    expect(safeImageSrc(null)).toBeNull()
    expect(safeImageSrc(undefined)).toBeNull()
  })
})
