import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import type { NozzleChoice } from '../../api/types'

type Flow = NozzleChoice['flow']

const FLOWS: readonly { flow: Flow; label: string }[] = [
  { flow: 'high_flow', label: 'High Flow' },
  { flow: 'standard', label: 'Standard' },
]

type Props = {
  value: Flow
  onChange: (flow: Flow) => void
}

/**
 * #2166 — Simple mode's one nozzle choice: High Flow or Standard, on both sides. The
 * dialog opens on what is mounted (#1895); the size comes from the mounted nozzles, and
 * ScadBuddy plans which filament prints from which nozzle.
 */
export function FlowChoice({ value, onChange }: Props) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
      <span id="print-flow" className="text-sm font-medium">
        Nozzle
      </span>
      <ToggleGroup
        type="single"
        variant="outline"
        spacing={0}
        value={value}
        // Choosing the chosen one again turns nothing off: there is always a flow.
        onValueChange={(next) => next && onChange(next as Flow)}
        aria-labelledby="print-flow"
      >
        {FLOWS.map(({ flow, label }) => (
          <ToggleGroupItem key={flow} value={flow} data-testid={`flow-${flow}`} className="px-3">
            {label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
    </div>
  )
}
