import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { StepCard } from './StepCard'
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
    <StepCard title={`Quality — ${size} mm nozzle`}>
      <div className="grid gap-1.5">
        <Label htmlFor="quality-process" className="text-xs font-normal text-muted-foreground">
          Process
        </Label>
        <Select value={selectedProcess} onValueChange={(next) => onChange({ tier: null, processName: next })}>
          <SelectTrigger id="quality-process" aria-label="Process" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {processes.map((name) => (
              <SelectItem key={name} value={name}>
                {name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </StepCard>
  )
}
