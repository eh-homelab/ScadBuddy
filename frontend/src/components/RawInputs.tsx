import type { JsonObject } from '../lib/inputs'

type Props = {
  inputs: JsonObject
  error: string
  /** Fewer where a capped row holds it (the page slot, #1811), so the textarea scrolls, not its row too. */
  rows?: number
  /**
   * Held to 30vh (the page slot's row, #3074): only the error scrolls, beside the
   * textarea's own scroll, so no scrollbar sits inside another and no error is clipped.
   */
  capped?: boolean
}

/** Saved inputs the template could not migrate (spec 2026-09-27 §8.2), shown read-only. */
export function RawInputs({ inputs, error, rows = 12, capped = false }: Props) {
  return (
    <div role="alert" className={capped ? 'raw-inputs flex max-h-[30vh] flex-col' : 'raw-inputs'}>
      <p className={capped ? 'min-h-0 overflow-auto' : undefined}>
        These saved inputs could not be brought up to this template version: {error}
      </p>
      <textarea
        readOnly
        rows={rows}
        value={JSON.stringify(inputs, null, 2)}
        aria-label="Saved inputs"
        className={capped ? 'shrink-0' : undefined}
      />
    </div>
  )
}
