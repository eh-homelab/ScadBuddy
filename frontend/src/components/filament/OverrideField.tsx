import { Undo2Icon } from 'lucide-react'
import type { ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'

export interface OverrideFieldProps {
  /** The spool field's name (`overridden` lists these). */
  name: string
  label: string
  /** What the spool holds itself, as text; `''` while it takes its type's value. */
  value: string
  /** What the field is after the type's value is taken, as text: the placeholder. */
  effective?: string
  /** The saved spool sets this field (`overridden.includes(name)`). */
  overridden: boolean
  /** The spool has a type. Without one every value is its own, so nothing is "set on this spool". */
  hasType: boolean
  onChange: (value: string) => void
  /** Sends `null` for the field: the spool takes its type's value again. */
  onReset: () => void
  hint?: string
  inputMode?: 'decimal' | 'numeric'
  /** Another control than a text input (a select, the colour field), which then owns `value`. */
  children?: ReactNode
  /** The control is several inputs (the colour field): they are a group the label names. */
  group?: boolean
  className?: string
}

/**
 * #6262 — one spool field that may override its type's. A text input whose placeholder is
 * the effective value, or `children` for a richer control. When the saved spool overrides
 * the field it carries the mark "Set on this spool" and "Reset to type", which the page
 * answers with a PATCH of `null` for that field alone (backend `SpoolPatch`: `null` on an
 * override clears it).
 */
export function OverrideField({ name, label, value, effective, overridden, hasType, onChange, onReset, hint, inputMode, children, group, className }: OverrideFieldProps) {
  const id = `spool-${name}`
  const marked = overridden && hasType
  return (
    <div className={cn('flex min-w-0 flex-col gap-1', className)} data-field={name} data-overridden={marked ? 'true' : undefined}>
      <div className="flex min-h-6 items-center justify-between gap-2">
        <Label id={`${id}-label`} htmlFor={group ? undefined : id} className="text-[13px]">
          {label}
        </Label>
        {marked && (
          <span className="flex items-center gap-1">
            <span className="rounded-sm bg-primary/10 px-1.5 py-0.5 text-[11px] font-medium text-primary">Set on this spool</span>
            <Button type="button" size="xs" variant="ghost" aria-label={`Reset ${label} to type`} onClick={onReset}>
              <Undo2Icon /> Reset to type
            </Button>
          </span>
        )}
      </div>
      {group ? (
        <div role="group" aria-labelledby={`${id}-label`}>
          {children}
        </div>
      ) : (
        (children ?? <Input id={id} inputMode={inputMode} value={value} placeholder={hasType ? effective : undefined} onChange={(e) => onChange(e.target.value)} />)
      )}
      {hint && <p className="text-[12px] text-muted-foreground">{hint}</p>}
    </div>
  )
}
