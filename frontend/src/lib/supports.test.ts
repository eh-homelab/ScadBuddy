import { describe, expect, it } from 'vitest'
import { SUPPORT_KEYS } from './supports'

describe('SUPPORT_KEYS', () => {
  it('is the backend’s list, in its order (bambuddy/supports.py, pinned by test_print_supports.py)', () => {
    expect([...SUPPORT_KEYS]).toEqual([
      'enable_support',
      'support_type',
      'support_threshold_angle',
      'support_on_build_plate_only',
    ])
  })
})
