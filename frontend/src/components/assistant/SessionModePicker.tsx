import { useState } from 'react'
import { ChevronRightIcon } from 'lucide-react'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { SessionMode } from '../../agent/chat/protocol'
import { api } from '../../api/client'
import { useAsync } from '../../lib/useAsync'

const LABEL: Record<SessionMode, string> = { classic: 'Classic', durable: 'Durable' }

/** Radix's Select takes no empty value: "Default" (no mode sent) is this one. */
const DEFAULT = 'default'

/**
 * Plan 5d — the composer's choice of a new chat's mode (spec §6.1), offered only while
 * no session is open: the mode is set when a session starts. "Default" sends none, so
 * the agent's default (Settings → Assistant) applies, and falls back to classic, with
 * a note, when durable cannot run; a picked mode is sent and never falls back. Named
 * "Session mode" rather than the spec's "Advanced": the header has an Advanced switch.
 * The summary shows the pick, so a remembered one is never hidden.
 */
export function SessionModePicker({
  value,
  onChange,
}: {
  value: SessionMode | ''
  onChange: (mode: SessionMode | '') => void
}) {
  // What "Default" means now; a failed read leaves it unnamed.
  const setting = useAsync(() => api.getSessionMode(), [])
  const current = setting.data
  const defaultLabel = !current
    ? 'Default'
    : current.mode === 'durable' && !current.durable_available
      ? 'Default (Durable, Classic for now)'
      : `Default (${LABEL[current.mode]})`
  const unavailable = value === 'durable' && current && !current.durable_available
  const [open, setOpen] = useState(false)
  const picker = (
    <Collapsible open={open} onOpenChange={setOpen} className="mb-1.5 text-[11.5px] text-muted-foreground">
      <CollapsibleTrigger className="flex items-center gap-1 rounded-sm outline-none select-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
        <ChevronRightIcon
          aria-hidden="true"
          className={`size-3.5 transition-transform motion-reduce:transition-none ${open ? 'rotate-90' : ''}`}
        />
        Session mode: {value ? LABEL[value] : defaultLabel}
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-1 flex flex-wrap items-center gap-2">
        <Select
          value={value || DEFAULT}
          onValueChange={(next) => onChange(next === 'classic' || next === 'durable' ? next : '')}
        >
          <SelectTrigger size="sm" aria-label="Session mode" className="text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={DEFAULT}>{defaultLabel}</SelectItem>
            <SelectItem value="classic">Classic</SelectItem>
            <SelectItem value="durable">Durable</SelectItem>
          </SelectContent>
        </Select>
        <span>
          Same tools and plugins either way, except that a durable chat&apos;s subagents have only ScadBuddy&apos;s own
          tools. Durable chats keep running through a restart of the assistant.
        </span>
      </CollapsibleContent>
    </Collapsible>
  )
  // Outside the disclosure, so a remembered Durable that would be refused is seen closed.
  return (
    <>
      {picker}
      {unavailable && (
        <p className="mb-1.5 text-[11.5px] text-warn" data-testid="session-mode-picker-unavailable">
          Durable sessions cannot start right now
          {current.durable_unavailable_reason ? `: ${current.durable_unavailable_reason}` : ''}. Pick Default or
          Classic under Session mode to chat.
        </p>
      )}
    </>
  )
}
