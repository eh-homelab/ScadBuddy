import { useState } from 'react'
import type { PartSettings, PartSettingsMap, PrintCheck, PrintRunRequest } from '../api/types'
import { sourceApi, sourceKey, type PrintSource } from './printSource'
import { ANALYSIS_DEBOUNCE_MS } from './useAnalysis'
import { useAsync } from './useAsync'
import { useDebounced } from './useDebounced'
import { useLatest } from './useLatest'

interface Answer {
  source: string | undefined
  request: string
  /** #5627 — the check asked for the process's part settings. */
  processValues: boolean
  verdict: PrintCheck
}

export interface PrintCheckState {
  /** The newest verdict for this source; it stays on screen while a re-check reads. */
  verdict: PrintCheck | null
  /** Whether `verdict` answers the request as it stands now, not an earlier one. */
  current: boolean
  /** The request as it stands now, serialised once: a key for what was asked, or `null`. */
  request: string | null
  error: Error | undefined
  /** Read the verdict again, as after a failed read. */
  reload: () => void
  /**
   * #5627 — a check of this source read the process's part settings for the choices as
   * they stand now. Only the request's own `part_settings` may differ: they do not change
   * the process's values, and a field typed in must stay enabled. A later check that
   * failed does not drop them (#5686); another process's values are never used. It is
   * also true for an answer that asked and got no values (`process_part_settings: null`),
   * so the controls turn on, empty; the next check asks again for that case (#5698).
   */
  processValuesRead: boolean
  /** #5627 — the process's part settings, only while `processValuesRead`; else `null`. */
  processPartSettings: PartSettings | null
  /**
   * #5627, #5626 — per slot a template sets, the process's values with the template's
   * laid over them (`slot_part_settings`), only while `processValuesRead`; else `null`.
   */
  slotPartSettings: PartSettingsMap | null
  /** #5627 — the keys of `processPartSettings` rounded to fit a control (`process_inexact`). */
  processInexact: readonly string[]
}

/** The request without its `part_settings`: what decides the process's values. */
function processKey(request: string | null): string | null {
  if (request === null) return null
  const { part_settings: _ignored, ...rest } = JSON.parse(request) as PrintRunRequest
  return JSON.stringify(rest)
}

/**
 * #755, #760 — what the run would refuse for the print the dialog would send, read
 * before Print (`POST …/check`, the run's own pre-upload refusals). The choices
 * settle for as long as the analyzers' do before it is read. `request: null` reads
 * nothing. `processValues` (#5627) asks for the process's part settings too; asking
 * reads again, though the request is the same. They are asked for only until a check
 * has read them for this source and `processKey` (#5686): a settled edit of
 * `part_settings` alone does not read the preset again. A check that asked and got none
 * (`process_part_settings: null`: the preset could not be read, or the check refused
 * first) does not count, so the next check asks again. A dialog that stops asking
 * (it closed, its source takes no settings per part, or its choices are not loaded)
 * drops what was read, so it reads the preset again when it asks again.
 */
export function usePrintCheck(
  source: PrintSource | undefined,
  request: PrintRunRequest | null,
  processValues = false,
): PrintCheckState {
  const latest = useLatest(source)
  const own = sourceKey(source)
  const wanted = request === null ? null : JSON.stringify(request)
  const key = useDebounced(wanted, ANALYSIS_DEBOUNCE_MS)
  // The newest answer to a check that asked for the process's values (#5686). Read
  // through a ref, so its arrival does not start another check.
  const [read, setRead] = useState<Answer | null>(null)
  // #5698: no values are asked for while the dialog is closed (its controls are not
  // shown), so what was read is dropped then, and the dialog reads the preset again
  // when it opens again: a preset edited in Bambuddy meanwhile is picked up. The same
  // happens while it is open, when it stops asking because the source takes no settings
  // per part (a sliced file) or its choices are not loaded yet (`PrintPicker`
  // `takesParts`); the next check that asks reads the preset once more.
  if (!processValues && read !== null) setRead(null)
  const readNow = useLatest(read)
  const state = useAsync<Answer | null>(async () => {
    const current = latest.current
    if (key === null || !current) return null
    const known = readNow.current
    // Values the check could not read are no values: ask again, as each check did before.
    const has =
      known !== null &&
      known.source === own &&
      known.verdict.process_part_settings != null &&
      processKey(known.request) === processKey(key)
    const asks = processValues && !has
    const verdict = await sourceApi(current).check(JSON.parse(key) as PrintRunRequest, asks)
    return { source: own, request: key, processValues: asks, verdict }
  }, [own, key, processValues])

  // Kept across re-checks of the same source, so a change of choice does not blank the
  // verdict while the new one is on its way.
  const [kept, setKept] = useState<Answer | null>(null)
  if (state.data && state.data !== kept) setKept(state.data)
  // Only this source's: a late answer for a source left behind never replaces it.
  if (state.data?.processValues && state.data.source === own && state.data !== read) setRead(state.data)
  const answer = kept?.source === own && wanted !== null ? kept : null
  // Only for the process the choices name now: another nozzle or printer is another process.
  const values =
    read !== null && read.source === own && wanted !== null && processKey(read.request) === processKey(wanted) ? read : null
  const processRead = values !== null

  return {
    verdict: state.error ? null : (answer?.verdict ?? null),
    current: !state.error && answer?.request === wanted,
    request: wanted,
    error: state.error,
    reload: state.reload,
    processValuesRead: processRead,
    processPartSettings: values?.verdict.process_part_settings ?? null,
    processInexact: values?.verdict.process_inexact ?? [],
    slotPartSettings: (values?.verdict.slot_part_settings as PartSettingsMap | null | undefined) ?? null,
  }
}
