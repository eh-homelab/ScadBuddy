import { useCallback, useEffect, useState, type Dispatch, type SetStateAction } from 'react'
import type { PrintOptions } from '../api/types'
import { lanClearedNotice, lanHonours, type OptionName, type OptionValue } from './printOptions'

const honoured = (name: string, value: unknown) => lanHonours(name as OptionName, (value ?? null) as OptionValue)

/**
 * #5352 — on a native route the LAN refuses a calibration value it cannot honour, so one
 * the print set (before the route turned native, or from a seed) is dropped from
 * `options`. #5602 — the values dropped are kept, for the dialog's notice
 * (`lanClearedNotice`), and a switch to another printer that is not native puts each
 * back, unless the user chose that option again since (then the notice no longer names
 * it either). `printer` names the chosen
 * printer: the same printer reading as not native for a moment (its route read again)
 * puts nothing back. `clear` forgets them, for when the options are reset.
 */
export function useLanCleared(
  native: boolean,
  printer: string,
  options: PrintOptions,
  setOptions: Dispatch<SetStateAction<PrintOptions>>,
): { notice: string | null; clear: () => void } {
  const [cleared, setCleared] = useState<PrintOptions>({})
  /** The printer chosen when they were cleared: only another printer puts them back. */
  const [clearedOn, setClearedOn] = useState<string | null>(null)
  const held = Object.keys(cleared).length > 0
  useEffect(() => {
    if (!native) {
      if (!held || printer === clearedOn) return
      setOptions((current) => ({ ...cleared, ...current }))
      setCleared({})
      setClearedOn(null)
      return
    }
    // On another LAN printer now: that one is the printer they were cleared on.
    if (held && clearedOn !== printer) setClearedOn(printer)
    // An option the user set since is theirs: it never comes back, nor is it named.
    const chosen = Object.keys(cleared).filter((name) => options[name as OptionName] !== undefined)
    if (chosen.length > 0) {
      setCleared((before) => Object.fromEntries(Object.entries(before).filter(([name]) => !chosen.includes(name))))
    }
    const dropped = Object.entries(options).filter(([name, value]) => !honoured(name, value))
    if (dropped.length === 0) return
    setCleared((before) => ({ ...before, ...Object.fromEntries(dropped) }))
    setClearedOn(printer)
    setOptions(Object.fromEntries(Object.entries(options).filter(([name, value]) => honoured(name, value))))
  }, [native, printer, options, setOptions, cleared, clearedOn, held])
  const clear = useCallback(() => {
    setCleared({})
    setClearedOn(null)
  }, [])
  return { notice: native ? lanClearedNotice(cleared) : null, clear }
}
