import { useLayoutEffect, useRef, useState } from 'react'
import type { Param } from '../../api/types'
import { textLength } from '../../lib/params'
import { Field } from './Field'

/**
 * An edit cut to `max` characters, counted in code points (#920, #1449). The edit is
 * the run between the common prefix and suffix of `before` and `after`; only as much of
 * what it inserted as fits is kept, so the name's other letters are never trimmed.
 * `caret` is where the kept run ends, in UTF-16 units, or null when the edit fits. An
 * edit that does not lengthen the value always fits, even past `max`.
 */
function fitEdit(before: string, after: string, max: number): { value: string; caret: number | null } {
  const a = Array.from(before)
  const b = Array.from(after)
  if (b.length <= max || b.length <= a.length) return { value: after, caret: null }
  let prefix = 0
  while (prefix < a.length && a[prefix] === b[prefix]) prefix++
  let suffix = 0
  while (suffix < a.length - prefix && a[a.length - 1 - suffix] === b[b.length - 1 - suffix]) suffix++
  const inserted = b.slice(prefix, b.length - suffix).slice(0, Math.max(0, max - prefix - suffix))
  const head = a.slice(0, prefix).join('') + inserted.join('')
  return { value: head + a.slice(a.length - suffix).join(''), caret: head.length }
}

export function TextWidget({
  param,
  value,
  onChange,
}: {
  param: Param
  value: string
  onChange: (next: string) => void
}) {
  const id = `p-${param.name}`
  const maxLength = param.max_length ?? undefined
  const length = textLength(value)
  const input = useRef<HTMLInputElement>(null)
  // Where the caret goes once a cut edit is written back: React's write-back of the
  // controlled value moves it to the end.
  const caret = useRef<number | null>(null)
  // The value when an IME composition started; null when none is in progress.
  const composedFrom = useRef<string | null>(null)
  // Counts refusals, so that a second refusal in a row still re-renders and the caret
  // is put back.
  const [refusals, setRefusals] = useState(0)

  useLayoutEffect(() => {
    if (caret.current === null || !input.current) return
    input.current.setSelectionRange(caret.current, caret.current)
    caret.current = null
  })

  const apply = (before: string, after: string) => {
    if (maxLength === undefined) return onChange(after)
    const fit = fitEdit(before, after, maxLength)
    if (fit.caret === null) {
      setRefusals(0)
    } else {
      caret.current = fit.caret
      setRefusals((n) => n + 1)
    }
    if (fit.value !== value) onChange(fit.value)
  }

  const readout =
    maxLength !== undefined ? (
      <span id={`${id}-count`} className={length >= maxLength ? 'text-accent' : undefined}>
        {length}/{maxLength}
      </span>
    ) : undefined

  return (
    <Field id={id} label={param.caption ?? param.name} name={param.name} readout={readout}>
      <input
        ref={input}
        id={id}
        type="text"
        value={value}
        aria-describedby={maxLength !== undefined ? `${id}-count` : undefined}
        // Not the maxLength attribute: it counts UTF-16 units, so an emoji takes two (#920).
        onChange={(event) => {
          // Mid-composition states are not cut, or the IME would abort; the
          // composition is cut when it ends (#1453).
          if (composedFrom.current !== null || (event.nativeEvent as InputEvent).isComposing) {
            onChange(event.target.value)
          } else {
            apply(value, event.target.value)
          }
        }}
        onCompositionStart={() => {
          composedFrom.current = value
        }}
        onCompositionEnd={(event) => {
          const before = composedFrom.current ?? value
          composedFrom.current = null
          apply(before, event.currentTarget.value)
        }}
        className="sb-field"
      />
      {maxLength !== undefined && (
        <p role="status" className="mt-1 text-[12px] text-warn empty:mt-0">
          {refusals > 0 ? `At most ${maxLength} characters.` : ''}
        </p>
      )}
    </Field>
  )
}
