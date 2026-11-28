import { ApiError } from '../../api/client'
import { problemCode, type MirrorOutcome, type PresetRow } from '../../api/filamentPresets'

/** What a failed write says, and what the page does about it. */
export interface WriteFailure {
  text: string
  /** The server has no writes yet (503 `writes_not_configured`): offer no retry. */
  off: boolean
  /** 503 `preset_write_pending`: the write may still run, so the same key goes again. */
  pending: boolean
  /** 409 `version_moved`: another edit came first. */
  moved: boolean
}

export const WRITES_OFF_TEXT = 'Writes are not on yet on this server.'

const TEXT: Record<string, string> = {
  writes_not_configured: WRITES_OFF_TEXT,
  preset_write_pending: 'The write has not finished yet. Try again. It will not be done twice.',
  version_moved: 'Another edit came first. The preset is loaded again. Make your change again.',
  not_manual: 'Only a manual preset can be edited. Make a copy first.',
  preset_not_found: 'There is no such preset.',
  idempotency_key_reused: 'This form already sent a different change. Close it and open it again.',
  mirror_off: 'The send to Bambuddy is off in Settings.',
  not_configured: 'Bambuddy is not set up in Settings.',
  instance_changed: 'Bambuddy changed. Reset the Bambuddy links in Settings first.',
  not_mirrored: 'Bambuddy owns this preset. ScadBuddy does not send it.',
}

const IDEMPOTENCY_REQUIRED = 'https://scadbuddy.dev/problems/crud/idempotency-key-required'

export function writeFailure(cause: unknown): WriteFailure {
  const code = problemCode(cause)
  const own = code ? TEXT[code] : undefined
  const required = cause instanceof ApiError && cause.problem.type === IDEMPOTENCY_REQUIRED
  const fallback = cause instanceof ApiError ? cause.detail : 'The write failed. Try again.'
  return {
    text: own ?? (required ? 'The server needs a key for this write. Reload the page and try again.' : fallback),
    off: code === 'writes_not_configured',
    pending: code === 'preset_write_pending',
    moved: code === 'version_moved',
  }
}

export const OUTCOME_TEXT: Record<MirrorOutcome, string> = {
  sent: 'Sent to Bambuddy.',
  failed: 'The send failed. The reason is shown with the preset.',
  busy: 'A send is running now. Wait a moment, then send again.',
  in_sync: 'Bambuddy already has this version.',
  pending: 'The send is queued. Bambuddy has not got this version yet.',
}

/** A preset can be sent again only when its last send failed or has not run. */
export const canSendAgain = (row: Pick<PresetRow, 'mirror'>) => row.mirror === 'failed' || row.mirror === 'pending'
