import { useState } from 'react'
import { ChevronDownIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Command, CommandEmpty, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { statusLabel } from '../../agent/chat/labels'
import { useAssistantOpener } from '../../agent/chat/opener'
import { api } from '../../api/client'
import type { ResourceRef } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Tip } from './Tip'

/** The most sessions listed; one more is asked for, so the count can say there are more. */
export const RESOURCE_SESSIONS_SHOWN = 100

interface Props {
  resource: ResourceRef
  /** The toggle's text, before the count. */
  label?: string
  /** The model the resource belongs to: the list reads again when that model changes. */
  model?: string
}

/**
 * #931 — the assistant sessions whose tool calls changed this resource
 * (`GET /api/v1/ai/resources/:type/:id/sessions`, the reverse of the panel's Touched
 * list), as a small menu on the resource's page; picking one opens it in the panel.
 * Shows nothing without the assistant, while it loads, when the agent cannot answer,
 * or when no session touched it: it is a pointer, not a status.
 */
export function ResourceSessions({ resource, label = 'Changed by assistant', model }: Props) {
  const opener = useAssistantOpener()
  const enabled = opener !== null
  const listed = useAsync(
    async () => (enabled ? (await api.listAiResourceSessions(resource, RESOURCE_SESSIONS_SHOWN + 1)).sessions : null),
    [enabled, resource.type, resource.id],
    enabled && model ? [`model:${model}`] : undefined,
  )
  const [open, setOpen] = useState(false)

  if (!opener || !listed.data || listed.data.length === 0) return null
  const more = listed.data.length > RESOURCE_SESSIONS_SHOWN
  const sessions = more ? listed.data.slice(0, RESOURCE_SESSIONS_SHOWN) : listed.data

  // #3673 — a popover with a command list: type to find a session, arrows to move, Enter
  // to open it; Escape or a click outside closes it, focus back on the toggle.
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="sm" className="text-xs text-muted-foreground">
          {label} ({sessions.length}
          {more ? '+' : ''})
          <ChevronDownIcon data-icon="inline-end" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <Command label="Find a session">
          {/* It takes the focus and the keys: type to filter, arrows to move, Enter to open. */}
          <CommandInput placeholder="Find a session…" aria-label="Find a session" />
          <CommandList label="Assistant sessions that changed this">
            <CommandEmpty>No session matches.</CommandEmpty>
            {sessions.map((s) => (
              <Tip key={s.id} content={s.id} side="left">
                <CommandItem
                  value={s.id}
                  keywords={[s.title || 'Untitled session']}
                  onSelect={() => {
                    setOpen(false)
                    opener.openSession(s.id)
                  }}
                  className="flex-col items-start gap-0 [&>svg:last-child]:hidden"
                >
                  <span className="block w-full truncate text-[13px]">{s.title || 'Untitled session'}</span>
                  <span className="block text-[11px] text-muted-foreground">
                    {statusLabel(s.status)} · {new Date(s.updated_at).toLocaleString()}
                  </span>
                </CommandItem>
              </Tip>
            ))}
          </CommandList>
          {more && (
            <p className="border-t border-border px-3 py-1.5 text-[11px] text-muted-foreground">
              Showing the {RESOURCE_SESSIONS_SHOWN} most recent.
            </p>
          )}
        </Command>
      </PopoverContent>
    </Popover>
  )
}
