import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'

type Props = {
  value: boolean
  onChange: (on: boolean) => void
}

/**
 * #6218 — the plan's `force_colour_match`. On (the default), Bambuddy's queue holds the
 * print until a tray of each slot's colour is loaded; off, it may print a slot in another
 * colour. Remembered with the spools, per model or library file.
 */
export function ColourMatchSwitch({ value, onChange }: Props) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <Switch
        id="print-colour-match"
        checked={value}
        onCheckedChange={onChange}
        aria-describedby="print-colour-match-help"
        data-testid="colour-match"
      />
      <Label htmlFor="print-colour-match">Wait for the right colour</Label>
      <span id="print-colour-match-help" className="text-xs text-muted-foreground">
        Bambuddy&apos;s queue holds the print until each colour is loaded.
      </span>
    </div>
  )
}
