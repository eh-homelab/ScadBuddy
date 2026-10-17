import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'

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
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <Switch
        id="print-advanced"
        checked={value}
        onCheckedChange={onToggle}
        disabled={disabled}
        aria-describedby="print-advanced-help"
      />
      <Label htmlFor="print-advanced">Advanced</Label>
      <span id="print-advanced-help" className="text-xs text-muted-foreground">
        Choose the nozzle, process, plate, options, project and copies.
      </span>
    </div>
  )
}
