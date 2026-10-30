import { beforeEach, describe, expect, it, vi } from 'vitest'

// #5021: `resolveLocation` knows the sections only once the Settings chunk has evaluated the
// registry. A hash-only move must not count as staying put before that, or an agent
// `navigate` to another area's section would answer at once, with the page still suspended.
describe('staysPut', () => {
  beforeEach(() => vi.resetModules())

  it('does not answer a Settings section hash before the registry has loaded', async () => {
    const { staysPut } = await import('./global')
    expect(staysPut('/settings/general#mcp-connect', '/settings/general')).toBe(false)
  })

  it('answers once the registry knows the section is in the area it is on', async () => {
    const { staysPut } = await import('./global')
    const { registerSections } = await import('../pages/settings/areas')
    registerSections([
      { id: 'mcp-connect', area: 'mcp' },
      { id: 'connection', area: 'general' },
    ])
    expect(staysPut('/settings/general#connection', '/settings/general')).toBe(true)
    expect(staysPut('/settings/general#mcp-connect', '/settings/general')).toBe(false)
  })

  it('stays put outside Settings whatever the registry holds', async () => {
    const { staysPut } = await import('./global')
    expect(staysPut('/library#top', '/library')).toBe(true)
  })
})
