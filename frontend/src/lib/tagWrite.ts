/**
 * #7273 G7 — one tap writes a tag: read it, back it up, write, read it back. Nothing is
 * written before ScadBuddy confirms the backup, and nothing to a tag with no serial number.
 * The first reading may come from any tag: the server ties the write to the backup's UID,
 * and writing a spool onto a new tag is a rebind.
 */
import { ApiError } from '../api/client'
import { createTagBackup, postTagRead, restoreTagBackup, unbindTag, type TagReadResult, type TagRow } from '../api/filament'
import { cleanTagUid } from './tagUid'
import { ndefMessageBytes, notWritable, sameRecords, type NfcSession, type Reading, type WireRecord } from './webNfc'

export type WriteStep = 'backing_up' | 'writing' | 'reading_back' | 'tap_again'

export type WriteOutcome =
  | { status: 'checked'; result: TagReadResult | null }
  /** `readBack`: what the tag holds after the write. */
  | { status: 'mismatch'; backupId: string; readBack: Reading }
  | { status: 'other_tag'; backupId: string }
  | { status: 'failed'; message: string; backupId: string | null }

/** What a run does to the tag: writes a spool's records, restores a backup, or clears it (#8362). */
export type WriteMode = 'write' | 'restore' | 'clear'

const CHECKED: Record<WriteMode, string> = { write: 'Written and checked.', restore: 'Restored and checked.', clear: 'Cleared and checked.' }

/** The sentence an outcome is told by, on the screen and in the page's live region. */
export function headlineOf(outcome: WriteOutcome, mode: WriteMode): string {
  switch (outcome.status) {
    case 'checked':
      return CHECKED[mode]
    case 'mismatch':
      return 'The tag does not hold what was sent.'
    case 'other_tag':
      return 'A different tag answered. Nothing was bound.'
    case 'failed':
      return outcome.message
  }
}

/** How long the read-back waits for the tag to answer before it asks for a second tap. */
export const TAP_AGAIN_MS = 3_000

const NO_UID = 'This tag gives no serial number, so it cannot be backed up. Nothing was written.'
const BACKUP_MISMATCH = 'The tag changed after it was backed up, or the backup is too old. Nothing was written. Hold the tag to the phone again.'
const BACKUP_STALE = 'The backup no longer covers this write, so the tag was not bound. Write the tag again.'
const SENT_MISMATCH = 'The spool changed while the tag was written, so the tag was not bound. Write the tag again.'

const EMPTY_RECORD: WireRecord = { record_type: 'empty', data_hex: '' }

/** Thrown by `prepare` or `confirm` to stop the run with this sentence as its outcome's message. */
export class WriteRefusedError extends Error {}

const codeOf = (cause: unknown) => (cause instanceof ApiError && cause.status === 409 ? cause.problem.code : undefined)

function detailOf(cause: unknown): string {
  if (cause instanceof ApiError) return cause.detail
  if (cause instanceof DOMException) return cause.message || cause.name
  if (cause instanceof Error) return cause.message
  return String(cause)
}

const isAbort = (cause: unknown, signal?: AbortSignal) => signal?.aborted === true || (cause instanceof DOMException && cause.name === 'AbortError')

export interface WriteOptions {
  session: NfcSession
  reason: 'spool_write' | 'agent_write' | 'restore' | 'place_write' | 'clear'
  jobId?: string
  /** Runs after the UID check and before the backup; throws `WriteRefusedError` to stop with nothing backed up. */
  precheck?: (now: Reading) => Promise<void>
  /** The records to write, for the tag as just read and its backup. */
  prepare: (now: Reading, backupId: string) => Promise<WireRecord[]>
  /** Checks the write with the server once the read-back equals what was sent. */
  confirm: (now: Reading, backupId: string, sent: WireRecord[], readBack: Reading) => Promise<TagReadResult | null>
  onStep: (step: WriteStep) => void
  /** Called once the run holds the session's claim; a run refused by another's claim never calls it. */
  onClaimed?: () => void
  signal?: AbortSignal
}

export async function writeWithBackup({ session, reason, jobId, precheck, prepare, confirm, onStep, onClaimed, signal }: WriteOptions): Promise<WriteOutcome> {
  const release = session.claim()
  if (!release) return { status: 'failed', message: 'Another write is running. Nothing was written.', backupId: null }
  onClaimed?.()
  let backupId: string | null = null
  const stopped = (message: string): WriteOutcome => ({ status: 'failed', message, backupId })
  try {
    let now: Reading
    try {
      now = await session.nextReading(signal)
    } catch {
      return stopped('Stopped. Nothing was written.')
    }
    if (signal?.aborted) return stopped('Stopped. Nothing was written.')
    if (!now.uid) return { status: 'failed', message: NO_UID, backupId: null }

    if (precheck) {
      try {
        await precheck(now)
      } catch (cause) {
        if (isAbort(cause, signal)) return stopped('Stopped. Nothing was written.')
        if (cause instanceof WriteRefusedError) return { status: 'failed', message: cause.message, backupId: null }
        return { status: 'failed', message: `The tag was not checked, so nothing was written: ${detailOf(cause)}`, backupId: null }
      }
      if (signal?.aborted) return stopped('Stopped. Nothing was written.')
    }

    onStep('backing_up')
    try {
      backupId = (await createTagBackup({ uid: now.uid, records: now.records, reason, ...(jobId ? { job_id: jobId } : {}) }, signal)).backup_id
    } catch (cause) {
      if (isAbort(cause, signal)) return stopped('Stopped. Nothing was written.')
      return { status: 'failed', message: `The tag was not backed up, so nothing was written: ${detailOf(cause)}`, backupId: null }
    }

    if (signal?.aborted) return stopped('Stopped. Nothing was written.')
    let sent: WireRecord[]
    try {
      sent = await prepare(now, backupId)
    } catch (cause) {
      if (isAbort(cause, signal)) return stopped('Stopped. Nothing was written.')
      if (cause instanceof WriteRefusedError) return { status: 'failed', message: cause.message, backupId }
      if (codeOf(cause) === 'backup_mismatch') return { status: 'failed', message: BACKUP_MISMATCH, backupId }
      return { status: 'failed', message: `ScadBuddy did not give the records to write, so nothing was written: ${detailOf(cause)}`, backupId }
    }

    // Web NFC refuses a message with no records, so a blank tag gets one empty record.
    const written = sent.length > 0 ? sent : [EMPTY_RECORD]
    if (signal?.aborted) return stopped('Stopped. Nothing was written.')
    const refused = notWritable(written)
    if (refused) return { status: 'failed', message: `This record cannot be written from this page: ${refused}. Nothing was written.`, backupId }
    // The ScadBuddy app says how much the tag holds (#8362); Web NFC does not, and the write then finds out.
    const holds = now.tag?.max_ndef_bytes
    const needs = ndefMessageBytes(written, document.documentElement.lang || 'en')
    if (holds != null && needs > holds) return { status: 'failed', message: `This tag holds ${holds} bytes; the record needs ${needs}. Nothing was written.`, backupId }
    onStep('writing')
    try {
      await session.write(written, signal)
    } catch (cause) {
      if (isAbort(cause, signal)) return stopped('Stopped during the write. Nothing was bound.')
      // Web NFC throws a TypeError (or a SyntaxError DOMException) for a record it will not map
      // to NDEF (https://w3c-cg.github.io/web-nfc/, "Creating NDEF message"): no tag was touched.
      if (cause instanceof TypeError || (cause instanceof DOMException && cause.name === 'SyntaxError')) {
        return { status: 'failed', message: `The tag was not written: the browser refused a record (${detailOf(cause)}).`, backupId }
      }
      return { status: 'failed', message: `The tag was not written: ${detailOf(cause)}. The tag may be too small for the record, or it moved away too soon.`, backupId }
    }

    let back: Reading
    try {
      back = await readBack(session, onStep, signal)
    } catch {
      return stopped('Stopped before the check. Nothing was bound.')
    }
    if (back.uid !== now.uid) return { status: 'other_tag', backupId }
    const same = sameRecords(back.records, written) || (sent.length === 0 && back.records.length === 0)
    if (!same) return { status: 'mismatch', backupId, readBack: back }

    try {
      return { status: 'checked', result: await confirm(now, backupId, sent, back) }
    } catch (cause) {
      if (cause instanceof WriteRefusedError) return { status: 'failed', message: cause.message, backupId }
      const code = codeOf(cause)
      if (code === 'read_back_mismatch') return { status: 'mismatch', backupId, readBack: back }
      if (code === 'sent_mismatch') return { status: 'failed', message: SENT_MISMATCH, backupId }
      if (code === 'backup_mismatch') return { status: 'failed', message: BACKUP_STALE, backupId }
      return { status: 'failed', message: `The write was not checked, so the tag was not bound: ${detailOf(cause)}`, backupId }
    }
  } finally {
    release()
  }
}

/** The tag after the write; after `TAP_AGAIN_MS` with no answer, asks for a second tap and keeps waiting. */
async function readBack(session: NfcSession, onStep: (step: WriteStep) => void, signal?: AbortSignal): Promise<Reading> {
  const next = session.nextReading(signal)
  onStep('reading_back')
  const timer = setTimeout(() => onStep('tap_again'), TAP_AGAIN_MS)
  try {
    return await next
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Writes a backup's records back to its tag (`restore`), itself backed up first. The page
 * checks the read-back with `sameRecords` and posts it as a read (R6); it binds nothing.
 */
export function restoreBackup(options: {
  session: NfcSession
  tagId: string
  backupId: string
  onStep: (step: WriteStep) => void
  onClaimed?: () => void
  signal?: AbortSignal
}): Promise<WriteOutcome> {
  const { tagId, backupId, ...rest } = options
  return writeWithBackup({
    ...rest,
    reason: 'restore',
    prepare: (now, fresh) => restoreTagBackup(tagId, backupId, { uid: now.uid, records: now.records, backup_id: fresh }).then((answer) => answer.records),
    confirm: async (_now, _backup, _sent, back) => {
      // The read back is no tap: no scan history row (#8148).
      await postTagRead({ uid: back.uid, records: back.records, tap: false })
      return null
    },
  })
}

const BAMBU_REFUSED = 'This is a Bambu (MIFARE Classic) tag. ScadBuddy never writes one. Nothing was written.'
const OTHER_TAG_REFUSED = 'A different tag answered. Hold the tag shown here to the phone. Nothing was written.'

/** Whether ScadBuddy may write this tag at all: never a Bambu spool's MIFARE Classic tag. */
export const clearable = (tag: TagRow) => !tag.kind.startsWith('bambu')

/**
 * #8362 — clears a tag: backs it up (`clear`), writes one empty NDEF record, reads it back, then
 * takes the tag off its spool (`cleared`) and reads it as ScadBuddy sees it now (no tap, #8148).
 * Only the tag shown (`tag`) is cleared, and never a Bambu tag. Its backups and history stay, so
 * a restore of the backup undoes it.
 */
export function clearTag(options: {
  session: NfcSession
  tag: TagRow
  /** The checked read-back, before the tag is taken off its spool. */
  onChecked?: (back: Reading) => void
  onStep: (step: WriteStep) => void
  onClaimed?: () => void
  signal?: AbortSignal
}): Promise<WriteOutcome> {
  const { tag, onChecked, ...rest } = options
  return writeWithBackup({
    ...rest,
    reason: 'clear',
    precheck: async (now) => {
      if (!clearable(tag) || now.tag?.tech === 'mifare_classic') throw new WriteRefusedError(BAMBU_REFUSED)
      if (cleanTagUid(now.uid) !== tag.tag_uid) throw new WriteRefusedError(OTHER_TAG_REFUSED)
    },
    prepare: async () => [],
    confirm: async (_now, _backup, _sent, back) => {
      onChecked?.(back)
      try {
        await unbindTag(tag.id, 'cleared')
      } catch (cause) {
        // A tag on no spool any more (unbound meanwhile) is cleared all the same.
        if (!(cause instanceof ApiError && cause.status === 404)) throw new WriteRefusedError(`The tag is empty now, but it is still on its spool: ${detailOf(cause)}. Clear it again.`)
      }
      return postTagRead({ uid: back.uid, records: back.records, tap: false })
    },
  })
}
