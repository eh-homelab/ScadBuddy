const MAX_COPIES = 50

type Props = {
  /** `null` until the user sets it, so a remembered quantity is not overridden (#124). */
  value: number | null
  /** #145 — what an unset box queues. */
  remembered: number | null
  onChange: (next: number | null) => void
}

export function CopiesField({ value, remembered, onChange }: Props) {
  return (
    <div className="flex items-center gap-3">
      <label htmlFor="print-copies" className="text-[13px] text-ink">
        Copies
      </label>
      <input
        id="print-copies"
        type="number"
        min={1}
        max={MAX_COPIES}
        value={value ?? ''}
        placeholder={String(remembered ?? 1)}
        onChange={(event) =>
          onChange(event.target.value === '' ? null : Math.max(1, Number(event.target.value)))
        }
        className="sb-field sb-num w-20 text-right"
      />
      {value === null && remembered !== null && (
        <span className="text-[12px] text-muted" data-testid="remembered-copies">
          <span className="sb-num">{remembered}</span> remembered — leave blank to use it
        </span>
      )}
    </div>
  )
}
