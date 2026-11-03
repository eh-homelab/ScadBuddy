import { describe, expect, it } from 'vitest'
import type { Output } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { wasSent } from './sent'

type Plate = NonNullable<Output['plates']>[number]
type Route = NonNullable<Output['print_route']>

const never: Output = {
  ...(fixtures.outputs[0] as Output),
  library_files: [],
  queue_item_id: null,
  plates: [],
  print_route: null,
  sent_to_bambuddy: false,
}

describe('wasSent (#2287, #2545)', () => {
  it('is false for an output that never reached Bambuddy', () => {
    expect(wasSent(never)).toBe(false)
    expect(wasSent({ ...never, library_files: undefined, plates: undefined })).toBe(false)
  })

  it('is true once any of it reached Bambuddy', () => {
    expect(wasSent({ ...never, library_files: (fixtures.outputs[0] as Output).library_files })).toBe(true)
    expect(wasSent({ ...never, queue_item_id: 4471 })).toBe(true)
    expect(wasSent({ ...never, plates: [{} as Plate] })).toBe(true)
    expect(wasSent({ ...never, print_route: 'slice_queue' as Route })).toBe(true)
  })

  it('is false for an output printed only over the LAN, which never reached Bambuddy (#4644)', () => {
    expect(wasSent({ ...never, print_route: 'lan', print_run_id: 'run-1' })).toBe(false)
  })

  it('is true for an output queued to Bambuddy, then printed over the LAN (#5143)', () => {
    // The LAN print cleared the queue item; the backend keeps that it was sent.
    expect(
      wasSent({ ...never, print_route: 'lan', print_run_id: 'run-1', sent_to_bambuddy: true }),
    ).toBe(true)
  })
})
