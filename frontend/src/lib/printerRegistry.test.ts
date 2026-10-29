import { describe, expect, it } from 'vitest'
import type { PrinterSyncResult } from '../api/types'
import { syncSummary } from './printerRegistry'

function result(targetMissing: number): PrinterSyncResult {
  return {
    dry_run: false,
    entries: [],
    virtual_printers_error: null,
    created: 0,
    linked: 0,
    already_linked: targetMissing,
    skipped: 0,
    conflicts: 0,
    lan_linked: 0,
    lan_failed: 0,
    target_missing: targetMissing,
  }
}

describe('syncSummary', () => {
  it('names one virtual printer with a missing target in the singular, more in the plural (#4858)', () => {
    expect(syncSummary(result(1))).toContain('1 virtual printer forwarding to a printer not here')
    expect(syncSummary(result(2))).toContain('2 virtual printers forwarding to a printer not here')
    expect(syncSummary(result(0))).not.toContain('forwarding')
  })
})
