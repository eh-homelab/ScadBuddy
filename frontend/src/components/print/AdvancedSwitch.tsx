type Props = {
  value: boolean
  onToggle: () => void
  /** A project create is in flight; switching would unmount the picker running it. */
  disabled?: boolean
}

/**
 * Simple shows only what the user has to choose (#768): the printer when there is more
 * than one, the spools and Print. Advanced adds the nozzles with per-side flow, the full
 * process list, the plate type, the print options, the project, copies and a per-slot
 * filament preset override; Simple sends their defaults.
 */
export function AdvancedSwitch({ value, onToggle, disabled }: Props) {
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
        disabled={disabled}
        className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors disabled:opacity-50 ${
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
        Choose the nozzle, process, plate, options, project and copies.
      </span>
    </div>
  )
}
