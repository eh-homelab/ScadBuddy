import { useState, type InputHTMLAttributes } from 'react'

/** The draft as a number, or undefined while it is empty or partial (`-`, `.`, `1e`). */
function parse(draft: string): number | undefined {
  if (draft.trim() === '') return undefined
  const next = Number(draft)
  return Number.isFinite(next) ? next : undefined
}

/**
 * #1323 — a number box that keeps what is typed as a draft. Only a draft that parses
 * is committed, so clearing the field or typing `-` never sends 0; leaving it (blur
 * or Enter) with nothing parseable shows the last committed value again.
 */
export function NumberInput({
  value,
  onCommit,
  ...rest
}: {
  value: number
  onCommit: (next: number) => void
} & Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'value' | 'onChange' | 'onBlur' | 'onKeyDown'>) {
  const [draft, setDraft] = useState(String(value))
  const [shown, setShown] = useState(value)
  // A value from outside (a preset, a reset, the slider, rounding) replaces the draft
  // unless the draft already says it.
  if (shown !== value) {
    setShown(value)
    if (parse(draft) !== value) setDraft(String(value))
  }

  const restore = () => {
    if (parse(draft) === undefined) setDraft(String(value))
  }

  return (
    <input
      {...rest}
      type="number"
      value={draft}
      onChange={(event) => {
        setDraft(event.target.value)
        const next = parse(event.target.value)
        if (next !== undefined) onCommit(next)
      }}
      onBlur={restore}
      onKeyDown={(event) => {
        if (event.key === 'Enter') restore()
      }}
    />
  )
}
