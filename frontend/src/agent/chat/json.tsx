import type { ReactNode } from 'react'

// #782/#886 — JSON as the assistant panel shows a call's arguments and raw result
// (components/assistant/JsonView.tsx).

/** The JSON `value` is, pretty-printed; a string is parsed first, and undefined when it is not JSON. */
export function prettyJson(value: unknown): string | undefined {
  if (typeof value !== 'string') return JSON.stringify(value, null, 2)
  const trimmed = value.trim()
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return undefined
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2)
  } catch {
    return undefined
  }
}

const TOKEN = /("(?:\\.|[^"\\])*")(\s*:)?|\b(?:true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g

function tokenClass(match: string, key: boolean): string {
  if (key) return 'text-accent'
  if (match.startsWith('"')) return 'text-ok'
  if (match === 'null') return 'text-faint'
  if (match === 'true' || match === 'false') return 'text-warn'
  return 'text-ink'
}

/** Pretty-printed JSON as spans, one class per token kind. */
export function highlightJson(json: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  for (const m of json.matchAll(TOKEN)) {
    const at = m.index
    if (at > last) out.push(json.slice(last, at))
    const key = m[2] !== undefined
    const token = key ? m[1]! : m[0]
    out.push(
      <span key={at} className={tokenClass(token, key)} data-token={key ? 'key' : undefined}>
        {token}
      </span>,
    )
    if (key) out.push(m[2])
    last = at + m[0].length
  }
  if (last < json.length) out.push(json.slice(last))
  return out
}
