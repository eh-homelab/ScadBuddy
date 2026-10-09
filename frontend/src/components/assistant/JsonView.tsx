import { useRef, useState } from 'react'
import { highlightJson, prettyJson } from '../../agent/chat/json'
import { copyText } from '../../lib/clipboard'

/**
 * #782/#886 — a tool call's arguments or raw result: JSON pretty-printed with 2-space
 * indentation and highlighted by token, with a Copy button. Text that does not parse
 * (a result cut short, plain words) is shown exactly as it is.
 */

export function JsonView({ label, value, testId }: { label: string; value: unknown; testId?: string }) {
  const pretty = prettyJson(value)
  const shown = pretty ?? String(value)
  const pre = useRef<HTMLPreElement>(null)
  const [copied, setCopied] = useState(false)
  return (
    <div data-testid={testId}>
      <div className="flex items-center gap-2">
        <span className="text-[11.5px] text-muted">{label}</span>
        <button
          type="button"
          className="ml-auto text-[11px] text-muted underline hover:text-ink"
          aria-label={`Copy ${label.toLowerCase()}`}
          onClick={() => {
            void copyText(shown, pre.current).then(setCopied)
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre
        ref={pre}
        className="mt-1 max-h-72 overflow-auto rounded-[4px] bg-bg px-2 py-1.5 font-mono text-[11px] whitespace-pre-wrap break-words text-muted"
      >
        {pretty === undefined ? shown : highlightJson(pretty)}
      </pre>
    </div>
  )
}
