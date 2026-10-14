import { describe, expect, it } from 'vitest'
import { profileMatches } from './filamentProfiles'

const silk = { match: { material: 'PLA', word: 'silk' } }

describe('profileMatches (#717)', () => {
  it.each([
    [{ material: 'PLA', subtype: 'Silk', slicer_filament_name: null }, true],
    // Bambu's tri-colour silk: subtype Tri Color, preset Bambu PLA Silk.
    [{ material: 'PLA', subtype: 'Tri Color', slicer_filament_name: 'Bambu PLA Silk @BBL H2C' }, true],
    [{ material: 'pla', subtype: 'Silk+', slicer_filament_name: null }, true],
    [{ material: 'PETG', subtype: 'Silk', slicer_filament_name: null }, false],
    [{ material: 'PLA', subtype: 'Basic', slicer_filament_name: 'Bambu PLA Basic' }, false],
  ])('%o → %s', (spool, matches) => {
    expect(profileMatches(silk, spool)).toBe(matches)
  })
})
