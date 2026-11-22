import { SearchIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { activeDialog } from '../../agent/dom'
import { isTyping } from '../../lib/isTyping'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'


/**
 * #6894 — the Spools and Types tabs' text search (`lib/filamentSearch.ts`), kept in the URL
 * as `q=` by the tab. Each keystroke filters at once: the lists are already loaded. "/" puts
 * the cursor in it, as on the other lists, never while typing elsewhere or behind a dialog.
 * Ctrl+K (⌘K) still opens the quick find from inside it (`data-quick-find`).
 */
export function FilamentSearchBox({
  value,
  onChange,
  label,
  placeholder,
  className,
}: {
  value: string
  onChange: (value: string) => void
  label: string
  placeholder: string
  className?: string
}) {
  const input = useRef<HTMLInputElement>(null)
  // Local text, so the cursor never jumps while the URL catches up; a change from outside
  // (back, a link) is taken in.
  const [text, setText] = useState(value)
  const [seen, setSeen] = useState(value)
  if (value !== seen) {
    setSeen(value)
    if (value !== text.trim()) setText(value)
  }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) return
      if (event.defaultPrevented || isTyping(event.target) || activeDialog()) return
      event.preventDefault()
      input.current?.focus()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className={cn('relative min-w-48', className)}>
      <SearchIcon aria-hidden className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
      <Input
        ref={input}
        type="search"
        data-quick-find=""
        aria-label={label}
        placeholder={placeholder}
        value={text}
        onChange={(event) => {
          setText(event.target.value)
          onChange(event.target.value.trim())
        }}
        className="h-7 pl-8 text-[13px]"
      />
    </div>
  )
}
