type Props = {
  bedTypes: string[]
  value: string
  lastBedType: string | null
  printerName: string | null
  onChange: (next: string) => void
}

/**
 * Fix round 1 — `printerName` is nullable (an eligible printer is not always chosen
 * yet), and reading it straight into the sentence rendered "The 's last print used…".
 * Falls back the way `backend/scadbuddy/bambuddy/hardware.py`'s `plate_warning` does
 * (`printer_name or "printer"`), so the two stay worded the same.
 */
export function PlateStep({ bedTypes, value, lastBedType, printerName, onChange }: Props) {
  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
      <legend className="px-1 text-[13px] text-ink">Plate</legend>
      <div className="mt-1.5 flex flex-col gap-1">
        <label htmlFor="plate-select" className="text-[12px] text-muted">
          Plate
        </label>
        <select
          id="plate-select"
          aria-label="Plate"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="sb-field"
        >
          {bedTypes.map((type) => (
            <option key={type} value={type}>
              {type}
            </option>
          ))}
        </select>
      </div>
      {lastBedType && (
        <p className="mt-1.5 text-[12px] text-muted">Last print used: {lastBedType}</p>
      )}
      {lastBedType && lastBedType !== value && (
        <p role="status" className="mt-1.5 text-[12px] text-muted">
          The {printerName ?? 'printer'}&apos;s last print used {lastBedType}. Swap to {value}{' '}
          before this starts.
        </p>
      )}
    </fieldset>
  )
}
