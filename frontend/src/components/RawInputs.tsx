import type { JsonObject } from '../lib/inputs'
import { Textarea } from '@/components/ui/textarea'

type Props = {
  inputs: JsonObject
  error: string
  /** 12 by default; fewer where a capped row holds it (the page slot, #1811), so the textarea scrolls, not its row too. */
  rows?: number
  /**
   * Held to 30vh (the page slot's row, #3074): the error and the textarea share it,
   * each scrolling inside itself, so no scrollbar sits inside another, no error is
   * clipped, and on a short frame the textarea shrinks rather than spill over the
   * template below. Down to three lines of text (#3624 review): flex shrink is weighted
   * by basis, so a long error would otherwise squeeze the saved inputs to a sliver. The
   * floor is border-box, so it adds the Textarea's padding and border (#3844 review).
   * Below it, in a frame too short for 30vh to hold three lines (under about 330px),
   * the textarea spills past the alert: readable inputs win over the cap there.
   */
  capped?: boolean
}

/** Saved inputs the template could not migrate (spec 2026-09-27 §8.2), shown read-only. */
export function RawInputs({ inputs, error, rows = 12, capped = false }: Props) {
  return (
    <div
      role="alert"
      className={`raw-inputs flex flex-col gap-2 border-b border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive ${capped ? 'max-h-[30vh]' : ''}`}
    >
      <p className={capped ? 'min-h-0 overflow-auto' : undefined}>
        These saved inputs could not be brought up to this template version: {error}
      </p>
      <Textarea
        readOnly
        rows={rows}
        value={JSON.stringify(inputs, null, 2)}
        aria-label="Saved inputs"
        className={`sb-num field-sizing-fixed text-xs text-foreground ${capped ? 'min-h-[calc(3lh_+_1rem_+_2px)]' : ''}`}
      />
    </div>
  )
}
