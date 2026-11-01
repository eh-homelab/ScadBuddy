import { describe, expect, it } from 'vitest'
import { lanBlockReason } from './lanBlock'

describe('lanBlockReason', () => {
  it('links a backend reason to the backends and a row reason to the printers', () => {
    expect(lanBlockReason('store_backend').href).toBe('/settings/library#projects')
    expect(lanBlockReason('no_report').href).toBe('/settings/printing#printers')
  })

  it('says when a stale report was last seen', () => {
    const seen = new Date(Date.now() - 2 * 3600 * 1000).toISOString()
    expect(lanBlockReason('stale_report', seen).text).toBe('its last LAN report was 2 hours ago')
    expect(lanBlockReason('stale_report', null).text).toBe('its LAN report is out of date')
  })
})
