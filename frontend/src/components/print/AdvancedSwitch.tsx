import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'

type Props = {
  value: boolean
  onToggle: () => void
  /** A project create is in flight; switching would unmount the picker running it. */
  disabled?: boolean
  /** #3818 — a native print is filed in a folder: projects are Bambuddy's (D6). */
  native?: boolean
}

/**
 * Simple shows what the user has to choose (#768): the summary row (the printer, the
 * nozzle, the bed plate and the quality, #6885), the spools and Print. Advanced adds the
 * side and rack choices, supports, the prime tower, the print sequence, the print
 * options, the project, copies, a per-slot filament preset override and any slicer
 * setting, in sections; Simple sends their defaults.
 */
export function AdvancedSwitch({ value, onToggle, disabled, native = false }: Props) {
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
        Choose the sides, supports, prime tower, options, {native ? 'folder' : 'project'}, copies and slicer
        settings.
      </span>
    </div>
  )
}
