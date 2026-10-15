import type { Param } from '../../api/types'
import { paramLabel } from '../../lib/params'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'

export function BooleanWidget({
  param,
  value,
  onChange,
}: {
  param: Param
  value: boolean
  onChange: (next: boolean) => void
}) {
  const id = `p-${param.name}`

  return (
    <div className="flex items-center justify-between gap-3 px-3 py-3">
      <Label htmlFor={id} className="leading-snug">
        {paramLabel(param)}
      </Label>
      <Switch id={id} checked={value} onCheckedChange={onChange} />
    </div>
  )
}
