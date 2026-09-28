type Props = {
  value: boolean
  onToggle: () => void
}

/**
 * Simple offers the tiers; Advanced adds the full process list, per-side flow and a
 * per-slot filament preset override.
 */
export function AdvancedSwitch({ value, onToggle }: Props) {
  return (
    <div className="flex items-center gap-3">
      <span id="print-advanced" className="text-[13px] text-ink">
        Advanced
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={value}
        aria-labelledby="print-advanced"
        aria-describedby="print-advanced-help"
        onClick={onToggle}
        className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors ${
          value ? 'border-accent bg-accent' : 'border-line-strong bg-surface-3'
        }`}
      >
        <span
          className={`absolute top-[2px] size-3.5 rounded-full transition-[left] ${
            value ? 'left-[18px] bg-accent-ink' : 'left-[2px] bg-muted'
          }`}
        />
      </button>
      <span id="print-advanced-help" className="text-[12px] text-faint">
        Pick any process, the flow per side and a preset per slot.
      </span>
    </div>
  )
}
