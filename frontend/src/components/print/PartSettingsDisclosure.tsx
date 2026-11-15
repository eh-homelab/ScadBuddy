import { useMemo } from 'react'
import type { PartSettings } from '../../api/types'
import { SlicerSettingsEditor } from './SlicerSettingsEditor'

type Props = {
  slotId: number
  /** This print's settings for the slot's parts; each unset one is the slot's default. */
  value: PartSettings | undefined
  /**
   * What the slot prints with when this print sets nothing: the process's values with the
   * template's `part_print_settings` (#5626) for the slot laid over them (the check's
   * `slot_part_settings`, else `process_part_settings`); `null` while unknown.
   */
  baseline: PartSettings | null
  /**
   * The default a setting is compared against (`comparedBaseline`): `baseline` without a
   * value the check rounded to fit, which is then still a change. `baseline` when omitted.
   */
  compared?: PartSettings | null
  /** #2998 — every value of the resolved process preset, the list's other keys; `null` while unknown. */
  processValues?: Record<string, unknown> | null
  /** Why the process's values could not be read. */
  unread?: string | null
  loading?: boolean
  /** Held until a check has read the process's values. */
  disabled?: boolean
  /** The disclosure opened: the dialog then asks for the process's values. */
  onOpen?: () => void
  onChange: (next: PartSettings) => void
}

/**
 * #5627, #2998 — "Settings for this colour": the slicer settings a print sets on the parts
 * of one filament slot (print-flow spec §4 "Settings per part (#5571)"). The same list as
 * the whole print's "Slicer settings": every key of the process, searchable, with any other
 * key added by name. Each row shows the slot's default (the template's for the slot over
 * the process's value), and only a setting changed from it is sent. Nothing is refused.
 */
export function PartSettingsDisclosure({
  slotId,
  value,
  baseline,
  compared = baseline,
  processValues = null,
  unread = null,
  loading = false,
  disabled = false,
  onOpen,
  onChange,
}: Props) {
  const merged = useMemo(
    () =>
      processValues === null && compared === null
        ? null
        : { ...(processValues ?? {}), ...Object.fromEntries(Object.entries(compared ?? {}).filter(([, v]) => v != null)) },
    [processValues, compared],
  )
  return (
    <SlicerSettingsEditor<PartSettings>
      id={`part-settings-${slotId}`}
      title="Settings for this colour"
      toggleTestId={`part-settings-toggle-${slotId}`}
      value={value}
      baseline={merged}
      unread={unread}
      loading={loading}
      disabled={disabled}
      onOpen={onOpen}
      onChange={onChange}
    />
  )
}
