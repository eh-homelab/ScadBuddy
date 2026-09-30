import { describe, expect, it } from 'vitest'
import { statusLabel } from './status'

describe('statusLabel', () => {
  it('names the statuses the history knows, and turns any other into words', () => {
    expect(statusLabel('completed')).toBe('Succeeded')
    expect(statusLabel('deleted_in_bambuddy')).toBe('Deleted in Bambuddy')
    expect(statusLabel('skipped_objects')).toBe('Skipped objects')
  })
})
