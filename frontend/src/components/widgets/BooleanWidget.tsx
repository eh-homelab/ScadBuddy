import type { Param } from '../../api/types'

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
    <div className="flex items-center justify-between gap-3 px-3 py-2.5">
      <label htmlFor={id} className="text-[13px] text-ink">
        {param.caption ?? param.name}
      </label>
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={value}
        onClick={() => onChange(!value)}
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
    </div>
  )
}
