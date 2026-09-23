import type { ReactNode } from 'react'

/**
 * One parameter row. The caption is the label; the OpenSCAD variable name is shown
 * alongside because it is what ends up in the stored parameter set.
 */
export function Field({
  id,
  label,
  name,
  readout,
  children,
}: {
  id: string
  label: string
  name: string
  readout?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="px-3 py-2.5">
      <div className="mb-1.5 flex items-baseline justify-between gap-3">
        <label htmlFor={id} className="text-[13px] text-ink">
          {label}
        </label>
        <span className="sb-num shrink-0 text-[11px] text-faint">{readout ?? name}</span>
      </div>
      {children}
    </div>
  )
}
