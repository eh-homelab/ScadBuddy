import { useRef, useState } from 'react'
import { highlightJson, prettyJson } from '../../agent/chat/json'
import { copyText } from '../../lib/clipboard'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'

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
        <span className="text-[11.5px] text-muted-foreground">{label}</span>
        <Button
          variant="link"
          size="xs"
          className="ml-auto h-auto px-0 text-[11px] text-muted-foreground"
          aria-label={`Copy ${label.toLowerCase()}`}
          onClick={() => {
            void copyText(shown, pre.current).then(setCopied)
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
      <ScrollArea className="mt-1 rounded-md bg-background [&>[data-slot=scroll-area-viewport]]:max-h-72">
        <pre
          ref={pre}
          className="px-2 py-1.5 font-mono text-[11px] break-words whitespace-pre-wrap text-muted-foreground"
        >
          {pretty === undefined ? shown : highlightJson(pretty)}
        </pre>
      </ScrollArea>
    </div>
  )
}
