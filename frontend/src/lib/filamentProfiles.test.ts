import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PrintChoices, SlotChoice, SpoolOption } from '../api/types'
import { profileMatches, useFilamentProfiles } from './filamentProfiles'

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

describe('useFilamentProfiles restore (#2718 review)', () => {
  const spools = [{ spool_id: 7, material: 'PLA', subtype: 'Silk', slicer_filament_name: null }] as unknown as SpoolOption[]
  const plan = [{ slot_id: 1, spool_id: 7 }] as unknown as SlotChoice[]
  const choices = { nozzles: [{ size: '0.4', flow: 'standard' }], tier: 'standard' } as unknown as PrintChoices

  it('brings back a saved request’s profiles, before and after the matches are known', async () => {
    const { result } = renderHook(() => useFilamentProfiles({ spools, plan, choices }))
    // Restored before the profiles are read: none of the request's, so silk stays off.
    act(() => result.current.restore([]))
    await waitFor(() => expect(result.current.suggestions.map((entry) => entry.profile.id)).toEqual(['silk-pla']))
    await waitFor(() => expect(result.current.suggestions[0]?.preview).not.toBeNull())
    expect(result.current.active).toEqual([])

    act(() => result.current.toggle('silk-pla', true))
    expect(result.current.active).toEqual(['silk-pla'])
    act(() => result.current.restore(['silk-pla']))
    expect(result.current.active).toEqual(['silk-pla'])
    act(() => result.current.reset())
    expect(result.current.active).toEqual(['silk-pla'])
  })
})
