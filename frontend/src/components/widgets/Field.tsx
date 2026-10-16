import type { ReactNode } from 'react'
import { Label } from '@/components/ui/label'

/**
 * One parameter row. The caption is the label; the OpenSCAD variable name is shown
 * alongside because it is what ends up in the stored parameter set.
 */
export function Field({
  id,
  label,
  name,
  readout,
  error,
  children,
}: {
  id: string
  label: string
  name: string
  readout?: ReactNode
  /** #921 — why the value cannot be rendered; the input names it with `aria-describedby={`${id}-error`}`. */
  error?: string | null
  children: ReactNode
}) {
  return (
    <div className="flex flex-col gap-2 px-3 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <Label htmlFor={id} className="leading-snug">
          {label}
        </Label>
        <span className="sb-num shrink-0 text-xs text-muted-foreground">{readout ?? name}</span>
      </div>
      {children}
      {error && (
        <p id={`${id}-error`} role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  )
}
