import { useEffect } from 'react'
import type { TierOption } from '../../api/types'

type Tier = 'fine' | 'standard' | 'draft'

type Props = {
  size: string
  tiers: TierOption[]
  processes: string[]
  tier: Tier | null
  processName: string | null
  onChange: (next: { tier: Tier | null; processName: string | null }) => void
}

/**
 * An Advanced step (#768): Simple mode does not show it, and sends the tier the dialog
 * opened on (this model's last, else Standard). Here the tier shows as its process, and
 * any of Bambu's processes for the size can be chosen instead.
 */
export function QualityStep({ size, tiers, processes, tier, processName, onChange }: Props) {
  const selectedProcess = processName ?? tiers.find((t) => t.tier === tier)?.process_name ?? ''
  // A process this size does not offer (remembered from another size, or gone from
  // Bambuddy) would be sent unseen; fall back to the tier. An empty list is "not read
  // yet", not "nothing offered".
  const stale = processName !== null && processes.length > 0 && !processes.includes(processName)
  useEffect(() => {
    if (stale) onChange({ tier: tier ?? 'standard', processName: null })
  }, [stale, tier, onChange])

  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
      <legend className="px-1 text-[13px] text-ink">Quality — {size} mm nozzle</legend>
      <div className="mt-1.5 flex flex-col gap-1">
        <label htmlFor="quality-process" className="text-[12px] text-muted">
          Process
        </label>
        <select
          id="quality-process"
          aria-label="Process"
          value={selectedProcess}
          onChange={(event) => onChange({ tier: null, processName: event.target.value })}
          className="sb-field"
        >
          {processes.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </div>
    </fieldset>
  )
}
