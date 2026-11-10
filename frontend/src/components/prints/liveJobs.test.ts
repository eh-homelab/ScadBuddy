import { describe, expect, it } from 'vitest'
import type { PrinterNow, PrintSummary, QueueItem } from '../../api/types'
import { PRINTERS_NOW } from '../../mocks/features/printersNow'
import { PRINT_QUEUE } from '../../mocks/features/printQueue'
import type { NowCard } from './nowCards'
import { liveJobs, liveLabel, printRows, QUEUE_SHOWN } from './liveJobs'

const card = (printer: Partial<PrinterNow>): NowCard => ({
  printer: { ...PRINTERS_NOW[2]!, ...printer },
  seenAt: 0,
  stale: null,
})

const item = (fields: Partial<QueueItem> & Pick<QueueItem, 'id'>): QueueItem => ({
  status: 'pending',
  manual_start: false,
  filament_short: false,
  ...fields,
})

const print = (archive_id: number, fields: Partial<PrintSummary> = {}): PrintSummary =>
  ({ archive_id, queue_item_id: null, status: 'completed', ...fields }) as PrintSummary

const printing = card({ id: 'p2', activity: 'printing', archive_id: 37, progress: 66, remaining_minutes: 54 })

describe('liveJobs (#3745): one job per print, keyed by what ties its copies together', () => {
  it('keys a working job by its archive, else by its printer; an idle printer is no job', () => {
    const jobs = liveJobs([printing, card({ id: 'p4', activity: 'printing', archive_id: null }), card({ id: 'p3' })], [])
    expect(jobs.map((job) => job.key)).toEqual(['archive:37', 'printer:p4'])
  })

  it("takes the archive ScadBuddy linked when the printer's status names none", () => {
    const scadbuddy = { subject: 'output:x', archive_id: 41, matched_by: 'lan_start' } as PrinterNow['scadbuddy']
    expect(liveJobs([card({ id: 'p5', activity: 'paused', archive_id: null, scadbuddy })], [])[0]!.key).toBe('archive:41')
  })

  it("lists the pending items in Bambuddy's order after the working jobs, with their place", () => {
    const jobs = liveJobs([printing], [item({ id: 9, position: 4 }), item({ id: 8, position: 2 }), item({ id: 7 })])
    expect(jobs.map((job) => job.key)).toEqual(['archive:37', 'queue:8', 'queue:9', 'queue:7'])
    expect(jobs.map(liveLabel)).toEqual([
      'Printing 66% · 54m left',
      'Queued · position 1',
      'Queued · position 2',
      'Queued · position 3',
    ])
  })

  it('keeps only the items that are still to print, and leaves the tail to Bambuddy', () => {
    const many = Array.from({ length: QUEUE_SHOWN + 2 }, (_, index) => item({ id: 100 + index, position: index }))
    const jobs = liveJobs([], [...many, item({ id: 99, status: 'printing' })])
    expect(jobs).toHaveLength(QUEUE_SHOWN)
    expect(jobs.some((job) => job.key === 'queue:99')).toBe(false)
  })

  it('drops a queued item whose linked archive a printer already prints: the printer is newer', () => {
    const scadbuddy = { subject: 'output:x', archive_id: 37, matched_by: 'queue_item' } as QueueItem['scadbuddy']
    expect(liveJobs([printing], [item({ id: 5, scadbuddy })]).map((job) => job.key)).toEqual(['archive:37'])
  })

  it('says a pause and its first HMS error, or its progress when there is none', () => {
    const [paused] = liveJobs([{ printer: PRINTERS_NOW[0]!, seenAt: 0, stale: null }], [])
    expect(liveLabel(paused!)).toBe('Paused: Spaghetti detected: check the print.')
    const [quiet] = liveJobs([card({ id: 'q', activity: 'paused', archive_id: 3, progress: 12.4 })], [])
    expect(liveLabel(quiet!)).toBe('Paused 12%')
    const [coded] = liveJobs(
      [card({ id: 'c', activity: 'paused', archive_id: 4, hms: [{ code: '0700', description: null, severity: 2 }] })],
      [],
    )
    expect(liveLabel(coded!)).toBe('Paused: HMS 0700')
  })

  it('says only what the printer reported', () => {
    const [bare] = liveJobs([card({ id: 'b', activity: 'printing', archive_id: 6 })], [])
    expect(liveLabel(bare!)).toBe('Printing')
  })

  it('reads the mocks as one of each kind: a Bambuddy job, a ScadBuddy job, and their queue', () => {
    const cards = PRINTERS_NOW.map((printer) => ({ printer, seenAt: 0, stale: null }))
    expect(liveJobs(cards, PRINT_QUEUE).map((job) => job.key)).toEqual([
      'archive:90',
      'archive:37',
      'queue:259',
      'queue:260',
      'queue:263',
    ])
  })
})

describe('printRows (#3745): each print once, in one list', () => {
  const jobs = liveJobs([printing], [item({ id: 8, position: 1 }), item({ id: 12, position: 2 })])

  it('lifts the archived print of a working job to the top, with its live state, and lists it once', () => {
    const { top, rest } = printRows(jobs, [print(38), print(37, { status: 'printing' }), print(36)], false)
    expect(top.map((row) => [row.key, row.print?.archive_id ?? null])).toEqual([
      ['archive:37', 37],
      ['queue:8', null],
      ['queue:12', null],
    ])
    expect(rest.map((row) => row.print.archive_id)).toEqual([38, 36])
    expect(rest.every((row) => row.live === null)).toBe(true)
  })

  it('drops a queued item whose print the history already has: the queue read is older', () => {
    const { top, rest } = printRows(jobs, [print(40, { queue_item_id: 12, status: 'printing' })], false)
    expect(top.map((row) => row.key)).toEqual(['archive:37', 'queue:8'])
    expect(rest.map((row) => row.print.archive_id)).toEqual([40])
  })

  it('on a filtered list shows only the prints that match, with a working one\'s live state in place', () => {
    const { top, rest } = printRows(jobs, [print(38), print(37, { status: 'printing' })], true)
    expect(top).toEqual([])
    expect(rest.map((row) => [row.print.archive_id, row.live?.key ?? null])).toEqual([
      [38, null],
      [37, 'archive:37'],
    ])
  })
})
