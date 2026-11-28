import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../../../api/client'
import { resolveCode } from '../../../api/filament'
import { Input } from '@/components/ui/input'
import type { InputField } from '../../../lib/flowInputs'
import { misfit } from '../../../lib/scanMisfit'

/** #8062 — a code scanned elsewhere (a station's own field) for the field named `field`; `n` counts the scans. */
export interface ScanOffer {
  field: string
  code: string
  n: number
}

type Looked = { kind: 'ok'; text: string } | { kind: 'no'; text: string } | { kind: 'busy' } | null

/**
 * #8062 — a code, typed or scanned (a scanner types it and presses Enter). It is looked up
 * with `resolveCode` and the field shows the name it finds, or says why it does not fit.
 * The answer is the code the server knows it by.
 */
export function ScanField({
  id,
  expects,
  value,
  onChange,
  autoFocus,
  common,
  offer,
}: {
  id?: string
  expects?: InputField['expects']
  value: string
  onChange: (next: string) => void
  autoFocus?: boolean
  common?: Record<string, unknown>
  offer?: ScanOffer
}) {
  const [looked, setLooked] = useState<Looked>(null)
  // What the person typed; the answer is only a code the lookup accepted.
  const [text, setText] = useState(value)
  // Counts edits and lookups: a result that is not the latest is dropped.
  const latest = useRef(0)
  const input = useRef<HTMLInputElement>(null)

  async function look(typed: string) {
    const code = typed.trim()
    const mine = ++latest.current
    if (!code) return setLooked(null)
    setLooked({ kind: 'busy' })
    try {
      const found = await resolveCode(code)
      if (mine !== latest.current) return
      const why = misfit(found, expects)
      if (why) {
        setLooked({ kind: 'no', text: why })
        return
      }
      onChange(found.code)
      setText(found.code)
      setLooked({ kind: 'ok', text: found.name || found.code })
    } catch (caught) {
      if (mine !== latest.current) return
      const unknown = caught instanceof ApiError && caught.status === 404
      setLooked({ kind: 'no', text: unknown ? 'No thing has this code' : caught instanceof Error ? caught.message : String(caught) })
    }
  }

  // A scan made elsewhere fills this field and looks it up as if it was typed here.
  const offered = offer?.n
  useEffect(() => {
    if (!offer || offered === undefined) return
    setText(offer.code)
    input.current?.focus()
    void look(offer.code)
    // Only a new scan (its number) acts; the offer's other parts come with it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offered])

  return (
    <div className="flex flex-col gap-1.5">
      <Input
        {...common}
        ref={input}
        id={id}
        aria-invalid={looked?.kind === 'no' ? true : common?.['aria-invalid'] ? true : undefined}
        data-widget="scan"
        type="text"
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
        autoFocus={autoFocus}
        value={text}
        className="h-11 font-mono text-base"
        onChange={(event) => {
          latest.current++
          setLooked(null)
          setText(event.target.value)
          onChange('')
        }}
        onKeyDown={(event) => {
          // Enter ends a scan; it never submits the form.
          if (event.key !== 'Enter') return
          event.preventDefault()
          void look(text)
        }}
        onBlur={() => {
          if (looked === null && text.trim()) void look(text)
        }}
      />
      {looked?.kind === 'busy' && (
        <p role="status" className="text-[12px] text-muted-foreground">
          Looking up…
        </p>
      )}
      {looked?.kind === 'ok' && (
        <p role="status" className="text-sm font-medium break-words">
          {looked.text}
        </p>
      )}
      {looked?.kind === 'no' && (
        <p role="alert" className="text-[12px] break-words text-destructive">
          {looked.text}
        </p>
      )}
    </div>
  )
}
