import type { JsonObject } from '../lib/inputs'

/** Saved inputs the template could not migrate (spec 2026-09-27 §8.2), shown read-only. */
export function RawInputs({ inputs, error }: { inputs: JsonObject; error: string }) {
  return (
    <div role="alert" className="raw-inputs">
      <p>These saved inputs could not be brought up to this template version: {error}</p>
      <textarea readOnly rows={12} value={JSON.stringify(inputs, null, 2)} aria-label="Saved inputs" />
    </div>
  )
}
