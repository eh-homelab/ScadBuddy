/**
 * The bed plate's swap reminder, one sentence for `PlateStep` and the summary row (#6885
 * review). A null printer name reads "printer", as `backend/scadbuddy/bambuddy/hardware.py`
 * `plate_warning` does (`printer_name or "printer"`).
 */
export function plateSwapReminder(printerName: string | null, lastBedType: string, bedType: string): string {
  return `The ${printerName ?? 'printer'}'s last print used ${lastBedType}. Swap to ${bedType} before this starts.`
}
