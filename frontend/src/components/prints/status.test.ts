import { describe, expect, it } from 'vitest'
import { isUnfinished, statusLabel } from './status'

describe('statusLabel', () => {
  it('names the statuses the history knows, and turns any other into words', () => {
    expect(statusLabel('completed')).toBe('Succeeded')
    expect(statusLabel('deleted_in_bambuddy')).toBe('Deleted in Bambuddy')
    expect(statusLabel('skipped_objects')).toBe('Skipped objects')
  })
})

describe('isUnfinished (#2736)', () => {
  it('is a print with no outcome yet', () => {
    expect(['pending', 'queued', 'printing', 'running', 'paused'].every(isUnfinished)).toBe(true)
    expect(['completed', 'failed', 'cancelled', 'deleted_in_bambuddy'].some(isUnfinished)).toBe(false)
  })
})
