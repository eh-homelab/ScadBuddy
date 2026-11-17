import { describe, expect, it } from 'vitest'
import app from '../../App.tsx?raw'
import { FILAMENT_TABS } from './tabs'

describe('the Filament tab routes in App.tsx', () => {
  it('are one literal path per available tab, with that tab', () => {
    const routes = [...app.matchAll(/<Route path="(filament\/[a-z-]+)" element=\{<FilamentPage tab="([a-z-]+)" \/>\} \/>/g)].map((match) => [`/${match[1]}`, match[2]])
    const available = FILAMENT_TABS.filter((tab) => tab.available).map((tab) => [tab.path, tab.id])
    expect(routes).toEqual(available)
  })
})
