/**
 * #7168 R6, D7 — a place's tag: one NDEF `url` record, the place label's own URL
 * (`<label base>/x/L-<n>`, `labelSheet.ts` `locationPayload`), which `tag_codes.py` reads. A tap,
 * a QR scan and the phone's own launch of the URL all open the same place. The write keeps a
 * backup first (reason `place_write`, D8), binds nothing, and never locks the tag.
 */
import { postTagRead, type LocationDetail, type TagReadResult } from '../api/filament'
import { locationPayload } from '../components/filament/labelSheet'
import type { RunHooks } from '../components/filament/scan/WriteTag'
import { writeWithBackup, WriteRefusedError, type WriteOutcome } from './tagWrite'
import type { NfcSession, WireRecord } from './webNfc'

const hexOf = (text: string) => Array.from(new TextEncoder().encode(text), (byte) => byte.toString(16).padStart(2, '0')).join('')

/** The records of a place tag: one `url` record that holds `url` as UTF-8. */
export const placeRecords = (url: string): WireRecord[] => [{ record_type: 'url', media_type: null, data_hex: hexOf(url) }]

const heldBy = (result: TagReadResult) => (result.next === 'open_spool' && result.spool ? `A spool holds this tag. Unbind it from S-${result.spool.number} first.` : null)

/** Writes `place`'s label URL on `base` to the next tag held to the phone, and checks that the tag then names `place`. */
export function writePlaceTag(session: NfcSession, place: LocationDetail, base: string, hooks: RunHooks): Promise<WriteOutcome> {
  const records = placeRecords(locationPayload(base, place.code))
  return writeWithBackup({
    session,
    reason: 'place_write',
    onStep: hooks.onStep,
    onClaimed: hooks.onClaimed,
    signal: hooks.signal,
    // The reads of a write are lookups, not taps: no scan history row (#8148).
    prepare: async (now) => {
      const held = heldBy(await postTagRead({ uid: now.uid, records: now.records, tap: false }))
      if (held) throw new WriteRefusedError(`${held} Nothing was written.`)
      return records
    },
    confirm: async (_now, _backup, _sent, back) => {
      const result = await postTagRead({ uid: back.uid, records: back.records, tap: false })
      const held = heldBy(result)
      if (held) throw new WriteRefusedError(held)
      const named = result.next === 'open_code' ? result.resolved : null
      if (named?.kind === 'location' && named.id === place.id) return result
      throw new WriteRefusedError(named ? `The tag now opens ${named.code}, not ${place.code}. Write it again.` : `ScadBuddy does not read the tag as ${place.code}. Write it again.`)
    },
  })
}
