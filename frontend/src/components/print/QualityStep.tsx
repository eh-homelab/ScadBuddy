import type { TierOption } from '../../api/types'

type Tier = 'fine' | 'standard' | 'draft'

type Props = {
  size: string
  tiers: TierOption[]
  processes: string[]
  advanced: boolean
  tier: Tier | null
  processName: string | null
  onChange: (next: { tier: Tier | null; processName: string | null }) => void
}

const TIER_LABELS: Record<Tier, string> = {
  fine: 'Fine',
  standard: 'Standard',
  draft: 'Draft',
}

/** The process name's leading `0.xxmm`, which is the layer height it slices at. */
function layerHeight(processName: string): string {
  const match = /^(0\.\d+mm)/.exec(processName)
  return match?.[1] ?? processName
}

export function QualityStep({ size, tiers, processes, advanced, tier, processName, onChange }: Props) {
  const selectedProcess = processName ?? tiers.find((t) => t.tier === tier)?.process_name ?? ''

  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
      <legend className="px-1 text-[13px] text-ink">Quality — {size} mm nozzle</legend>
      {!advanced && (
        <div role="radiogroup" aria-label="Quality" className="mt-1.5 flex flex-wrap gap-3">
          {tiers.map((option) => (
            <label
              key={option.tier}
              className="flex cursor-pointer items-center gap-1.5 text-[12px] text-ink"
            >
              <input
                type="radio"
                name="quality-tier"
                checked={!processName && tier === option.tier}
                onChange={() => onChange({ tier: option.tier, processName: null })}
                className="accent-[var(--sb-accent)]"
              />
              {TIER_LABELS[option.tier]} — {layerHeight(option.process_name)}
            </label>
          ))}
        </div>
      )}
      {advanced && (
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
      )}
    </fieldset>
  )
}
