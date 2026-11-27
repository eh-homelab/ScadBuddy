import { useRef, useState } from 'react'
import { ApiError } from '../../../api/client'
import { resolveCode } from '../../../api/filament'
import { Input } from '@/components/ui/input'
import type { InputField } from '../../../lib/flowInputs'

const KIND_WORD: Record<string, string> = { location: 'location', spool: 'spool' }
const article = (word: string) => (/^[aeiou]/i.test(word) ? 'an' : 'a')

/** Why a resolved code does not fit what the field expects, or null. */
function misfit(found: { kind: string; code: string }, expects: InputField['expects']): string | null {
  const kinds = expects?.kinds
  if (kinds && kinds.length > 0 && !kinds.includes(found.kind)) {
    const words = kinds.map((kind) => KIND_WORD[kind] ?? kind)
    return `Expected ${words.map((word) => `${article(word)} ${word}`).join(' or ')}`
  }
  const codes = expects?.codes
  if (codes && codes.length > 0 && !codes.includes(found.code)) return `Expected ${codes.join(' or ')}`
  return null
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
}: {
  id?: string
  expects?: InputField['expects']
  value: string
  onChange: (next: string) => void
  autoFocus?: boolean
  common?: Record<string, unknown>
}) {
  const [looked, setLooked] = useState<Looked>(null)
  // What the person typed; the answer is only a code the lookup accepted.
  const [text, setText] = useState(value)
  // Counts edits and lookups: a result that is not the latest is dropped.
  const latest = useRef(0)

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

  return (
    <div className="flex flex-col gap-1.5">
      <Input
        {...common}
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
          if (looked === null && value.trim()) void look(text)
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
