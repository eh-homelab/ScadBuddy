import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { FilamentProfile, PrintChoices, ProfilePreview, SlotChoice, SpoolOption } from '../api/types'

/**
 * #717 — whether `profile` is for `spool`: its material, and the profile's word in the
 * spool's subtype or slicer preset name. The backend's `ProfileMatch.matches`, which
 * the silk analyzer (SB2001) reads the same way: Bambu's tri-colour silk is subtype
 * `Tri Color` with the preset `Bambu PLA Silk`.
 */
export function profileMatches(
  profile: Pick<FilamentProfile, 'match'>,
  spool: Pick<SpoolOption, 'material' | 'subtype' | 'slicer_filament_name'>,
): boolean {
  if ((spool.material ?? '').trim().toUpperCase() !== profile.match.material.toUpperCase()) return false
  const word = profile.match.word.toLowerCase()
  return (spool.subtype ?? '').toLowerCase().includes(word) || (spool.slicer_filament_name ?? '').toLowerCase().includes(word)
}

export interface ProfileSuggestion {
  profile: FilamentProfile
  /** The plan's slots whose spool the profile is for. */
  slots: { slot_id: number; spool: SpoolOption }[]
  /** Its changes against this print's process preset; null until read, or unreadable. */
  preview: ProfilePreview | null
  /** The preview request failed: the change was not checked against this print. */
  unchecked: boolean
  /** Left on: sent with the print. */
  on: boolean
}

/**
 * #717 — the profiles this print's spools match, each on unless the user switched it
 * off or it cannot apply (its preview says why). Never applied silently: a profile is
 * only sent while it is listed, and listed only while a chosen spool matches it.
 */
export function useFilamentProfiles({
  spools,
  plan,
  choices,
}: {
  spools: SpoolOption[] | undefined
  plan: SlotChoice[]
  choices: PrintChoices | null
}) {
  const [profiles, setProfiles] = useState<FilamentProfile[]>([])
  const [previews, setPreviews] = useState<Record<string, ProfilePreview>>({})
  const [previewFailed, setPreviewFailed] = useState(false)
  const [off, setOff] = useState<ReadonlySet<string>>(new Set())
  /** The only profiles on, once restored from a saved request; `null` is every one on but `off`. */
  const [onlyOn, setOnlyOn] = useState<ReadonlySet<string> | null>(null)
  const reset = useCallback(() => {
    setOff((current) => (current.size === 0 ? current : new Set()))
    setOnlyOn(null)
  }, [])
  /** A saved request's profiles back, before the profiles that match are known: a batch
   * step reopened from its own choices (#2718 review). Stable, as `reset` is. */
  const restore = useCallback((active: readonly string[]) => {
    setOff(new Set())
    setOnlyOn(new Set(active))
  }, [])

  useEffect(() => {
    let live = true
    api.listFilamentProfiles().then(
      (all) => live && setProfiles(all),
      () => {},
    )
    return () => {
      live = false
    }
  }, [])

  const matched = useMemo(() => {
    const byId = new Map((spools ?? []).map((spool) => [spool.spool_id, spool]))
    return profiles
      .map((profile) => ({
        profile,
        slots: plan
          .map((slot) => ({ slot_id: slot.slot_id, spool: slot.spool_id == null ? undefined : byId.get(slot.spool_id) }))
          .filter((slot): slot is { slot_id: number; spool: SpoolOption } => !!slot.spool && profileMatches(profile, slot.spool)),
      }))
      .filter((entry) => entry.slots.length > 0)
  }, [profiles, spools, plan])

  const ids = matched.map((entry) => entry.profile.id)
  const previewKey = choices && ids.length > 0 ? JSON.stringify([choices, ids]) : null
  useEffect(() => {
    if (previewKey === null || choices === null) return
    let live = true
    setPreviews({})
    setPreviewFailed(false)
    api.previewFilamentProfiles(choices, ids).then(
      (shown) => live && setPreviews(Object.fromEntries(shown.map((preview) => [preview.profile.id, preview]))),
      () => live && setPreviewFailed(true),
    )
    return () => {
      live = false
    }
    // `previewKey` is `choices` and `ids` by value.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewKey])

  const suggestions: ProfileSuggestion[] = matched.map(({ profile, slots }) => {
    const preview = previews[profile.id] ?? null
    return {
      profile,
      slots,
      preview,
      unchecked: preview === null && previewFailed,
      on: (onlyOn ? onlyOn.has(profile.id) : !off.has(profile.id)) && preview?.applicable !== false,
    }
  })

  return {
    suggestions,
    /** The ids to send: every suggestion left on. One not yet previewed is sent; if it
     * cannot apply, the server skips it and the run's warnings say so. */
    active: suggestions.filter((entry) => entry.on).map((entry) => entry.profile.id),
    toggle: (id: string, on: boolean) => {
      const flip = (current: ReadonlySet<string>, add: boolean) => {
        const next = new Set(current)
        if (add) next.add(id)
        else next.delete(id)
        return next
      }
      if (onlyOn) setOnlyOn((current) => current && flip(current, on))
      else setOff((current) => flip(current, !on))
    },
    /** Every profile back on: a switch-off is for one output, as the print sequence and
     * supports are (#2804 review). Stable, so the dialog's reset effect can call it. */
    reset,
    restore,
  }
}
