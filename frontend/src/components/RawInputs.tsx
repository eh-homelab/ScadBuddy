import type { JsonObject } from '../lib/inputs'

/** Saved inputs the template could not migrate (spec 2026-09-27 §8.2), shown read-only. */
/** `rows`: fewer where a capped row holds it (the page slot, #1811), so the textarea scrolls, not its row too. */
export function RawInputs({ inputs, error, rows = 12 }: { inputs: JsonObject; error: string; rows?: number }) {
  return (
    <div role="alert" className="raw-inputs">
      <p>These saved inputs could not be brought up to this template version: {error}</p>
      <textarea readOnly rows={rows} value={JSON.stringify(inputs, null, 2)} aria-label="Saved inputs" />
    </div>
  )
}
