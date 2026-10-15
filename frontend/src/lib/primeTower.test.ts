import { describe, expect, it } from 'vitest'
import { overriddenTowerKeys, PRIME_TOWER_KEYS } from './primeTower'

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
