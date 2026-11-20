import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import type { PrimeTowerChoice } from '../api/types'
import { analysisTower, overriddenTowerKeys, PRIME_TOWER_KEYS } from './primeTower'

describe('PRIME_TOWER_KEYS', () => {
  it('is the backend’s list, Bambu Studio’s names (bambuddy/prime_tower.py)', () => {
    expect([...PRIME_TOWER_KEYS]).toEqual(['enable_prime_tower', 'wipe_tower_no_sparse_layers'])
  })
})

describe('overriddenTowerKeys', () => {
  it('strikes nothing for Default', () => {
    expect(overriddenTowerKeys(null)).toEqual([])
  })

  it('strikes both with the tower off', () => {
    expect(overriddenTowerKeys({ enabled: false })).toEqual([...PRIME_TOWER_KEYS])
  })

  it('strikes No sparse layers only when the choice sets it', () => {
    expect(overriddenTowerKeys({ enabled: true })).toEqual(['enable_prime_tower'])
    expect(overriddenTowerKeys({ enabled: true, no_sparse_layers: false })).toEqual([...PRIME_TOWER_KEYS])
  })
})

describe('analysisTower (#6721)', () => {
  it('is the choice when no setting names the tower', () => {
    expect(analysisTower(null, null)).toBeNull()
    expect(analysisTower({ enabled: true, no_sparse_layers: true }, { enable_support: '1' })).toEqual({
      enabled: true,
      no_sparse_layers: true,
    })
  })

  it("takes the setting over the choice, as the run's analysis does", () => {
    expect(analysisTower(null, { enable_prime_tower: '1' })).toEqual({ enabled: true })
    expect(analysisTower({ enabled: true }, { enable_prime_tower: '0' })).toEqual({ enabled: false })
    expect(analysisTower(null, { enable_prime_tower: false })).toEqual({ enabled: false })
    expect(analysisTower(null, { enable_prime_tower: ['0'] })).toEqual({ enabled: false })
  })

  it('keeps the choice when the setting agrees with it', () => {
    const choice = { enabled: true, no_sparse_layers: false }
    expect(analysisTower(choice, { enable_prime_tower: '1' })).toBe(choice)
  })

  // #6725: the backend's `_tower_choice` and `tower_asked` read the same file
  // (`backend/tests/bambuddy/test_tower_choice_vectors.py`), so the two cannot drift.
  it('agrees with the shared vectors', () => {
    const vectors = JSON.parse(readFileSync(`${import.meta.dirname}/../../../test-fixtures/prime-tower/tower-choice.json`, 'utf8')) as {
      case: string
      choice: PrimeTowerChoice | null
      process_overrides?: Record<string, unknown>
      want: PrimeTowerChoice | null
    }[]
    expect(vectors.length).toBeGreaterThan(0)
    for (const vector of vectors) {
      expect(analysisTower(vector.choice, vector.process_overrides ?? null), vector.case).toStrictEqual(vector.want)
    }
  })
})
