import { describe, expect, it } from 'vitest'
import { safeHttpUrl } from './safeUrl'

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
