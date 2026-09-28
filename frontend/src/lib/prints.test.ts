import { describe, expect, it } from 'vitest'
import { formatBytes, formatDuration, statusLabel } from './prints'

describe('print facts (#311)', () => {
  it('names statuses in words', () => {
    expect(statusLabel('completed')).toBe('Completed')
    expect(statusLabel('deleted_in_bambuddy')).toBe('Deleted in Bambuddy')
    expect(statusLabel('skipped_objects')).toBe('Skipped objects')
  })

  it('formats durations and sizes', () => {
    expect(formatDuration(null)).toBeNull()
    expect(formatDuration(2590)).toBe('43m')
    expect(formatDuration(6437)).toBe('1h 47m')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(48213)).toBe('47.1 KB')
    expect(formatBytes(2091667)).toBe('2.0 MB')
  })
})
