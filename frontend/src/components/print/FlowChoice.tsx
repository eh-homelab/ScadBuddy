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
    <div role="radiogroup" aria-label="Nozzle" className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
      <span className="text-[13px] text-ink">Nozzle</span>
      <div className="flex gap-1.5">
        {FLOWS.map(({ flow, label }) => (
          <button
            key={flow}
            type="button"
            role="radio"
            aria-checked={value === flow}
            onClick={() => onChange(flow)}
            data-testid={`flow-${flow}`}
            className={`rounded-[6px] border px-3 py-1.5 text-[13px] ${
              value === flow ? 'border-accent bg-accent/8 text-ink' : 'border-line text-muted hover:text-ink'
            }`}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  )
}
